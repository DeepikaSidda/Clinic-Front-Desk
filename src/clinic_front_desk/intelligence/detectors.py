"""``PatternDetectors`` — pure pattern-analysis functions (task 10.1, Req 13.2, 8.1).

Each detector reads over a :class:`PatternInput` snapshot assembled from the
Data_Layer (Appointment / Slot / Waitlist / Call_Session data) and returns zero
or more :class:`~clinic_front_desk.models.Finding` objects. A ``Finding`` carries:

- a **stable ``key``** built exactly as the design's *findingKey construction*
  table prescribes, so the downstream ``DecisionSynthesizer`` (task 10.2) can
  dedupe deterministically against open Decisions (Req 13.3);
- an accurate **``supporting_record_count``** — the number of records that back
  the observation — which the synthesizer's ≥ 5 gate reads (Req 13.6); and
- an **``actionable``** flag (Req 13.4).

The detectors deliberately do **not** apply the ≥ 5-record support gate, the
actionability drop, or the open-Decision dedupe — those generation gates belong
to the ``DecisionSynthesizer`` (task 10.2). A detector emits a finding whenever
its qualitative pattern is present and reports the true supporting count; the
synthesizer decides whether that finding becomes a Decision.

findingKey construction (design):

===========================  ===========================================
Detector                     findingKey
===========================  ===========================================
No-show trend                ``no_show_trend#<window>``
Schedule gap                 ``schedule_gap#<providerId>#<pattern>``
Unmet demand                 ``unmet_demand#<service>``
Unoffered-service demand     ``unoffered_service_demand#<serviceName>``
Gap fill                     ``gap_fill#<slotId>``
===========================  ===========================================

All functions are pure and deterministic given their input, so they are trivial
to unit- and property-test against in-memory data.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from clinic_front_desk.models import (
    Appointment,
    AppointmentStatus,
    CallSession,
    DecisionKind,
    Finding,
    ISODate,
    Slot,
    SlotStatus,
    WaitlistEntry,
)

# ---------------------------------------------------------------------------
# Detector thresholds (qualitative triggers).
#
# These gate the *shape* of a pattern (e.g. "the no-show rate is elevated"),
# which is distinct from the synthesizer's ≥ 5-record *support* gate (Req 13.6).
# ---------------------------------------------------------------------------

#: A no-show rate strictly above this baseline over the window is a "trend".
NO_SHOW_BASELINE_RATE: float = 0.15

#: Utilization (booked / total slots) at or below this is "low utilization".
LOW_UTILIZATION_THRESHOLD: float = 0.5

#: Open slots recurring on the same weekday at or above this count form a
#: recurring schedule-gap pattern.
RECURRING_GAP_THRESHOLD: int = 2

#: A waitlist depth for a single service at or above this is "unmet demand".
UNMET_DEMAND_THRESHOLD: int = 3

#: Weekday names for readable schedule-gap summaries; index 0 == Monday to match
#: :meth:`datetime.date.weekday`.
_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


@dataclass(frozen=True)
class PatternInput:
    """A snapshot of the accumulated data an analysis run reads over (Req 13.2).

    The caller (the ``DecisionSynthesizer`` / ``AnalysisScheduler``) assembles
    this from the Data_Layer. Detectors treat it as read-only.

    Attributes:
        appointments: Appointments in scope (used for no-show trend).
        slots: Slots in scope (used for schedule-gap and gap-fill detection).
        waitlist: Active waitlist entries (unmet demand + gap-fill matching).
        call_sessions: Recent call sessions (kept for windowing/traceability).
        offered_services: The clinic's currently offered service names.
        named_service_requests: Service names patients named during calls,
            extracted upstream from Call_Session data. Used to detect repeated
            demand for a service the clinic does not offer (Req 13.3 unoffered).
        window_days: The analysis window in days; also forms the no-show
            findingKey suffix (``no_show_trend#<window>``).
        now: Reference date (ISO) for the window's right edge. When empty, no
            date filtering is applied and every supplied record is in scope.
    """

    appointments: list[Appointment] = field(default_factory=list)
    slots: list[Slot] = field(default_factory=list)
    waitlist: list[WaitlistEntry] = field(default_factory=list)
    call_sessions: list[CallSession] = field(default_factory=list)
    offered_services: frozenset[str] = frozenset()
    named_service_requests: list[str] = field(default_factory=list)
    window_days: int = 30
    now: ISODate = ""


# ---------------------------------------------------------------------------
# Small helpers.
# ---------------------------------------------------------------------------


def _parse_date(value: str) -> date | None:
    """Parse the leading ISO date out of a date/datetime string, else ``None``."""
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _within_window(value: str, now: date | None, window_days: int) -> bool:
    """Return whether ``value``'s date lies in ``(now - window_days, now]``.

    With no reference ``now`` (or an unparseable ``value``) the record is kept,
    so an unwindowed snapshot analyses everything supplied.
    """
    if now is None:
        return True
    d = _parse_date(value)
    if d is None:
        return True
    delta = (now - d).days
    return 0 <= delta <= window_days


def _weekday(value: str) -> int | None:
    """Return the weekday index (0 == Monday) of an ISO date/datetime, else ``None``."""
    d = _parse_date(value)
    return None if d is None else d.weekday()


def _window_label(window_days: int) -> str:
    """The ``<window>`` token used in the no-show findingKey (e.g. ``30d``)."""
    return f"{window_days}d"


# ---------------------------------------------------------------------------
# Detectors.
# ---------------------------------------------------------------------------


def detect_no_show_trend(inp: PatternInput) -> list[Finding]:
    """Detect an elevated no-show rate over the window (findingKey ``no_show_trend#<window>``).

    Considers appointments whose date falls in the window and that reached a
    terminal attendance state (completed / no_show). Emits a single finding when
    the no-show rate exceeds :data:`NO_SHOW_BASELINE_RATE`, with
    ``supporting_record_count`` equal to the number of such terminal
    appointments (the records backing the rate).
    """
    now = _parse_date(inp.now)
    terminal = {AppointmentStatus.NO_SHOW, AppointmentStatus.COMPLETED}
    considered = [
        a
        for a in inp.appointments
        if a.status in terminal and _within_window(a.date, now, inp.window_days)
    ]
    if not considered:
        return []

    no_shows = sum(1 for a in considered if a.status == AppointmentStatus.NO_SHOW)
    rate = no_shows / len(considered)
    if rate <= NO_SHOW_BASELINE_RATE:
        return []

    window = _window_label(inp.window_days)
    pct = round(rate * 100)
    return [
        Finding(
            key=f"no_show_trend#{window}",
            kind=DecisionKind.NO_SHOW_TREND,
            summary=(
                f"No-show rate is {pct}% over the last {inp.window_days} days "
                f"({no_shows} of {len(considered)} appointments), above the "
                f"{round(NO_SHOW_BASELINE_RATE * 100)}% baseline."
            ),
            recommended_action=(
                "Enable appointment reminders or a light-overbooking policy to "
                "recover no-show capacity."
            ),
            action_payload={
                "window_days": inp.window_days,
                "no_show_count": no_shows,
                "considered_count": len(considered),
                "rate": rate,
            },
            supporting_record_count=len(considered),
            actionable=True,
        )
    ]


def detect_schedule_gaps(inp: PatternInput) -> list[Finding]:
    """Detect recurring open slots / low utilization per provider.

    For each provider whose utilization (booked / total in-window slots) is at or
    below :data:`LOW_UTILIZATION_THRESHOLD`, groups the provider's open slots by
    weekday and emits one finding per weekday carrying at least
    :data:`RECURRING_GAP_THRESHOLD` open slots. findingKey is
    ``schedule_gap#<providerId>#<pattern>`` where ``<pattern>`` is ``dow<n>``
    (weekday index). ``supporting_record_count`` is the number of open slots on
    that weekday.
    """
    now = _parse_date(inp.now)
    in_window = [s for s in inp.slots if _within_window(s.start, now, inp.window_days)]
    if not in_window:
        return []

    findings: list[Finding] = []
    provider_ids = sorted({s.provider_id for s in in_window})
    for provider_id in provider_ids:
        provider_slots = [s for s in in_window if s.provider_id == provider_id]
        total = len(provider_slots)
        booked = sum(1 for s in provider_slots if s.status == SlotStatus.BOOKED)
        utilization = booked / total if total else 0.0
        if utilization > LOW_UTILIZATION_THRESHOLD:
            continue

        open_slots = [s for s in provider_slots if s.status == SlotStatus.OPEN]
        by_weekday: Counter[int] = Counter()
        for s in open_slots:
            wd = _weekday(s.start)
            if wd is not None:
                by_weekday[wd] += 1

        for weekday in sorted(by_weekday):
            count = by_weekday[weekday]
            if count < RECURRING_GAP_THRESHOLD:
                continue
            day_name = _WEEKDAY_NAMES[weekday]
            findings.append(
                Finding(
                    key=f"schedule_gap#{provider_id}#dow{weekday}",
                    kind=DecisionKind.SCHEDULE_GAP,
                    summary=(
                        f"Provider {provider_id} has {count} recurring open slots on "
                        f"{day_name}s (utilization {round(utilization * 100)}%)."
                    ),
                    recommended_action=(
                        f"Consolidate or promote {day_name} availability for provider "
                        f"{provider_id} to lift utilization."
                    ),
                    action_payload={
                        "provider_id": provider_id,
                        "weekday": weekday,
                        "open_slot_count": count,
                        "utilization": utilization,
                    },
                    supporting_record_count=count,
                    actionable=True,
                )
            )
    return findings


def detect_unmet_demand(inp: PatternInput) -> list[Finding]:
    """Detect services whose active waitlist depth signals unmet demand.

    Emits one finding per service whose active-waitlist depth is at or above
    :data:`UNMET_DEMAND_THRESHOLD` (findingKey ``unmet_demand#<service>``).
    ``supporting_record_count`` is the waitlist depth.
    """
    depth: Counter[str] = Counter()
    for e in inp.waitlist:
        if e.active:
            depth[e.service] += 1

    findings: list[Finding] = []
    for service in sorted(depth):
        count = depth[service]
        if count < UNMET_DEMAND_THRESHOLD:
            continue
        findings.append(
            Finding(
                key=f"unmet_demand#{service}",
                kind=DecisionKind.UNMET_DEMAND,
                summary=(
                    f"{count} patients are waitlisted for {service!r}, indicating "
                    "unmet demand."
                ),
                recommended_action=(
                    f"Add capacity for {service!r} (extra slots or a provider) to "
                    "clear the waitlist."
                ),
                action_payload={"service": service, "waitlist_depth": count},
                supporting_record_count=count,
                actionable=True,
            )
        )
    return findings


def detect_unoffered_service_demand(inp: PatternInput) -> list[Finding]:
    """Detect repeated calls naming a service the clinic does not offer.

    Counts occurrences in ``named_service_requests`` of services not present in
    ``offered_services`` and emits one finding per such service (findingKey
    ``unoffered_service_demand#<serviceName>``). ``supporting_record_count`` is
    the number of calls naming that service. Matching is case-insensitive.
    """
    offered_lower = {s.lower() for s in inp.offered_services}
    counts: Counter[str] = Counter()
    for name in inp.named_service_requests:
        if not name:
            continue
        if name.lower() in offered_lower:
            continue
        counts[name] += 1

    findings: list[Finding] = []
    for service_name in sorted(counts):
        count = counts[service_name]
        findings.append(
            Finding(
                key=f"unoffered_service_demand#{service_name}",
                kind=DecisionKind.UNOFFERED_SERVICE_DEMAND,
                summary=(
                    f"{count} callers requested {service_name!r}, which the clinic "
                    "does not currently offer."
                ),
                recommended_action=(
                    f"Consider offering {service_name!r} or a referral path for it."
                ),
                action_payload={"service_name": service_name, "request_count": count},
                supporting_record_count=count,
                actionable=True,
            )
        )
    return findings


def detect_gap_fill(inp: PatternInput) -> list[Finding]:
    """Detect open slots that match at least one waitlisted patient (Req 8.1).

    For each open slot whose service is requested by one or more active waitlist
    entries, emits a finding recommending the slot be filled from the waitlist
    (findingKey ``gap_fill#<slotId>``). ``supporting_record_count`` is the number
    of matching active waitlist entries. Service matching is case-sensitive to
    mirror the exact offered-service matching used elsewhere.
    """
    matches_by_service: Counter[str] = Counter()
    for e in inp.waitlist:
        if e.active:
            matches_by_service[e.service] += 1

    findings: list[Finding] = []
    for slot in inp.slots:
        if slot.status != SlotStatus.OPEN:
            continue
        match_count = matches_by_service.get(slot.service, 0)
        if match_count < 1:
            continue
        findings.append(
            Finding(
                key=f"gap_fill#{slot.id}",
                kind=DecisionKind.GAP_FILL,
                summary=(
                    f"Open slot {slot.id} for {slot.service!r} matches {match_count} "
                    "waitlisted patient(s)."
                ),
                recommended_action=(
                    "Contact the earliest matching waitlisted patient to fill the slot."
                ),
                action_payload={
                    "slot_id": slot.id,
                    "service": slot.service,
                    "provider_id": slot.provider_id,
                    "matching_entry_count": match_count,
                },
                supporting_record_count=match_count,
                actionable=True,
            )
        )
    return findings


def detect_all(inp: PatternInput) -> list[Finding]:
    """Run every detector and return the aggregated findings (Req 13.2, 8.1).

    The order is stable: no-show trend, schedule gaps, unmet demand,
    unoffered-service demand, then gap-fill matches.
    """
    findings: list[Finding] = []
    findings.extend(detect_no_show_trend(inp))
    findings.extend(detect_schedule_gaps(inp))
    findings.extend(detect_unmet_demand(inp))
    findings.extend(detect_unoffered_service_demand(inp))
    findings.extend(detect_gap_fill(inp))
    return findings


__all__ = [
    "NO_SHOW_BASELINE_RATE",
    "LOW_UTILIZATION_THRESHOLD",
    "RECURRING_GAP_THRESHOLD",
    "UNMET_DEMAND_THRESHOLD",
    "PatternInput",
    "detect_no_show_trend",
    "detect_schedule_gaps",
    "detect_unmet_demand",
    "detect_unoffered_service_demand",
    "detect_gap_fill",
    "detect_all",
]
