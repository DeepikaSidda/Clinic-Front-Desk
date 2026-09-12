"""Smoke test: the ``AnalysisScheduler`` interval is <= 24 h (task 10.7, Req 13.1).

Practice_Intelligence must "start an analysis run on a recurring interval not
exceeding 24 hours" (Req 13.1). This is one-time configuration that does not vary
meaningfully with input, so per the design's Testing Strategy it is covered by a
single-execution smoke test rather than a property.

The check exercises the scheduler's guarantee directly: the documented maximum
is 24 h, the default cadence is within it, an in-bound interval is kept as-is,
and an over-long interval is clamped down to the 24 h maximum (so the effective
interval is *always* <= 24 h).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from clinic_front_desk.intelligence.scheduler import (
    DEFAULT_INTERVAL,
    MAX_INTERVAL,
    AnalysisScheduler,
)

_ONE_DAY = timedelta(hours=24)


class _FixedClock:
    def __init__(self) -> None:
        self.now = datetime(2024, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


@pytest.mark.smoke
def test_scheduler_interval_is_at_most_24_hours() -> None:
    # The documented maximum and the default cadence honour the <= 24 h bound.
    assert MAX_INTERVAL == _ONE_DAY
    assert DEFAULT_INTERVAL <= _ONE_DAY

    # A scheduler built with the default cadence keeps the effective interval
    # within the bound (Req 13.1).
    default_sched = AnalysisScheduler(lambda: None, clock=_FixedClock())
    assert default_sched.interval <= _ONE_DAY

    # An in-bound interval is kept as configured.
    six_hourly = AnalysisScheduler(
        lambda: None, interval=timedelta(hours=6), clock=_FixedClock()
    )
    assert six_hourly.interval == timedelta(hours=6)
    assert six_hourly.interval <= _ONE_DAY

    # An over-long interval is clamped down to the 24 h maximum, so the effective
    # recurring interval never exceeds 24 h.
    clamped = AnalysisScheduler(
        lambda: None, interval=timedelta(hours=48), clock=_FixedClock()
    )
    assert clamped.interval == _ONE_DAY
    assert clamped.interval <= _ONE_DAY
