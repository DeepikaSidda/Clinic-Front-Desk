"""Latency tests for the Voice_Front_Desk voice stream (tasks 9.3, 9.4).

These measure the two timing characteristics of the Nova Sonic voice pipeline
against a *fake* bidirectional stream (no real Bedrock connection):

- **task 9.3 (Req 12.1)** — after the patient finishes speaking, an audible
  response begins within 1.5 s (``RESPONSE_START_BUDGET_MS``).
- **task 9.4 (Req 12.2)** — while speaking, a detected barge-in stops playback
  within 500 ms (``BARGE_IN_STOP_BUDGET_MS``).

They drive :class:`~clinic_front_desk.voice.stream.VoiceStreamManager` with a
scripted :class:`FakeVoiceStream` and a controllable :class:`FakeClock` (the
same pattern as ``tests/unit/test_voice_stream.py``, defined locally here so
this file stands alone) and assert the manager's measured
``last_response_start_timing`` / ``last_barge_in_timing`` report ``within_budget``.

No ``pytest-asyncio`` is available, so coroutines are driven with ``asyncio.run``
via :func:`_run`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from clinic_front_desk.voice.stream import (
    BargeInDetected,
    InterpretedTurn,
    ResponseStarted,
    VoiceStreamManager,
)

pytestmark = pytest.mark.latency


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

    Yields ``(clock_time, event)`` pairs; before each event the shared clock is
    set to ``clock_time`` so timing intervals are fully controlled.
    ``stop_playback`` advances the clock by ``stop_playback_delay`` to simulate
    the cost of stopping in-progress audio (used for the barge-in latency test).
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
        return None

    async def send_text(self, text: str) -> None:
        return None

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
# Task 9.3 — response-start latency <= 1.5 s (Req 12.1)
# ---------------------------------------------------------------------------


def test_response_start_within_budget() -> None:
    """Response begins 1.0 s after the patient finishes speaking -> within 1.5 s (Req 12.1)."""
    clock = FakeClock()
    script = [
        (0.0, InterpretedTurn(text="book me in", role="user")),  # patient finishes
        (1.0, ResponseStarted(response_id="r1")),  # audible response 1.0 s later
    ]
    stream = FakeVoiceStream(script, clock=clock)
    manager = VoiceStreamManager(stream, clock=clock)

    _run(manager.start())
    _run(manager.run())

    timing = manager.last_response_start_timing
    assert timing is not None
    assert timing.budget_ms == VoiceStreamManager.RESPONSE_START_BUDGET_MS == 1500.0
    assert timing.latency_ms == pytest.approx(1000.0)
    assert timing.latency_ms <= 1500.0
    assert timing.within_budget is True


# ---------------------------------------------------------------------------
# Task 9.4 — barge-in stop latency <= 500 ms (Req 12.2)
# ---------------------------------------------------------------------------


def test_barge_in_stop_within_budget() -> None:
    """A detected barge-in stops playback in 200 ms -> within the 500 ms budget (Req 12.2)."""
    clock = FakeClock()
    script = [
        (0.0, ResponseStarted(response_id="r1")),  # agent is speaking
        (1.0, BargeInDetected(reason="user_speech")),  # patient interrupts
    ]
    stream = FakeVoiceStream(script, clock=clock, stop_playback_delay=0.2)  # 200 ms
    manager = VoiceStreamManager(stream, clock=clock)

    _run(manager.start())
    _run(manager.run())

    assert stream.stop_playback_calls == 1
    timing = manager.last_barge_in_timing
    assert timing is not None
    assert timing.budget_ms == VoiceStreamManager.BARGE_IN_STOP_BUDGET_MS == 500.0
    assert timing.latency_ms == pytest.approx(200.0)
    assert timing.latency_ms <= 500.0
    assert timing.within_budget is True
