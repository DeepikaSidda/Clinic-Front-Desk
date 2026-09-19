"""Live human takeover: the caller stays on the line and talks to a person.

Everything else in this package *notifies* a human. This hands the call over while
the caller is still holding, which is the only version that does not ask them to
wait for a callback.

How it works, and why each piece is where it is:

**A registry of calls in progress.** Each WebSocket handler owns its own session
locally, so nothing outside it could reach a call. :class:`LiveCallRegistry` is the
one place that knows which calls are open and how to speak into them. In-process on
purpose: one instance serves every call, and a call cannot outlive the process
holding its socket, so a shared store would add coordination for nothing.

**The agent goes quiet, but keeps listening.** On takeover the agent's audio is
dropped while its speech-to-text keeps running, so the doctor reads what the caller
says in real time without the caller hearing two voices. The existing barge-in
suppression is not reused for this: that flag clears on the next model response,
which is correct for an interruption and wrong for a handover that must persist
until the human hangs up.

**The human's words arrive as ordinary agent audio.** Amazon Polly synthesises what
the doctor types, and it is pushed down the same ``agent_audio`` channel the caller's
browser is already playing. Polly emits PCM at 16 kHz rather than the 24 kHz Nova
Sonic streams, which turned out not to matter: the client reads the sample rate off
each frame and lets the browser resample, so the honest label works and no
resampling happens on the call path.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

#: Polly emits PCM at 8 kHz or 16 kHz only. 16 kHz is the better of the two, and the
#: browser resamples it to the playback context's rate.
POLLY_SAMPLE_RATE = 16_000

#: An Indian-English neural voice, so a human stepping in does not sound like a
#: different clinic to the caller mid-call.
DEFAULT_VOICE_ID = "Kajal"
DEFAULT_ENGINE = "neural"

#: ~32 ms per frame at 16 kHz, matching the cadence the client already expects.
FRAME_SAMPLES = 512

#: A send callable bound to one caller's WebSocket.
Sender = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class LiveCall:
    """One call in progress, and the handles needed to step into it."""

    session_id: str
    send: Sender
    started_at: str
    #: Set when the agent has asked for a human, so the doctor's console can show
    #: which calls are actually waiting rather than listing every call.
    needs_human: bool = False
    #: Why a human is wanted, taken from the escalation.
    reason: str = ""
    #: True once a human is on the call. While true the agent is silent.
    taken_over: bool = False
    #: Who the caller is, as far as the call has established.
    patient_name: str = ""
    callback_phone: str = ""
    #: Rolling transcript, so a doctor joining late can read what they missed.
    transcript: list[dict[str, str]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """The shape the doctor's console renders."""
        return {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "needs_human": self.needs_human,
            "reason": self.reason,
            "taken_over": self.taken_over,
            "patient_name": self.patient_name,
            "callback_phone": self.callback_phone,
            "turns": len(self.transcript),
        }


class LiveCallRegistry:
    """The calls currently open, and the only way to speak into one.

    Deliberately tiny and synchronous apart from the sends. It is touched on the
    call path, so it holds no locks that a slow doctor console could contend on.
    """

    def __init__(self) -> None:
        self._calls: dict[str, LiveCall] = {}

    def register(self, session_id: str, send: Sender) -> LiveCall:
        call = LiveCall(
            session_id=session_id,
            send=send,
            started_at=datetime.now(UTC).isoformat(),
        )
        self._calls[session_id] = call
        return call

    def unregister(self, session_id: str) -> None:
        self._calls.pop(session_id, None)

    def get(self, session_id: str) -> LiveCall | None:
        return self._calls.get(session_id)

    def list_calls(self) -> list[dict[str, Any]]:
        """Waiting calls first — that is what the doctor is looking for."""
        calls = sorted(
            self._calls.values(),
            key=lambda c: (not c.needs_human, c.started_at),
        )
        return [call.summary() for call in calls]

    def record_turn(self, session_id: str, role: str, text: str) -> None:
        call = self._calls.get(session_id)
        if call is not None:
            call.transcript.append({"role": role, "text": text})

    def mark_needs_human(self, session_id: str, reason: str) -> None:
        call = self._calls.get(session_id)
        if call is not None:
            call.needs_human = True
            call.reason = reason

    def identify(
        self, session_id: str, *, name: str = "", phone: str = ""
    ) -> None:
        call = self._calls.get(session_id)
        if call is None:
            return
        if name:
            call.patient_name = name
        if phone:
            call.callback_phone = phone

    def transcript_of(self, session_id: str) -> list[dict[str, str]]:
        call = self._calls.get(session_id)
        return list(call.transcript) if call is not None else []


class LiveHandoverService:
    """Takes a call over, and speaks the doctor's words into it."""

    def __init__(
        self,
        registry: LiveCallRegistry,
        *,
        polly: Any | None = None,
        region: str | None = None,
        voice_id: str = DEFAULT_VOICE_ID,
        engine: str = DEFAULT_ENGINE,
    ) -> None:
        self._registry = registry
        self._polly = polly
        self._region = region
        self._voice_id = voice_id
        self._engine = engine

    def _client(self) -> Any:
        if self._polly is None:
            # Lazy so importing this costs nothing and needs no credentials.
            import boto3  # type: ignore[import-untyped]

            self._polly = boto3.client("polly", region_name=self._region)
        return self._polly

    async def take_over(self, session_id: str) -> bool:
        """Silence the agent and tell the caller a person is joining.

        The caller is told explicitly. Going quiet and then speaking in a different
        voice with no explanation is disorienting on a phone call.
        """
        call = self._registry.get(session_id)
        if call is None:
            return False
        call.taken_over = True
        await call.send(
            {
                "message_type": "human_joined",
                "session_id": session_id,
                "text": "A member of the clinic team has joined the call.",
            }
        )
        logger.info("live handover: a human took over %s", session_id)
        return True

    async def release(self, session_id: str) -> bool:
        """Hand the call back to the agent."""
        call = self._registry.get(session_id)
        if call is None:
            return False
        call.taken_over = False
        await call.send(
            {"message_type": "human_left", "session_id": session_id}
        )
        return True

    def synthesize(self, text: str) -> bytes:
        """The doctor's words as 16 kHz mono 16-bit PCM."""
        response = self._client().synthesize_speech(
            Text=text,
            OutputFormat="pcm",
            VoiceId=self._voice_id,
            Engine=self._engine,
            SampleRate=str(POLLY_SAMPLE_RATE),
        )
        audio: bytes = response["AudioStream"].read()
        return audio

    async def say(self, session_id: str, text: str) -> bool:
        """Speak ``text`` to the caller as the human on the call.

        Framed and paced rather than sent as one blob: the client plays frames as
        they arrive, and a single large buffer would arrive late and all at once.
        The transcript is updated first so the doctor sees their own line
        immediately, even if synthesis is slow.
        """
        call = self._registry.get(session_id)
        if call is None:
            return False

        self._registry.record_turn(session_id, "human", text)
        await call.send(
            {
                "message_type": "transcript",
                "session_id": session_id,
                "role": "human",
                "text": text,
            }
        )

        try:
            pcm = await asyncio.to_thread(self.synthesize, text)
        except Exception as exc:  # noqa: BLE001 - a failed voice must not drop the call
            logger.warning("Polly synthesis failed for %s: %s", session_id, exc)
            return False

        frame_bytes = FRAME_SAMPLES * 2
        for offset in range(0, len(pcm), frame_bytes):
            chunk = pcm[offset : offset + frame_bytes]
            await call.send(
                {
                    "message_type": "agent_audio",
                    "session_id": session_id,
                    "audio": base64.b64encode(chunk).decode(),
                    "format": "pcm",
                    # Labelled honestly at Polly's rate; the browser resamples.
                    "sample_rate": POLLY_SAMPLE_RATE,
                    "channels": 1,
                }
            )
        return True


__all__ = [
    "DEFAULT_ENGINE",
    "DEFAULT_VOICE_ID",
    "FRAME_SAMPLES",
    "POLLY_SAMPLE_RATE",
    "LiveCall",
    "LiveCallRegistry",
    "LiveHandoverService",
]
