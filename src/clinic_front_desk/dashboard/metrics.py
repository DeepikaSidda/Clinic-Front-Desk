"""Impact-metrics service for the Dashboard (task 12.6, Req 15.3).

The dashboard's ``ImpactMetricsStrip`` component (design "Dashboard") shows three
value-of-the-agent metrics computed over a doctor-selectable reporting period of
7, 30, or 90 days:

- **front-desk hours saved** — an estimate of the human front-desk time the
  agent absorbed by handling calls end-to-end;
- **appointments recovered from the waitlist** — the count of appointments the
  agent created by filling an open slot from the waitlist (gap-fill);
- **no-show rate** — the fraction of expected appointments the patient did not
  attend, shown together with a *trend*: the change in that rate relative to the
  immediately preceding period of equal length (Req 15.3).

Design intent (task 12.6): the computations are **pure and deterministic** given
the input records and an injectable reference instant ``now``. There is no I/O,
no hidden clock read, and no mutation of the inputs — the same records and the
same ``now`` always produce the same :class:`ImpactMetrics`. This is exactly what
Property 21 (task 12.7) exercises: each computed value equals a reference
computation over the window, and the no-show-rate trend equals the current-period
rate minus the preceding-period rate.

Documented formulas
--------------------

**Reporting periods.** Given a reference instant ``now`` and a window of ``W``
days, two equal-length, non-overlapping periods are defined, both anchored at
``now`` and measured backward:

- *current* period ``(now - W, now]`` — start-exclusive, end-inclusive;
- *preceding* period ``(now - 2W, now - W]``.

The boundary instant ``now - W`` belongs to the *preceding* period, so a record
is never counted in both. A record is attributed to a period by a single
timestamp (below); records outside both periods are ignored.

**Front-desk hours saved** = ``handled_calls * AVERAGE_CALL_HANDLING_MINUTES / 60``.
A *handled* call is a :class:`~clinic_front_desk.models.CallSession` that the
agent finalized with an outcome other than ``interrupted`` — i.e. the agent
carried the interaction to a conclusion the human front desk would otherwise have
staffed. Un-finalized sessions (no outcome) and ``interrupted`` sessions (the
voice layer dropped, Req 12.7) are not credited. A session is attributed to a
period by its ``started_at`` instant. ``AVERAGE_CALL_HANDLING_MINUTES`` is a
fixed modeling constant (see :data:`AVERAGE_CALL_HANDLING_MINUTES`).

**Appointments recovered from the waitlist** = the number of *approved gap-fill
Decisions* whose action executed within the period. Each approved
``gap_fill`` :class:`~clinic_front_desk.models.Decision` corresponds to exactly
one appointment booked from the waitlist by ``fill_gap_from_waitlist`` (Req 8.2,
8.3), so counting those decisions counts the recovered appointments. A decision
is attributed to a period by its ``resolved_at`` instant (when the fill ran); a
decision that is not ``approved``, not ``gap_fill``, or has no ``resolved_at`` is
not counted.

**No-show rate** = ``no_show_appointments / attended_appointments`` where the
denominator is the appointments in the period that reached a known attendance
outcome — status ``completed`` or ``no_show`` — and the numerator is those with
status ``no_show``. An appointment is attributed to a period by its scheduled
``date``. When the denominator is zero the rate is ``0.0`` (no expected visits →
no no-shows). **No-show-rate trend** = current-period rate minus preceding-period
rate; it is positive when no-shows are getting worse and negative when improving.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from clinic_front_desk.models import (
    Appointment,
    AppointmentStatus,
    CallOutcome,
    CallSession,
    Decision,
    DecisionKind,
    DecisionStatus,
)

#: The reporting windows the doctor may select, in days (Req 15.3).
SUPPORTED_WINDOW_DAYS: Final[tuple[int, ...]] = (7, 30, 90)

#: Modeling constant: the average human front-desk handling time credited per
#: call the agent handles end-to-end, in minutes. "Hours saved" is a deliberate
#: estimate (there is no ground-truth per-call human time), so this is a single
#: documented constant rather than a per-call measurement; changing it rescales
#: the hours-saved metric linearly.
AVERAGE_CALL_HANDLING_MINUTES: Final[float] = 8.0

#: Call outcomes that count as *handled* for hours-saved: every finalized outcome
#: except ``interrupted`` (a dropped voice layer, Req 12.7, is not a completed
#: interaction). Un-finalized sessions (outcome ``None``) are excluded separately.
_HANDLED_OUTCOMES: Final[frozenset[CallOutcome]] = frozenset(
    outcome for outcome in CallOutcome if outcome is not CallOutcome.INTERRUPTED
)

#: Appointment statuses that form the no-show-rate denominator: appointments that
#: were expected to be attended and have a known attendance outcome.
_ATTENDED_STATUSES: Final[frozenset[AppointmentStatus]] = frozenset(
    {AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW}
)


@dataclass(frozen=True)
class Period:
    """A half-open reporting period ``(start, end]`` (start-exclusive, end-inclusive).

    Membership is decided by :meth:`contains`; the exclusive start guarantees two
    adjacent equal-length periods (current and preceding) never both claim the
    boundary instant.
    """

    start: datetime
    end: datetime

    def contains(self, instant: datetime) -> bool:
        """Return whether ``instant`` lies in ``(start, end]``."""
        return self.start < instant <= self.end


@dataclass(frozen=True)
class ImpactMetrics:
    """The computed impact-metrics strip for one reporting window (Req 15.3).

    Attributes:
        window_days: The selected reporting window (7, 30, or 90).
        front_desk_hours_saved: Estimated human front-desk hours the agent saved
            over the current period.
        waitlist_recovered_count: Appointments recovered from the waitlist
            (approved gap-fill Decisions) over the current period.
        no_show_rate: The current-period no-show rate in ``[0.0, 1.0]``.
        no_show_rate_trend: Current-period rate minus preceding-period rate;
            positive means worsening, negative means improving.
        handled_call_count: Handled calls credited toward hours saved (current
            period), exposed for transparency.
        no_show_count: No-show appointments in the current period (numerator).
        attended_appointment_count: Appointments with a known attendance outcome
            in the current period (no-show-rate denominator).
        preceding_no_show_rate: The preceding-period no-show rate, from which the
            trend is derived.
    """

    window_days: int
    front_desk_hours_saved: float
    waitlist_recovered_count: int
    no_show_rate: float
    no_show_rate_trend: float
    handled_call_count: int
    no_show_count: int
    attended_appointment_count: int
    preceding_no_show_rate: float


def _to_utc(value: datetime) -> datetime:
    """Normalize a datetime to timezone-aware UTC (assume UTC if naive)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_instant(value: str) -> datetime | None:
    """Parse an ISO date or date-time string to an aware UTC datetime.

    Accepts both ``"2025-06-01"`` (interpreted as midnight UTC) and
    ``"2025-06-01T14:30:00Z"``. Returns ``None`` for an empty or unparseable
    value so callers can skip records lacking a usable timestamp.
    """
    if not value:
        return None
    try:
        return _to_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def reporting_periods(now: datetime, window_days: int) -> tuple[Period, Period]:
    """Return the ``(current, preceding)`` periods for ``window_days`` at ``now``.

    The current period is ``(now - W, now]`` and the preceding period is the
    immediately preceding equal-length window ``(now - 2W, now - W]`` (Req 15.3).
    """
    now = _to_utc(now)
    width = timedelta(days=window_days)
    current = Period(start=now - width, end=now)
    preceding = Period(start=now - 2 * width, end=now - width)
    return current, preceding


def _validate_window(window_days: int) -> int:
    """Validate ``window_days`` is a supported reporting window (Req 15.3)."""
    if window_days not in SUPPORTED_WINDOW_DAYS:
        raise ValueError(
            f"window_days must be one of {SUPPORTED_WINDOW_DAYS}, got {window_days!r}"
        )
    return window_days


def front_desk_hours_saved(
    sessions: Iterable[CallSession], period: Period
) -> tuple[float, int]:
    """Estimate front-desk hours saved over ``period``.

    Counts handled calls (finalized, non-``interrupted``) attributed to the
    period by ``started_at`` and multiplies by
    :data:`AVERAGE_CALL_HANDLING_MINUTES`. Returns ``(hours_saved, handled_count)``.
    """
    handled = 0
    for session in sessions:
        if session.outcome is None or session.outcome not in _HANDLED_OUTCOMES:
            continue
        started = _parse_instant(session.started_at)
        if started is None or not period.contains(started):
            continue
        handled += 1
    hours = handled * AVERAGE_CALL_HANDLING_MINUTES / 60.0
    return hours, handled


def waitlist_recovered_count(decisions: Iterable[Decision], period: Period) -> int:
    """Count appointments recovered from the waitlist over ``period``.

    An approved ``gap_fill`` :class:`~clinic_front_desk.models.Decision`
    corresponds to one appointment booked from the waitlist; it is attributed to
    the period by its ``resolved_at`` instant.
    """
    count = 0
    for decision in decisions:
        if decision.kind is not DecisionKind.GAP_FILL:
            continue
        if decision.status is not DecisionStatus.APPROVED:
            continue
        if decision.resolved_at is None:
            continue
        resolved = _parse_instant(decision.resolved_at)
        if resolved is None or not period.contains(resolved):
            continue
        count += 1
    return count


def no_show_rate(
    appointments: Iterable[Appointment], period: Period
) -> tuple[float, int, int]:
    """Compute the no-show rate over ``period``.

    Denominator is appointments with a known attendance outcome (``completed`` or
    ``no_show``) attributed to the period by scheduled ``date``; numerator is
    those with status ``no_show``. Returns ``(rate, no_show_count, attended_count)``;
    the rate is ``0.0`` when the denominator is zero.
    """
    no_shows = 0
    attended = 0
    for appointment in appointments:
        if appointment.status not in _ATTENDED_STATUSES:
            continue
        scheduled = _parse_instant(appointment.date)
        if scheduled is None or not period.contains(scheduled):
            continue
        attended += 1
        if appointment.status is AppointmentStatus.NO_SHOW:
            no_shows += 1
    rate = (no_shows / attended) if attended else 0.0
    return rate, no_shows, attended


def compute_impact_metrics(
    *,
    appointments: Iterable[Appointment],
    call_sessions: Iterable[CallSession],
    recovered_decisions: Iterable[Decision],
    window_days: int,
    now: datetime | None = None,
) -> ImpactMetrics:
    """Compute the impact-metrics strip for one reporting window (Req 15.3).

    Pure and deterministic: given the same records, ``window_days``, and ``now``
    it always returns the same :class:`ImpactMetrics`, with no I/O or input
    mutation. See the module docstring for the exact formulas.

    Args:
        appointments: Appointment records to compute the no-show rate from.
        call_sessions: Call-session records to compute hours saved from.
        recovered_decisions: Decision records; approved ``gap_fill`` decisions
            are counted as waitlist-recovered appointments.
        window_days: The reporting window in days; must be 7, 30, or 90.
        now: The reference instant for the current/preceding periods. Defaults to
            the current UTC time; inject a fixed value for deterministic results.

    Returns:
        The computed :class:`ImpactMetrics`.

    Raises:
        ValueError: If ``window_days`` is not one of :data:`SUPPORTED_WINDOW_DAYS`.
    """
    _validate_window(window_days)
    reference = _to_utc(now) if now is not None else datetime.now(UTC)
    current, preceding = reporting_periods(reference, window_days)

    # Materialize once so each metric can iterate independently without consuming
    # a one-shot iterator.
    appointment_list = list(appointments)
    session_list = list(call_sessions)
    decision_list = list(recovered_decisions)

    hours_saved, handled_count = front_desk_hours_saved(session_list, current)
    recovered = waitlist_recovered_count(decision_list, current)
    current_rate, no_show_count, attended_count = no_show_rate(appointment_list, current)
    preceding_rate, _, _ = no_show_rate(appointment_list, preceding)

    return ImpactMetrics(
        window_days=window_days,
        front_desk_hours_saved=hours_saved,
        waitlist_recovered_count=recovered,
        no_show_rate=current_rate,
        no_show_rate_trend=current_rate - preceding_rate,
        handled_call_count=handled_count,
        no_show_count=no_show_count,
        attended_appointment_count=attended_count,
        preceding_no_show_rate=preceding_rate,
    )


__all__ = [
    "SUPPORTED_WINDOW_DAYS",
    "AVERAGE_CALL_HANDLING_MINUTES",
    "Period",
    "ImpactMetrics",
    "reporting_periods",
    "front_desk_hours_saved",
    "waitlist_recovered_count",
    "no_show_rate",
    "compute_impact_metrics",
]
