"""Unit tests for ``VoiceStreamManager`` and the Nova Sonic adapter (task 9.1).

These are example/edge-case unit tests for the voice-stream boundary (Req 12.1,
12.2). The *latency* tests that assert the ≤ 1.5 s response-start and ≤ 500 ms
barge-in budgets against a Nova Sonic test stream are tasks 9.3 and 9.4 and are
intentionally not written here — though this manager exposes the measured
timings (``last_response_start_timing`` / ``last_barge_in_timing``) those tests
will consume.

No ``pytest-asyncio`` is available, so async coroutines are driven with
``asyncio.run`` via the :func:`_run` helper. A scripted :class:`FakeVoiceStream`
plus a controllable :class:`FakeClock` let us drive lifecycle, interpreted-turn
emission, and timing deterministically without real Nova Sonic.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from clinic_front_desk.voice.stream import (
    AudioOutput,
    BargeInDetected,
    BargeInStopTiming,
    InterpretedTurn,
    NovaSonicVoiceStream,
    ResponseCompleted,
    ResponseStarted,
    ResponseStartTiming,
    StreamClosed,
    StreamConnected,
    StreamError,
    VoiceStream,
    VoiceStreamManager,
)


def _run(coro: Any) -> Any:
    """Drive a coroutine to completion (no pytest-asyncio in this project)."""
    return asyncio.run(coro)


class FakeClock:
    """A monotonic clock stand-in whose ``now`` the test controls (seconds)."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeVoiceStream:
    """A scripted :class:`VoiceStream` for driving the manager deterministically.

    Yields ``(clock_time, event)`` pairs: before each event is emitted the shared
    clock is set to ``clock_time``, so timing intervals are fully controlled.
    ``stop_playback`` advances the clock by ``stop_playback_delay`` to simulate
    the cost of stopping in-progress audio (used for barge-in latency tests).
    """

    def __init__(
        self,
        script: list[tuple[float, Any]] | None = None,
        *,
        clock: FakeClock | None = None,
        stop_playback_delay: float = 0.0,
    ) -> None:
        self._script = script or []
        self._clock = clock
        self.stop_playback_delay = stop_playback_delay

        self.started = False
        self.closed = False
        self.stop_playback_calls = 0
        self.sent_audio: list[dict[str, Any]] = []
        self.sent_text: list[str] = []

    async def start(self) -> None:
        self.started = True

    async def send_audio(
        self,
        audio: str,
        *,
        format: str = "pcm",
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> None:
        self.sent_audio.append(
            {"audio": audio, "format": format, "sample_rate": sample_rate, "channels": channels}
        )

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)

    async def stop_playback(self) -> None:
        self.stop_playback_calls += 1
        if self._clock is not None and self.stop_playback_delay:
            self._clock.advance(self.stop_playback_delay)

    async def events(self) -> AsyncIterator[Any]:
        for clock_time, event in self._script:
            if self._clock is not None:
                self._clock.now = clock_time
            yield event

    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Boundary shape
# ---------------------------------------------------------------------------


def test_fake_and_adapter_satisfy_voice_stream_protocol() -> None:
    """Both the fake and the real adapter are structurally ``VoiceStream``\\ s."""
    assert isinstance(FakeVoiceStream(), VoiceStream)
    assert isinstance(NovaSonicVoiceStream(), VoiceStream)


def test_timing_budgets_are_documented_constants() -> None:
    """The ≤1.5 s / ≤500 ms contract lives as named constants (Req 12.1, 12.2)."""
    assert VoiceStreamManager.RESPONSE_START_BUDGET_MS == 1500.0
    assert VoiceStreamManager.BARGE_IN_STOP_BUDGET_MS == 500.0


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_start_and_stop_lifecycle() -> None:
    stream = FakeVoiceStream()
    mgr = VoiceStreamManager(stream)

    assert mgr.started is False
    _run(mgr.start())
    assert stream.started is True
    assert mgr.started is True

    _run(mgr.stop())
    assert stream.closed is True
    assert mgr.started is False


def test_double_start_raises() -> None:
    mgr = VoiceStreamManager(FakeVoiceStream())
    _run(mgr.start())
    with pytest.raises(RuntimeError):
        _run(mgr.start())


def test_send_before_start_raises() -> None:
    mgr = VoiceStreamManager(FakeVoiceStream())
    with pytest.raises(RuntimeError):
        _run(mgr.send_text("hello"))
    with pytest.raises(RuntimeError):
        _run(mgr.send_audio("Zm9v"))


def test_stop_is_idempotent_before_start() -> None:
    mgr = VoiceStreamManager(FakeVoiceStream())
    # Stopping a never-started manager must not blow up or close the stream.
    _run(mgr.stop())
    assert mgr.started is False


def test_send_audio_and_text_forwarded_to_stream() -> None:
    stream = FakeVoiceStream()
    mgr = VoiceStreamManager(stream)
    _run(mgr.start())

    _run(mgr.send_text("book a hearing test"))
    _run(mgr.send_audio("Zm9v", format="pcm", sample_rate=16000, channels=1))

    assert stream.sent_text == ["book a hearing test"]
    assert stream.sent_audio[0]["audio"] == "Zm9v"
    assert stream.sent_audio[0]["sample_rate"] == 16000


def test_run_stops_on_closed_event() -> None:
    stream = FakeVoiceStream([(0.0, StreamClosed(reason="complete"))])
    mgr = VoiceStreamManager(stream)
    _run(mgr.start())
    _run(mgr.run())  # returns rather than hanging
    assert mgr.speaking is False


# ---------------------------------------------------------------------------
# Interpreted turns
# ---------------------------------------------------------------------------


def test_interpreted_turns_emitted_to_handler() -> None:
    turns: list[InterpretedTurn] = []
    script = [
        (0.0, InterpretedTurn(text="I'd like to book", role="user")),
        (0.1, InterpretedTurn(text="Sure, what service?", role="assistant")),
        (0.2, StreamClosed()),
    ]
    stream = FakeVoiceStream(script)
    mgr = VoiceStreamManager(stream, on_interpreted_turn=turns.append)
    _run(mgr.start())
    _run(mgr.run())

    assert [t.text for t in turns] == ["I'd like to book", "Sure, what service?"]
    assert [t.role for t in turns] == ["user", "assistant"]


def test_user_turn_marks_awaiting_response() -> None:
    clock = FakeClock()
    stream = FakeVoiceStream(
        [(1.0, InterpretedTurn(text="hi", role="user"))], clock=clock
    )
    mgr = VoiceStreamManager(stream, clock=clock)
    _run(mgr.start())
    _run(mgr.run())
    assert mgr.awaiting_response is True


# ---------------------------------------------------------------------------
# Response-start timing (Req 12.1)
# ---------------------------------------------------------------------------


def test_response_start_latency_measured_within_budget() -> None:
    clock = FakeClock()
    timings: list[ResponseStartTiming] = []
    # No trailing close event: the run loop ends when the iterator exhausts, so
    # ``speaking`` reflects the response-started state (a close would reset it).
    script = [
        (1.0, InterpretedTurn(text="book me in", role="user")),  # patient finishes
        (2.0, ResponseStarted(response_id="r1")),  # 1.0 s later -> 1000 ms
    ]
    stream = FakeVoiceStream(script, clock=clock)
    mgr = VoiceStreamManager(stream, on_response_start=timings.append, clock=clock)
    _run(mgr.start())
    _run(mgr.run())

    assert len(timings) == 1
    assert timings[0].latency_ms == pytest.approx(1000.0)
    assert timings[0].within_budget is True
    assert timings[0].budget_ms == 1500.0
    assert mgr.last_response_start_timing == timings[0]
    # Pending response cleared once it starts.
    assert mgr.awaiting_response is False
    assert mgr.speaking is True


def test_response_start_latency_exceeding_budget_flagged() -> None:
    clock = FakeClock()
    timings: list[ResponseStartTiming] = []
    script = [
        (0.0, InterpretedTurn(text="hello", role="user")),
        (2.0, ResponseStarted(response_id="r1")),  # 2.0 s -> 2000 ms > 1500
    ]
    stream = FakeVoiceStream(script, clock=clock)
    mgr = VoiceStreamManager(stream, on_response_start=timings.append, clock=clock)
    _run(mgr.start())
    _run(mgr.run())

    assert timings[0].latency_ms == pytest.approx(2000.0)
    assert timings[0].within_budget is False


def test_assistant_turn_does_not_start_response_timer() -> None:
    clock = FakeClock()
    timings: list[ResponseStartTiming] = []
    # Only an assistant transcript precedes the response start: no user turn
    # means no pending response, so no timing is produced.
    script = [
        (0.0, InterpretedTurn(text="one moment", role="assistant")),
        (0.5, ResponseStarted(response_id="r1")),
    ]
    stream = FakeVoiceStream(script, clock=clock)
    mgr = VoiceStreamManager(stream, on_response_start=timings.append, clock=clock)
    _run(mgr.start())
    _run(mgr.run())

    assert timings == []


# ---------------------------------------------------------------------------
# Barge-in stop (Req 12.2)
# ---------------------------------------------------------------------------


def test_barge_in_stops_playback_and_reports_timing() -> None:
    clock = FakeClock()
    timings: list[BargeInStopTiming] = []
    script = [
        (0.0, ResponseStarted(response_id="r1")),
        (1.0, BargeInDetected(reason="user_speech")),
    ]
    stream = FakeVoiceStream(script, clock=clock, stop_playback_delay=0.2)  # 200 ms
    mgr = VoiceStreamManager(stream, on_barge_in=timings.append, clock=clock)
    _run(mgr.start())
    _run(mgr.run())

    assert stream.stop_playback_calls == 1
    assert mgr.speaking is False
    assert len(timings) == 1
    assert timings[0].latency_ms == pytest.approx(200.0)
    assert timings[0].within_budget is True
    assert timings[0].budget_ms == 500.0
    assert mgr.last_barge_in_timing == timings[0]


def test_barge_in_stop_exceeding_budget_flagged() -> None:
    clock = FakeClock()
    timings: list[BargeInStopTiming] = []
    script = [(0.0, BargeInDetected())]
    stream = FakeVoiceStream(script, clock=clock, stop_playback_delay=0.75)  # 750 ms
    mgr = VoiceStreamManager(stream, on_barge_in=timings.append, clock=clock)
    _run(mgr.start())
    _run(mgr.run())

    assert timings[0].latency_ms == pytest.approx(750.0)
    assert timings[0].within_budget is False


# ---------------------------------------------------------------------------
# Audio output forwarding + barge-in suppression (Req 12.2)
# ---------------------------------------------------------------------------


def test_audio_output_forwarded_while_speaking() -> None:
    chunks: list[AudioOutput] = []
    script = [
        (0.0, ResponseStarted(response_id="r1")),
        (0.1, AudioOutput(audio="aaa")),
        (0.2, AudioOutput(audio="bbb")),
        (0.3, StreamClosed()),
    ]
    stream = FakeVoiceStream(script)
    mgr = VoiceStreamManager(stream, on_audio_output=chunks.append)
    _run(mgr.start())
    _run(mgr.run())

    assert [c.audio for c in chunks] == ["aaa", "bbb"]


def test_audio_suppressed_after_barge_in_until_next_response() -> None:
    """After a barge-in the agent goes silent; audio resumes only on a new response."""
    chunks: list[AudioOutput] = []
    script = [
        (0.0, ResponseStarted(response_id="r1")),
        (0.1, AudioOutput(audio="before-1")),
        (0.2, BargeInDetected()),
        (0.3, AudioOutput(audio="dropped-1")),  # suppressed
        (0.4, AudioOutput(audio="dropped-2")),  # suppressed
        (0.5, ResponseStarted(response_id="r2")),  # clears suppression
        (0.6, AudioOutput(audio="after-2")),
        (0.7, StreamClosed()),
    ]
    stream = FakeVoiceStream(script)
    mgr = VoiceStreamManager(stream, on_audio_output=chunks.append)
    _run(mgr.start())
    _run(mgr.run())

    assert [c.audio for c in chunks] == ["before-1", "after-2"]


# ---------------------------------------------------------------------------
# Errors + async handlers + registration
# ---------------------------------------------------------------------------


def test_error_event_dispatched_to_error_handler() -> None:
    errors: list[StreamError] = []
    script = [(0.0, StreamError(message="boom", code="RuntimeError")), (0.1, StreamClosed())]
    stream = FakeVoiceStream(script)
    mgr = VoiceStreamManager(stream, on_error=errors.append)
    _run(mgr.start())
    _run(mgr.run())

    assert len(errors) == 1
    assert errors[0].message == "boom"


def test_async_handlers_are_awaited() -> None:
    seen: list[str] = []

    async def async_turn_handler(turn: InterpretedTurn) -> None:
        await asyncio.sleep(0)
        seen.append(turn.text)

    script = [(0.0, InterpretedTurn(text="async please", role="user")), (0.1, StreamClosed())]
    mgr = VoiceStreamManager(FakeVoiceStream(script))
    mgr.add_interpreted_turn_handler(async_turn_handler)
    _run(mgr.start())
    _run(mgr.run())

    assert seen == ["async please"]


def test_multiple_handlers_all_invoked() -> None:
    a: list[Any] = []
    b: list[Any] = []
    script = [(0.0, InterpretedTurn(text="hi", role="user")), (0.1, StreamClosed())]
    mgr = VoiceStreamManager(FakeVoiceStream(script))
    mgr.add_interpreted_turn_handler(a.append)
    mgr.add_interpreted_turn_handler(b.append)
    _run(mgr.start())
    _run(mgr.run())

    assert len(a) == 1 and len(b) == 1


def test_connected_event_needs_no_handler() -> None:
    """A connection-start event is tolerated with no handlers registered."""
    script = [(0.0, StreamConnected(connection_id="c1", model="nova-sonic")), (0.1, StreamClosed())]
    mgr = VoiceStreamManager(FakeVoiceStream(script))
    _run(mgr.start())
    _run(mgr.run())  # must not raise


# ---------------------------------------------------------------------------
# NovaSonicVoiceStream adapter — event translation
# ---------------------------------------------------------------------------


def test_adapter_translates_connection_start() -> None:
    ev = NovaSonicVoiceStream._translate(
        {"type": "bidi_connection_start", "connection_id": "c1", "model": "nova"}
    )
    assert isinstance(ev, StreamConnected)
    assert ev.connection_id == "c1"
    assert ev.model == "nova"


def test_adapter_translates_response_start() -> None:
    ev = NovaSonicVoiceStream._translate({"type": "bidi_response_start", "response_id": "r1"})
    assert isinstance(ev, ResponseStarted)
    assert ev.response_id == "r1"


def test_adapter_translates_final_transcript_to_interpreted_turn() -> None:
    ev = NovaSonicVoiceStream._translate(
        {"type": "bidi_transcript_stream", "text": "hello", "role": "user", "is_final": True}
    )
    assert isinstance(ev, InterpretedTurn)
    assert ev.text == "hello"
    assert ev.role == "user"


def test_adapter_drops_partial_transcript() -> None:
    ev = NovaSonicVoiceStream._translate(
        {"type": "bidi_transcript_stream", "text": "hel", "role": "user", "is_final": False}
    )
    assert ev is None


def test_adapter_translates_assistant_transcript_role() -> None:
    ev = NovaSonicVoiceStream._translate(
        {"type": "bidi_transcript_stream", "text": "hi there", "role": "assistant", "is_final": True}
    )
    assert isinstance(ev, InterpretedTurn)
    assert ev.role == "assistant"


def test_adapter_translates_audio_stream() -> None:
    ev = NovaSonicVoiceStream._translate(
        {
            "type": "bidi_audio_stream",
            "audio": "Zm9v",
            "format": "pcm",
            "sample_rate": 24000,
            "channels": 1,
        }
    )
    assert isinstance(ev, AudioOutput)
    assert ev.audio == "Zm9v"
    assert ev.sample_rate == 24000


def test_adapter_translates_interruption_to_barge_in() -> None:
    ev = NovaSonicVoiceStream._translate({"type": "bidi_interruption", "reason": "user_speech"})
    assert isinstance(ev, BargeInDetected)
    assert ev.reason == "user_speech"


def test_adapter_translates_response_complete() -> None:
    ev = NovaSonicVoiceStream._translate(
        {"type": "bidi_response_complete", "response_id": "r1", "stop_reason": "complete"}
    )
    assert isinstance(ev, ResponseCompleted)
    assert ev.stop_reason == "complete"


def test_adapter_translates_connection_close() -> None:
    ev = NovaSonicVoiceStream._translate({"type": "bidi_connection_close", "reason": "timeout"})
    assert isinstance(ev, StreamClosed)
    assert ev.reason == "timeout"


def test_adapter_translates_error() -> None:
    ev = NovaSonicVoiceStream._translate(
        {"type": "bidi_error", "message": "kaboom", "code": "ValueError"}
    )
    assert isinstance(ev, StreamError)
    assert ev.message == "kaboom"
    assert ev.code == "ValueError"


def test_adapter_ignores_unmapped_events() -> None:
    assert NovaSonicVoiceStream._translate({"type": "bidi_usage", "inputTokens": 1}) is None
    assert NovaSonicVoiceStream._translate({"type": "something_else"}) is None


def test_adapter_requires_start_before_use() -> None:
    adapter = NovaSonicVoiceStream()
    with pytest.raises(RuntimeError):
        _run(adapter.send_text("hi"))


def test_adapter_stop_playback_is_fast_noop() -> None:
    """The documented best-effort barge-in stop does no blocking work."""
    adapter = NovaSonicVoiceStream()
    _run(adapter.stop_playback())  # no agent needed, must not raise


def test_adapter_close_without_agent_is_safe() -> None:
    adapter = NovaSonicVoiceStream()
    _run(adapter.close())  # never started; no-op
