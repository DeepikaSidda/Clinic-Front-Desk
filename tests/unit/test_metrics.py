"""Unit tests for the impact-metrics service (task 12.6, Req 15.3).

Covers :mod:`clinic_front_desk.dashboard.metrics`:

- Front-desk hours saved counts finalized, non-interrupted calls in the current
  period and applies the average-handling-time constant.
- Appointments recovered from the waitlist counts approved gap-fill Decisions
  resolved within the current period.
- No-show rate is no-shows over attended appointments, with a zero-denominator
  guard, attributed to the period by scheduled date.
- The no-show-rate trend is the current-period rate minus the preceding-period
  rate.
- Period boundaries are half-open ``(start, end]`` so adjacent periods never
  double-count, and only supported windows (7, 30, 90) are accepted.

The property test for the computation and trend lives in task 12.7.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from clinic_front_desk.dashboard.metrics import (
    AVERAGE_CALL_HANDLING_MINUTES,
    SUPPORTED_WINDOW_DAYS,
    ImpactMetrics,
    compute_impact_metrics,
    reporting_periods,
)
from clinic_front_desk.models import (
    Appointment,
    AppointmentStatus,
    CallOutcome,
    CallSession,
    Decision,
    DecisionKind,
    DecisionStatus,
)

NOW = datetime(2025, 6, 30, 12, 0, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _session(id: str, outcome: CallOutcome | None, started_at: datetime) -> CallSession:
    return CallSession(id=id, started_at=_iso(started_at), outcome=outcome)


def _appointment(id: str, status: AppointmentStatus, date: datetime) -> Appointment:
    return Appointment(
        id=id,
        provider_id="prov-1",
        patient_id="pat-1",
        service="hearing_test",
        slot_id=f"slot-{id}",
        date=date.date().isoformat(),
        time="09:00",
        status=status,
    )


def _gap_fill_decision(
    id: str,
    status: DecisionStatus,
    resolved_at: datetime | None,
    *,
    kind: DecisionKind = DecisionKind.GAP_FILL,
) -> Decision:
    return Decision(
        id=id,
        kind=kind,
        finding_key=f"gap_fill#{id}",
        summary="fill open slot",
        recommended_action="contact waitlisted patient",
        status=status,
        resolved_at=_iso(resolved_at) if resolved_at is not None else None,
    )


# ---------------------------------------------------------------------------
# Reporting periods
# ---------------------------------------------------------------------------


def test_reporting_periods_are_adjacent_equal_length_windows() -> None:
    current, preceding = reporting_periods(NOW, 30)
    assert current.end == NOW
    assert current.start == NOW - timedelta(days=30)
    assert preceding.end == current.start
    assert preceding.start == NOW - timedelta(days=60)


def test_period_boundary_belongs_to_preceding_not_current() -> None:
    # The shared boundary instant now-W is end-inclusive for preceding and
    # start-exclusive for current, so it is counted once (in preceding).
    current, preceding = reporting_periods(NOW, 7)
    boundary = NOW - timedelta(days=7)
    assert preceding.contains(boundary)
    assert not current.contains(boundary)


# ---------------------------------------------------------------------------
# Front-desk hours saved
# ---------------------------------------------------------------------------


def test_hours_saved_counts_finalized_non_interrupted_calls() -> None:
    sessions = [
        _session("s1", CallOutcome.BOOKED, NOW - timedelta(days=1)),
        _session("s2", CallOutcome.NO_ACTION, NOW - timedelta(days=2)),
        _session("s3", CallOutcome.INTERRUPTED, NOW - timedelta(days=1)),  # excluded
        _session("s4", None, NOW - timedelta(days=1)),  # un-finalized, excluded
        _session("s5", CallOutcome.BOOKED, NOW - timedelta(days=40)),  # out of window
    ]
    metrics = compute_impact_metrics(
        appointments=[],
        call_sessions=sessions,
        recovered_decisions=[],
        window_days=30,
        now=NOW,
    )
    assert metrics.handled_call_count == 2
    assert metrics.front_desk_hours_saved == pytest.approx(
        2 * AVERAGE_CALL_HANDLING_MINUTES / 60.0
    )


# ---------------------------------------------------------------------------
# Waitlist-recovered appointments
# ---------------------------------------------------------------------------


def test_waitlist_recovered_counts_only_approved_gap_fill_in_window() -> None:
    decisions = [
        _gap_fill_decision("d1", DecisionStatus.APPROVED, NOW - timedelta(days=3)),
        _gap_fill_decision("d2", DecisionStatus.APPROVED, NOW - timedelta(days=5)),
        _gap_fill_decision("d3", DecisionStatus.DISMISSED, NOW - timedelta(days=2)),
        _gap_fill_decision("d4", DecisionStatus.OPEN, None),
        _gap_fill_decision("d5", DecisionStatus.APPROVED, NOW - timedelta(days=40)),
        _gap_fill_decision(
            "d6",
            DecisionStatus.APPROVED,
            NOW - timedelta(days=1),
            kind=DecisionKind.NO_SHOW_TREND,  # not a gap-fill, excluded
        ),
    ]
    metrics = compute_impact_metrics(
        appointments=[],
        call_sessions=[],
        recovered_decisions=decisions,
        window_days=30,
        now=NOW,
    )
    assert metrics.waitlist_recovered_count == 2


# ---------------------------------------------------------------------------
# No-show rate and trend
# ---------------------------------------------------------------------------


def test_no_show_rate_is_no_shows_over_attended() -> None:
    appointments = [
        _appointment("a1", AppointmentStatus.NO_SHOW, NOW - timedelta(days=1)),
        _appointment("a2", AppointmentStatus.NO_SHOW, NOW - timedelta(days=2)),
        _appointment("a3", AppointmentStatus.COMPLETED, NOW - timedelta(days=3)),
        _appointment("a4", AppointmentStatus.COMPLETED, NOW - timedelta(days=4)),
        # booked/cancelled are not attendance outcomes -> excluded from denominator
        _appointment("a5", AppointmentStatus.BOOKED, NOW - timedelta(days=1)),
        _appointment("a6", AppointmentStatus.CANCELLED, NOW - timedelta(days=1)),
    ]
    metrics = compute_impact_metrics(
        appointments=appointments,
        call_sessions=[],
        recovered_decisions=[],
        window_days=30,
        now=NOW,
    )
    assert metrics.no_show_count == 2
    assert metrics.attended_appointment_count == 4
    assert metrics.no_show_rate == pytest.approx(0.5)


def test_no_show_rate_zero_when_no_attended_appointments() -> None:
    metrics = compute_impact_metrics(
        appointments=[_appointment("a1", AppointmentStatus.BOOKED, NOW - timedelta(days=1))],
        call_sessions=[],
        recovered_decisions=[],
        window_days=30,
        now=NOW,
    )
    assert metrics.attended_appointment_count == 0
    assert metrics.no_show_rate == 0.0
    assert metrics.no_show_rate_trend == 0.0


def test_no_show_trend_is_current_minus_preceding() -> None:
    appointments = [
        # current period (last 7 days): 1 of 2 -> 0.5
        _appointment("c1", AppointmentStatus.NO_SHOW, NOW - timedelta(days=1)),
        _appointment("c2", AppointmentStatus.COMPLETED, NOW - timedelta(days=2)),
        # preceding period (days 7..14 ago): 1 of 4 -> 0.25
        _appointment("p1", AppointmentStatus.NO_SHOW, NOW - timedelta(days=9)),
        _appointment("p2", AppointmentStatus.COMPLETED, NOW - timedelta(days=9)),
        _appointment("p3", AppointmentStatus.COMPLETED, NOW - timedelta(days=10)),
        _appointment("p4", AppointmentStatus.COMPLETED, NOW - timedelta(days=11)),
    ]
    metrics = compute_impact_metrics(
        appointments=appointments,
        call_sessions=[],
        recovered_decisions=[],
        window_days=7,
        now=NOW,
    )
    assert metrics.no_show_rate == pytest.approx(0.5)
    assert metrics.preceding_no_show_rate == pytest.approx(0.25)
    assert metrics.no_show_rate_trend == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# Window validation and determinism
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("window", SUPPORTED_WINDOW_DAYS)
def test_supported_windows_are_accepted(window: int) -> None:
    metrics = compute_impact_metrics(
        appointments=[],
        call_sessions=[],
        recovered_decisions=[],
        window_days=window,
        now=NOW,
    )
    assert isinstance(metrics, ImpactMetrics)
    assert metrics.window_days == window


@pytest.mark.parametrize("window", [0, 1, 14, 60, 365])
def test_unsupported_window_raises(window: int) -> None:
    with pytest.raises(ValueError):
        compute_impact_metrics(
            appointments=[],
            call_sessions=[],
            recovered_decisions=[],
            window_days=window,
            now=NOW,
        )


def test_computation_is_deterministic_for_same_inputs() -> None:
    sessions = [_session("s1", CallOutcome.BOOKED, NOW - timedelta(days=1))]
    first = compute_impact_metrics(
        appointments=[],
        call_sessions=sessions,
        recovered_decisions=[],
        window_days=30,
        now=NOW,
    )
    second = compute_impact_metrics(
        appointments=[],
        call_sessions=sessions,
        recovered_decisions=[],
        window_days=30,
        now=NOW,
    )
    assert first == second
