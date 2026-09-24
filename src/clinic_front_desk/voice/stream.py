"""``VoiceStreamManager`` — the Nova Sonic bidirectional voice stream boundary (task 9.1).

Task 9.1 (Req 12.1, 12.2). See design "Voice_Front_Desk / VoiceStreamManager":

    "``VoiceStreamManager`` — Wraps the Strands ``BidiAgent`` / Nova Sonic stream;
     emits interpreted turns, handles barge-in stop (≤ 500 ms) and response-start
     timing (≤ 1.5 s)."

Design intent — a mockable boundary
-----------------------------------
Nova Sonic is a *speech-to-speech* model reached over a persistent bidirectional
stream (Strands' :class:`BidiAgent`). That real stream needs the network,
credentials, and audio hardware, none of which belong in a unit or latency test.
So this module splits the responsibility in two:

- :class:`VoiceStream` — a narrow, provider-agnostic :class:`~typing.Protocol`
  describing exactly what the manager needs from *any* bidirectional voice
  transport: start, send audio/text, stop in-progress playback (for barge-in),
  iterate normalized events, and close. Orchestration and latency tests inject a
  fake implementation of this Protocol instead of real Nova Sonic.
- :class:`NovaSonicVoiceStream` — the concrete adapter that fulfils
  :class:`VoiceStream` by driving a Strands :class:`BidiAgent` backed by Nova
  Sonic through Amazon Bedrock, translating provider events
  (``BidiOutputEvent``) into this module's normalized :data:`VoiceStreamEvent`\\ s.

:class:`VoiceStreamManager` depends **only** on the :class:`VoiceStream`
Protocol and on the normalized events — never on Strands directly — so the core
lifecycle, interpreted-turn emission, response-start timing, and barge-in stop
logic are fully exercisable with a fake stream.

Timing contract (documented constants, not magic numbers)
---------------------------------------------------------
- Req 12.1: after the patient finishes speaking, an audible response must begin
  within **1.5 s** — :data:`VoiceStreamManager.RESPONSE_START_BUDGET_MS`.
- Req 12.2: while speaking, on a detected barge-in the agent must stop within
  **500 ms** — :data:`VoiceStreamManager.BARGE_IN_STOP_BUDGET_MS`.

The manager *measures* both intervals against a monotonic clock and reports them
(with a ``within_budget`` flag) through its hooks and last-timing properties, so
the latency tests (tasks 9.3, 9.4) can assert the budgets against a fake stream.

Hooks / callbacks
-----------------
The manager exposes three primary hooks required by task 9.1, plus convenience
hooks for audio output and errors. Each hook is a list of handlers registered via
``add_*_handler`` (or passed to the constructor). Handlers may be plain callables
or coroutine functions; coroutine results are awaited.

- **interpreted-turn** — a finalized interpreted turn (Nova Sonic's transcription
  of a user or assistant turn) is available.
- **barge-in** — the patient spoke while the agent was talking; the manager has
  already told the stream to stop playback and reports the measured stop latency.
- **response-start** — the model has begun an audible response; the manager
  reports the measured latency since the patient finished speaking.
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, Union, runtime_checkable

logger = logging.getLogger(__name__)

__all__ = [
    # normalized events
    "StreamConnected",
    "ResponseStarted",
    "InterpretedTurn",
    "AudioOutput",
    "BargeInDetected",
    "ResponseCompleted",
    "StreamError",
    "StreamClosed",
    "VoiceStreamEvent",
    # timings
    "ResponseStartTiming",
    "BargeInStopTiming",
    # boundary + manager + adapter
    "VoiceStream",
    "VoiceStreamManager",
    "NovaSonicVoiceStream",
]


# ---------------------------------------------------------------------------
# Normalized, provider-agnostic stream events.
#
# The manager reasons only over these, so the same logic serves Nova Sonic in
# production and a fake stream in tests. One frozen dataclass per ``kind``,
# mirroring the ``ToolError`` / ``TurnAction`` union style used elsewhere in the
# Voice_Front_Desk package.
# ---------------------------------------------------------------------------

Role = Literal["user", "assistant"]


@dataclass(frozen=True)
class StreamConnected:
    """The bidirectional connection is established and ready."""

    connection_id: str | None = None
    model: str | None = None
    kind: Literal["connected"] = "connected"


@dataclass(frozen=True)
class ResponseStarted:
    """The model has begun generating an (audible) response (Req 12.1).

    Arrival of this event ends the response-start interval that began when the
    patient finished speaking.
    """

    response_id: str | None = None
    kind: Literal["response_started"] = "response_started"


@dataclass(frozen=True)
class InterpretedTurn:
    """A finalized interpreted turn — Nova Sonic's transcription of speech.

    ``role`` distinguishes the patient (``"user"``) from the agent
    (``"assistant"``). A finalized *user* turn marks the moment the patient
    finished speaking and therefore starts the response-start timer (Req 12.1).
    Partial (non-final) transcripts are not surfaced as interpreted turns.
    """

    text: str
    role: Role = "user"
    kind: Literal["interpreted_turn"] = "interpreted_turn"


@dataclass(frozen=True)
class AudioOutput:
    """A chunk of assistant audio to be played back to the patient.

    The manager forwards these to its audio-output hook while the agent is
    speaking, and suppresses them immediately after a barge-in so the agent
    stops speaking (Req 12.2).
    """

    audio: str
    format: str = "pcm"
    sample_rate: int = 24000
    channels: int = 1
    kind: Literal["audio_output"] = "audio_output"


@dataclass(frozen=True)
class BargeInDetected:
    """The patient spoke while the agent was speaking (Req 12.2)."""

    reason: str = "user_speech"
    kind: Literal["barge_in"] = "barge_in"


@dataclass(frozen=True)
class ResponseCompleted:
    """The model finished (or stopped) generating a response."""

    response_id: str | None = None
    stop_reason: str | None = None
    kind: Literal["response_completed"] = "response_completed"


@dataclass(frozen=True)
class StreamError:
    """An error surfaced on the stream."""

    message: str
    code: str | None = None
    kind: Literal["error"] = "error"


@dataclass(frozen=True)
class StreamClosed:
    """The bidirectional connection was closed; the event loop ends."""

    reason: str | None = None
    kind: Literal["closed"] = "closed"


#: Discriminated union of normalized events a :class:`VoiceStream` yields.
VoiceStreamEvent = Union[
    StreamConnected,
    ResponseStarted,
    InterpretedTurn,
    AudioOutput,
    BargeInDetected,
    ResponseCompleted,
    StreamError,
    StreamClosed,
]


# ---------------------------------------------------------------------------
# Timing reports (the ≤1.5 s / ≤500 ms contract, measured).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResponseStartTiming:
    """Measured response-start latency for one turn (Req 12.1).

    ``latency_ms`` is the elapsed time from the patient finishing speaking to the
    model beginning its audible response. ``within_budget`` is ``True`` iff it is
    at most :data:`VoiceStreamManager.RESPONSE_START_BUDGET_MS`.
    """

    latency_ms: float
    within_budget: bool
    budget_ms: float


@dataclass(frozen=True)
class BargeInStopTiming:
    """Measured barge-in stop latency for one interruption (Req 12.2).

    ``latency_ms`` is the elapsed time from the barge-in being detected to the
    stream's playback being stopped. ``within_budget`` is ``True`` iff it is at
    most :data:`VoiceStreamManager.BARGE_IN_STOP_BUDGET_MS`.
    """

    latency_ms: float
    within_budget: bool
    budget_ms: float


# ---------------------------------------------------------------------------
# The mockable boundary.
# ---------------------------------------------------------------------------


@runtime_checkable
class VoiceStream(Protocol):
    """A narrow bidirectional voice transport the manager drives (task 9.1).

    Any implementation — the real Nova Sonic adapter or a test fake — provides
    exactly these operations. Keeping the surface this small is what makes the
    manager testable without real Nova Sonic.
    """

    async def start(self) -> None:
        """Open the bidirectional connection and begin streaming."""
        ...

    async def send_audio(
        self,
        audio: str,
        *,
        format: str = "pcm",
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> None:
        """Send a chunk of patient audio (base64-encoded) to the model."""
        ...

    async def send_text(self, text: str) -> None:
        """Send a text turn to the model (used for text-mode/testing)."""
        ...

    async def stop_playback(self) -> None:
        """Stop any in-progress assistant playback for a barge-in (Req 12.2)."""
        ...

    def events(self) -> AsyncIterator[VoiceStreamEvent]:
        """Yield normalized stream events until the connection closes."""
        ...

    async def close(self) -> None:
        """Close the bidirectional connection and release resources."""
        ...


#: A hook handler: called with a single event/timing argument. May be sync or
#: async; async results are awaited.
Handler = Callable[[Any], Union[None, Awaitable[None]]]


# ---------------------------------------------------------------------------
# VoiceStreamManager
# ---------------------------------------------------------------------------


class VoiceStreamManager:
    """Owns one Call_Session's bidirectional voice stream (Req 12.1, 12.2).

    Depends only on the :class:`VoiceStream` Protocol. Responsibilities:

    - **Lifecycle**: :meth:`start` opens the stream, :meth:`run` consumes its
      events until it closes, :meth:`stop` closes it.
    - **Interpreted turns**: finalized transcripts are emitted to the
      interpreted-turn hook; a finalized user turn also starts the response-start
      timer.
    - **Response-start timing (Req 12.1)**: measures the interval from the patient
      finishing speaking to :class:`ResponseStarted`, reports it via the
      response-start hook and :attr:`last_response_start_timing`.
    - **Barge-in stop (Req 12.2)**: on :class:`BargeInDetected` it tells the
      stream to stop playback, suppresses further assistant audio until the next
      response, measures the stop latency, and reports it via the barge-in hook
      and :attr:`last_barge_in_timing`.
    """

    #: Response must begin within 1.5 s of the patient finishing speaking (Req 12.1).
    RESPONSE_START_BUDGET_MS: float = 1500.0

    #: Playback must stop within 500 ms of a detected barge-in (Req 12.2).
    BARGE_IN_STOP_BUDGET_MS: float = 500.0

    def __init__(
        self,
        stream: VoiceStream,
        *,
        on_interpreted_turn: Handler | None = None,
        on_barge_in: Handler | None = None,
        on_response_start: Handler | None = None,
        on_audio_output: Handler | None = None,
        on_error: Handler | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Create a manager over ``stream``.

        Args:
            stream: The bidirectional transport (real Nova Sonic adapter or fake).
            on_interpreted_turn: Optional initial interpreted-turn handler.
            on_barge_in: Optional initial barge-in handler (receives
                :class:`BargeInStopTiming`).
            on_response_start: Optional initial response-start handler (receives
                :class:`ResponseStartTiming`).
            on_audio_output: Optional initial assistant-audio handler.
            on_error: Optional initial stream-error handler.
            clock: Monotonic time source in seconds (default :func:`time.monotonic`).
                Injectable so latency tests can drive timing deterministically.
        """
        self._stream = stream
        self._clock = clock or time.monotonic

        self._interpreted_turn_handlers: list[Handler] = []
        self._barge_in_handlers: list[Handler] = []
        self._response_start_handlers: list[Handler] = []
        self._audio_output_handlers: list[Handler] = []
        self._error_handlers: list[Handler] = []

        for handler, registry in (
            (on_interpreted_turn, self._interpreted_turn_handlers),
            (on_barge_in, self._barge_in_handlers),
            (on_response_start, self._response_start_handlers),
            (on_audio_output, self._audio_output_handlers),
            (on_error, self._error_handlers),
        ):
            if handler is not None:
                registry.append(handler)

        # Lifecycle / runtime state.
        self._started = False
        self._closed = False
        self._speaking = False
        # While True, assistant audio is dropped rather than played — the
        # post-barge-in "stopped speaking" state, cleared on the next response.
        self._suppress_audio = False
        # Monotonic timestamp of the patient finishing speaking; None when no
        # response is awaited.
        self._response_pending_since: float | None = None

        self._last_response_start_timing: ResponseStartTiming | None = None
        self._last_barge_in_timing: BargeInStopTiming | None = None

    # -- registration -------------------------------------------------------

    def add_interpreted_turn_handler(self, handler: Handler) -> None:
        """Register a handler invoked with each finalized :class:`InterpretedTurn`."""
        self._interpreted_turn_handlers.append(handler)

    def add_barge_in_handler(self, handler: Handler) -> None:
        """Register a handler invoked with a :class:`BargeInStopTiming` on barge-in."""
        self._barge_in_handlers.append(handler)

    def add_response_start_handler(self, handler: Handler) -> None:
        """Register a handler invoked with a :class:`ResponseStartTiming` on response start."""
        self._response_start_handlers.append(handler)

    def add_audio_output_handler(self, handler: Handler) -> None:
        """Register a handler invoked with each played :class:`AudioOutput` chunk."""
        self._audio_output_handlers.append(handler)

    def add_error_handler(self, handler: Handler) -> None:
        """Register a handler invoked with each :class:`StreamError`."""
        self._error_handlers.append(handler)

    # -- read-only state ----------------------------------------------------

    @property
    def started(self) -> bool:
        """Whether the stream has been started and not yet closed."""
        return self._started and not self._closed

    @property
    def speaking(self) -> bool:
        """Whether the agent is currently producing an audible response."""
        return self._speaking

    @property
    def awaiting_response(self) -> bool:
        """Whether the patient has finished speaking and a response is pending."""
        return self._response_pending_since is not None

    @property
    def last_response_start_timing(self) -> ResponseStartTiming | None:
        """The most recent measured response-start latency (Req 12.1), or ``None``."""
        return self._last_response_start_timing

    @property
    def last_barge_in_timing(self) -> BargeInStopTiming | None:
        """The most recent measured barge-in stop latency (Req 12.2), or ``None``."""
        return self._last_barge_in_timing

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Open the underlying stream (idempotent guard, Req 12.1 lifecycle).

        Raises:
            RuntimeError: If already started (mirrors ``BidiAgent.start``).
        """
        if self._started:
            raise RuntimeError("voice stream already started | call stop before starting again")
        await self._stream.start()
        self._started = True
        self._closed = False

    async def send_audio(
        self,
        audio: str,
        *,
        format: str = "pcm",
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> None:
        """Forward a chunk of patient audio to the model."""
        self._require_started()
        await self._stream.send_audio(
            audio, format=format, sample_rate=sample_rate, channels=channels
        )

    async def send_text(self, text: str) -> None:
        """Forward a text turn to the model (text-mode / testing)."""
        self._require_started()
        await self._stream.send_text(text)

    async def run(self) -> None:
        """Consume stream events until the connection closes (Req 12.1, 12.2).

        Dispatches each normalized event: emits interpreted turns, times
        response starts, and stops playback on barge-in. Returns when a
        :class:`StreamClosed` event is seen or the stream's event iterator is
        exhausted.
        """
        self._require_started()
        async for event in self._stream.events():
            done = await self._handle(event)
            if done:
                break

    async def stop(self) -> None:
        """Close the underlying stream and mark the session ended (idempotent)."""
        if self._closed or not self._started:
            self._closed = True
            self._started = False
            return
        await self._stream.close()
        self._closed = True
        self._started = False
        self._speaking = False
        self._suppress_audio = False
        self._response_pending_since = None

    # -- event handling -----------------------------------------------------

    async def _handle(self, event: VoiceStreamEvent) -> bool:
        """Dispatch one normalized event. Returns ``True`` when the loop should end."""
        kind = event.kind

        if kind == "interpreted_turn":
            await self._on_interpreted_turn(event)  # type: ignore[arg-type]
        elif kind == "response_started":
            await self._on_response_started(event)  # type: ignore[arg-type]
        elif kind == "audio_output":
            await self._on_audio_output(event)  # type: ignore[arg-type]
        elif kind == "barge_in":
            await self._on_barge_in(event)  # type: ignore[arg-type]
        elif kind == "response_completed":
            self._speaking = False
        elif kind == "error":
            await self._emit(self._error_handlers, event)
        elif kind == "closed":
            self._speaking = False
            self._closed = True
            return True
        # StreamConnected and any other event need no state change.
        return False

    async def _on_interpreted_turn(self, event: InterpretedTurn) -> None:
        # A finalized user turn = the patient finished speaking, so start the
        # response-start timer (Req 12.1). Assistant transcripts are surfaced too
        # but do not start the timer.
        if event.role == "user":
            self._response_pending_since = self._clock()
        await self._emit(self._interpreted_turn_handlers, event)

    async def _on_response_started(self, event: ResponseStarted) -> None:
        self._speaking = True
        # A fresh response clears any post-barge-in audio suppression.
        self._suppress_audio = False
        if self._response_pending_since is not None:
            elapsed_ms = (self._clock() - self._response_pending_since) * 1000.0
            timing = ResponseStartTiming(
                latency_ms=elapsed_ms,
                within_budget=elapsed_ms <= self.RESPONSE_START_BUDGET_MS,
                budget_ms=self.RESPONSE_START_BUDGET_MS,
            )
            self._last_response_start_timing = timing
            self._response_pending_since = None
            await self._emit(self._response_start_handlers, timing)

    async def _on_audio_output(self, event: AudioOutput) -> None:
        # Drop assistant audio while suppressed (post-barge-in) so the agent is
        # actually silent after an interruption (Req 12.2).
        if self._suppress_audio:
            return
        self._speaking = True
        await self._emit(self._audio_output_handlers, event)

    async def _on_barge_in(self, event: BargeInDetected) -> None:
        # Measure how long stopping playback takes and enforce the ≤500 ms
        # contract (Req 12.2). Stop playback first, then compute the interval.
        started_at = self._clock()
        await self._stream.stop_playback()
        elapsed_ms = (self._clock() - started_at) * 1000.0

        # Stop speaking: suppress any further buffered audio until the next
        # response begins.
        self._speaking = False
        self._suppress_audio = True

        timing = BargeInStopTiming(
            latency_ms=elapsed_ms,
            within_budget=elapsed_ms <= self.BARGE_IN_STOP_BUDGET_MS,
            budget_ms=self.BARGE_IN_STOP_BUDGET_MS,
        )
        self._last_barge_in_timing = timing
        await self._emit(self._barge_in_handlers, timing)

    # -- helpers ------------------------------------------------------------

    async def _emit(self, handlers: list[Handler], payload: Any) -> None:
        """Invoke each handler with ``payload``, awaiting coroutine results."""
        for handler in handlers:
            result = handler(payload)
            if inspect.isawaitable(result):
                await result

    def _require_started(self) -> None:
        if not self._started or self._closed:
            raise RuntimeError("voice stream not started | call start before use")


# ---------------------------------------------------------------------------
# Concrete Nova Sonic adapter (behind the VoiceStream boundary).
# ---------------------------------------------------------------------------


class NovaSonicVoiceStream:
    """Nova Sonic / Strands ``BidiAgent`` adapter implementing :class:`VoiceStream`.

    This is the only component in the voice-stream layer that knows about Strands
    and Nova Sonic. It drives a :class:`BidiAgent` (speech-to-speech through
    Amazon Bedrock) and translates its ``BidiOutputEvent`` stream into this
    module's normalized :data:`VoiceStreamEvent`\\ s so that
    :class:`VoiceStreamManager` never depends on the provider.

    Documented assumptions
    -----------------------
    - **Event mapping** follows the Strands bidi event contract: a *final*
      ``BidiTranscriptStreamEvent`` becomes an :class:`InterpretedTurn`;
      ``BidiResponseStartEvent`` → :class:`ResponseStarted`;
      ``BidiAudioStreamEvent`` → :class:`AudioOutput`;
      ``BidiInterruptionEvent(reason="user_speech")`` → :class:`BargeInDetected`;
      ``BidiResponseCompleteEvent`` → :class:`ResponseCompleted`;
      ``BidiConnectionStartEvent`` → :class:`StreamConnected`;
      ``BidiConnectionCloseEvent`` → :class:`StreamClosed`;
      ``BidiErrorEvent`` → :class:`StreamError`. Other events are ignored.
    - **Barge-in stop**: the current experimental ``BidiAgent`` exposes no direct
      "stop the current audio playback" call. Nova Sonic itself detects the
      barge-in (emitting the interruption event) and halts generation; the
      manager additionally suppresses any already-buffered assistant audio. So
      :meth:`stop_playback` here is a fast, local best-effort no-op that satisfies
      the ≤ 500 ms contract by not doing blocking work. If a future SDK adds an
      explicit cancel/flush API, it belongs *here* only — the manager and
      Protocol are unaffected.
    """

    #: Default Amazon Nova Sonic model id (the v2 speech-to-speech model on
    #: Amazon Bedrock). Overridable via the ``model_id`` constructor arg.
    DEFAULT_MODEL_ID = "amazon.nova-2-sonic-v1:0"

    #: Default Nova Sonic output voice.
    DEFAULT_VOICE_ID = "matthew"

    def __init__(
        self,
        agent: Any | None = None,
        *,
        model: Any | None = None,
        model_id: str | None = None,
        region: str | None = None,
        voice_id: str | None = None,
        endpointing_sensitivity: str = "MEDIUM",
        provider_config: dict[str, Any] | None = None,
        client_config: dict[str, Any] | None = None,
        tools: list[Any] | None = None,
        system_prompt: str | None = None,
        agent_kwargs: dict[str, Any] | None = None,
    ) -> None:
        """Create the adapter.

        Args:
            agent: An already-constructed Strands ``BidiAgent``. When ``None`` a
                ``BidiAgent`` is built lazily on :meth:`start` (this keeps the
                Strands/Bedrock imports out of module import time so the rest of
                the system tests without AWS).
            model: An explicit ``BidiModel`` instance or model-id string. When
                ``None`` (the default) a real
                :class:`~strands.experimental.bidi.models.nova_sonic.BidiNovaSonicModel`
                is constructed from ``model_id`` / ``region`` / ``voice_id`` /
                ``endpointing_sensitivity`` (and any ``provider_config`` /
                ``client_config`` overrides), so the production path uses genuine
                Nova Sonic speech-to-speech over Amazon Bedrock.
            model_id: Nova Sonic model id (default :data:`DEFAULT_MODEL_ID`).
            region: AWS region for the Bedrock connection (default: the ambient
                boto3 session region, else ``us-east-1``).
            voice_id: Nova Sonic output voice (default :data:`DEFAULT_VOICE_ID`).
            endpointing_sensitivity: Nova Sonic v2 turn-detection sensitivity —
                ``"HIGH"`` | ``"MEDIUM"`` | ``"LOW"``.
            provider_config: Extra Nova Sonic provider config merged over the
                audio/turn-detection defaults built here.
            client_config: Extra AWS client config (e.g. a ``boto_session``);
                merged with the resolved ``region``.
            tools: Tools to register with the built ``BidiAgent`` (the nine
                patient-facing tools, wired by the agent composition).
            system_prompt: The guardrail system prompt for the built agent.
            agent_kwargs: Extra keyword arguments forwarded to ``BidiAgent``.
        """
        self._agent = agent
        self._model = model
        self._model_id = model_id or self.DEFAULT_MODEL_ID
        self._region = region
        self._voice_id = voice_id or self.DEFAULT_VOICE_ID
        self._endpointing_sensitivity = endpointing_sensitivity
        self._provider_config = provider_config
        self._client_config = client_config
        self._tools = tools
        self._system_prompt = system_prompt
        self._agent_kwargs = dict(agent_kwargs or {})

    def _build_nova_sonic_model(self) -> Any:
        """Construct a real ``BidiNovaSonicModel`` for the Bedrock connection.

        Imported lazily so the ``aws-sdk-bedrock-runtime`` dependency is only
        required when an actual voice session is started (installable via the
        ``voice`` extra), keeping the rest of the system importable and testable
        without AWS.
        """
        from strands.experimental.bidi.models.nova_sonic import BidiNovaSonicModel

        provider_config: dict[str, Any] = {"audio": {"voice": self._voice_id}}
        # Turn detection is a Nova Sonic *v2*-only feature; only default it for
        # the v2 model so a v1 model_id does not get rejected.
        if "nova-2-sonic" in self._model_id:
            provider_config["turn_detection"] = {
                "endpointingSensitivity": self._endpointing_sensitivity
            }
        if self._provider_config:
            # Shallow-merge caller overrides over the defaults, deep-merging the
            # nested audio/turn_detection maps so partial overrides still work.
            for key, value in self._provider_config.items():
                if isinstance(value, dict) and isinstance(provider_config.get(key), dict):
                    provider_config[key] = {**provider_config[key], **value}
                else:
                    provider_config[key] = value

        client_config: dict[str, Any] = dict(self._client_config or {})
        # Only set a region when the caller did not supply a boto_session (the
        # model rejects specifying both).
        if "boto_session" not in client_config and self._region is not None:
            client_config["region"] = self._region

        return BidiNovaSonicModel(
            model_id=self._model_id,
            provider_config=provider_config,
            client_config=client_config or None,
        )

    async def start(self) -> None:
        """Build (if needed) and start the underlying ``BidiAgent``."""
        if self._agent is None:
            from strands.experimental.bidi import BidiAgent

            # Build a genuine Nova Sonic model unless the caller injected an
            # explicit model/model-id, so the production path is real Bedrock
            # speech-to-speech rather than relying on BidiAgent's default.
            model = self._model if self._model is not None else self._build_nova_sonic_model()
            self._agent = BidiAgent(
                model=model,
                tools=self._tools,
                system_prompt=self._system_prompt,
                **self._agent_kwargs,
            )
        await self._agent.start()

    async def send_audio(
        self,
        audio: str,
        *,
        format: str = "pcm",
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> None:
        """Send patient audio to Nova Sonic via a ``BidiAudioInputEvent``."""
        from strands.experimental.bidi import BidiAudioInputEvent

        await self._require_agent().send(
            BidiAudioInputEvent(
                audio=audio,
                format=format,
                sample_rate=sample_rate,  # type: ignore[arg-type]
                channels=channels,  # type: ignore[arg-type]
            )
        )

    async def send_text(self, text: str) -> None:
        """Send a text turn to Nova Sonic (``BidiAgent.send`` accepts ``str``)."""
        await self._require_agent().send(text)

    async def stop_playback(self) -> None:
        """Best-effort barge-in stop (see class docstring). Intentionally fast."""
        # Nova Sonic halts generation on the interruption it detected; there is
        # no separate blocking cancel to issue here. Kept as a hook so a future
        # explicit flush/cancel API lives behind this boundary only.
        return None

    async def events(self) -> AsyncIterator[VoiceStreamEvent]:
        """Translate ``BidiAgent.receive()`` output into normalized events."""
        async for event in self._require_agent().receive():
            translated = self._translate(event)
            if translated is not None:
                yield translated

    async def close(self) -> None:
        """Stop the underlying ``BidiAgent`` connection."""
        if self._agent is not None:
            await self._agent.stop()

    # -- internals ----------------------------------------------------------

    def _require_agent(self) -> Any:
        if self._agent is None:
            raise RuntimeError("NovaSonicVoiceStream not started | call start first")
        return self._agent

    @staticmethod
    def _translate(event: Any) -> VoiceStreamEvent | None:
        """Map one Strands ``BidiOutputEvent`` to a normalized event (or ``None``)."""
        etype = event.get("type") if isinstance(event, dict) else None

        if etype == "bidi_connection_start":
            return StreamConnected(
                connection_id=event.get("connection_id"),
                model=event.get("model"),
            )
        if etype == "bidi_response_start":
            return ResponseStarted(response_id=event.get("response_id"))
        if etype == "bidi_transcript_stream":
            # Only finalized transcripts are surfaced as interpreted turns.
            if not event.get("is_final"):
                return None
            # Compared case-insensitively. Nova Sonic labels roles in upper case
            # ("ASSISTANT"/"USER"), so an exact match against "assistant" quietly
            # relabelled every agent turn as the patient's — and since the recorder
            # maps anything that is not "assistant" to "patient", the stored
            # transcript of every call came out one-sided.
            role = str(event.get("role") or "user").strip().lower()
            if role not in ("assistant", "user"):
                # Logged rather than assumed: a role this code does not recognise is
                # the exact shape of the bug above, and it should be visible.
                logger.warning(
                    "unrecognised transcript role %r, treating it as the caller",
                    event.get("role"),
                )
            return InterpretedTurn(
                text=event.get("text", ""),
                role="assistant" if role == "assistant" else "user",
            )
        if etype == "bidi_audio_stream":
            return AudioOutput(
                audio=event.get("audio", ""),
                format=event.get("format", "pcm"),
                sample_rate=event.get("sample_rate", 24000),
                channels=event.get("channels", 1),
            )
        if etype == "bidi_interruption":
            return BargeInDetected(reason=event.get("reason", "user_speech"))
        if etype == "bidi_response_complete":
            return ResponseCompleted(
                response_id=event.get("response_id"),
                stop_reason=event.get("stop_reason"),
            )
        if etype == "bidi_connection_close":
            return StreamClosed(reason=event.get("reason"))
        if etype == "bidi_error":
            return StreamError(
                message=event.get("message", ""),
                code=event.get("code"),
            )
        # Usage, restart, image, tool-use stream events, etc. are not part of the
        # manager's contract.
        return None
