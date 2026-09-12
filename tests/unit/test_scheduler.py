"""Unit tests for ``AnalysisScheduler`` (task 10.3, Req 13.1).

Deterministic tests driven by a controllable fake clock and a counting run
callback — no real wall-clock sleeping. They cover the recurring-interval
guarantee (interval always <= 24 h, via clamp or strict rejection), the
``tick`` due/not-due semantics, next-run computation, manual ``trigger``, and
multi-cycle recurrence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from clinic_front_desk.intelligence.scheduler import (
    DEFAULT_INTERVAL,
    MAX_INTERVAL,
    AnalysisScheduler,
)

BASE = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """A manually advanced clock for deterministic scheduling tests."""

    def __init__(self, start: datetime = BASE) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class Counter:
    """A zero-arg run callback that records how many times it fired."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


# --- interval bounds (Req 13.1) --------------------------------------------


def test_max_interval_is_24_hours() -> None:
    assert MAX_INTERVAL == timedelta(hours=24)
    assert DEFAULT_INTERVAL <= MAX_INTERVAL


def test_interval_within_bound_is_kept() -> None:
    clock = FakeClock()
    sched = AnalysisScheduler(Counter(), timedelta(hours=6), clock=clock)
    assert sched.interval == timedelta(hours=6)
    assert sched.interval <= MAX_INTERVAL


def test_over_long_interval_is_clamped_by_default() -> None:
    clock = FakeClock()
    sched = AnalysisScheduler(Counter(), timedelta(hours=48), clock=clock)
    # Req 13.1: the effective interval never exceeds 24 h.
    assert sched.interval == MAX_INTERVAL


def test_strict_mode_rejects_over_long_interval() -> None:
    with pytest.raises(ValueError):
        AnalysisScheduler(
            Counter(), timedelta(hours=25), clock=FakeClock(), strict=True
        )


def test_non_positive_interval_rejected() -> None:
    with pytest.raises(ValueError):
        AnalysisScheduler(Counter(), timedelta(0), clock=FakeClock())
    with pytest.raises(ValueError):
        AnalysisScheduler(Counter(), timedelta(hours=-1), clock=FakeClock())


def test_default_interval_is_the_maximum() -> None:
    sched = AnalysisScheduler(Counter(), clock=FakeClock())
    assert sched.interval == MAX_INTERVAL


# --- scheduling / tick semantics -------------------------------------------


def test_first_run_scheduled_one_interval_out_by_default() -> None:
    clock = FakeClock()
    sched = AnalysisScheduler(Counter(), timedelta(hours=6), clock=clock)
    assert sched.next_run_at == BASE + timedelta(hours=6)
    assert sched.is_due() is False


def test_tick_does_not_run_before_due() -> None:
    clock = FakeClock()
    counter = Counter()
    sched = AnalysisScheduler(counter, timedelta(hours=6), clock=clock)

    clock.advance(timedelta(hours=5, minutes=59))
    assert sched.tick() is False
    assert counter.calls == 0


def test_tick_runs_when_due_and_reschedules() -> None:
    clock = FakeClock()
    counter = Counter()
    sched = AnalysisScheduler(counter, timedelta(hours=6), clock=clock)

    clock.advance(timedelta(hours=6))
    assert sched.tick() is True
    assert counter.calls == 1
    # Next run is exactly one interval from the run time.
    assert sched.next_run_at == BASE + timedelta(hours=12)


def test_start_at_controls_first_run() -> None:
    clock = FakeClock()
    counter = Counter()
    first = BASE + timedelta(minutes=30)
    sched = AnalysisScheduler(
        counter, timedelta(hours=1), clock=clock, start_at=first
    )
    assert sched.next_run_at == first
    assert sched.tick() is False

    clock.advance(timedelta(minutes=30))
    assert sched.tick() is True
    assert counter.calls == 1


def test_recurring_fires_once_per_interval() -> None:
    clock = FakeClock()
    counter = Counter()
    sched = AnalysisScheduler(counter, timedelta(hours=24), clock=clock)

    # Advance across three full days, ticking each hour.
    for _ in range(72):
        clock.advance(timedelta(hours=1))
        sched.tick()

    assert counter.calls == 3
    assert sched.run_count == 3


def test_single_tick_runs_at_most_once_even_when_overdue() -> None:
    clock = FakeClock()
    counter = Counter()
    sched = AnalysisScheduler(counter, timedelta(hours=6), clock=clock)

    clock.advance(timedelta(hours=30))  # far past due
    assert sched.tick() is True
    assert counter.calls == 1


def test_trigger_runs_immediately_and_reschedules() -> None:
    clock = FakeClock()
    counter = Counter()
    sched = AnalysisScheduler(counter, timedelta(hours=6), clock=clock)

    sched.trigger()
    assert counter.calls == 1
    assert sched.next_run_at == BASE + timedelta(hours=6)


def test_is_due_uses_injected_now() -> None:
    clock = FakeClock()
    sched = AnalysisScheduler(Counter(), timedelta(hours=6), clock=clock)
    assert sched.is_due(now=BASE + timedelta(hours=6)) is True
    assert sched.is_due(now=BASE + timedelta(hours=5)) is False


def test_raising_callback_leaves_schedule_unchanged() -> None:
    clock = FakeClock()

    def boom() -> None:
        raise RuntimeError("run failed")

    sched = AnalysisScheduler(boom, timedelta(hours=6), clock=clock)
    original_next = sched.next_run_at
    clock.advance(timedelta(hours=6))

    with pytest.raises(RuntimeError):
        sched.tick()
    # Next-run time is only advanced after a successful run.
    assert sched.next_run_at == original_next
    assert sched.run_count == 0
