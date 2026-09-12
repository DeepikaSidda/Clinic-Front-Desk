"""Call-activity-log aggregation for the Dashboard (task 12.8, Req 15.2, 9.6).

The dashboard's ``CallActivityLog`` component (design "Dashboard") shows a single
chronological feed of what the Voice_Front_Desk did, drawn from two stores:

- :class:`~clinic_front_desk.data_layer.interfaces.CallSessionStore.list_recent`
  supplies finalized call sessions, whose outcome maps to a *booked*,
  *rescheduled*, or *cancelled* interaction.
- :class:`~clinic_front_desk.data_layer.interfaces.EscalationStore.list_recent`
  supplies escalations, each an *escalated* interaction (Req 9.6).

Every entry exposes the interaction type, the date-time of the interaction, and
the associated patient identifier, ordered most-recent-first (Req 15.2).

The core aggregation, :func:`build_activity_log`, is a **pure, deterministic**
function over already-read records: given the same call sessions and escalations
it always returns the same ordered log, with no I/O and no hidden state. This is
what Property 22 (task 12.9) exercises. :func:`aggregate_activity_log` is the
thin store-reading wrapper the BFF calls; it defers all shaping/ordering to the
pure function.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from clinic_front_desk.data_layer.interfaces import (
    CallSessionStore,
    EscalationStore,
)
from clinic_front_desk.models import (
    CallOutcome,
    CallSession,
    Escalation,
    ISODateTime,
    PatientRef,
    StoreResult,
    is_err,
)
from clinic_front_desk.models import Ok


class InteractionType(StrEnum):
    """The interaction type shown for an activity-log entry (Req 15.2).

    Exactly the four types the dashboard surfaces; escalations always map to
    :attr:`ESCALATED`, while call sessions map only from the booked/rescheduled/
    cancelled outcomes (see :data:`_OUTCOME_TO_INTERACTION`).
    """

    BOOKED = "booked"
    RESCHEDULED = "rescheduled"
    CANCELLED = "cancelled"
    ESCALATED = "escalated"


# Which call-session outcomes surface as activity-log entries, and as what type.
# Only these three map to an interaction type (Req 15.2). Escalated sessions are
# intentionally excluded here: escalations are sourced from the EscalationStore
# so they are never double-counted. Outcomes that have no dashboard interaction
# type (waitlisted, no_action, interrupted) and un-finalized sessions (no
# outcome) produce no entry.
_OUTCOME_TO_INTERACTION: dict[CallOutcome, InteractionType] = {
    CallOutcome.BOOKED: InteractionType.BOOKED,
    CallOutcome.RESCHEDULED: InteractionType.RESCHEDULED,
    CallOutcome.CANCELLED: InteractionType.CANCELLED,
}


@dataclass(frozen=True)
class ActivityLogEntry:
    """A single row of the dashboard call-activity log (Req 15.2).

    Attributes:
        interaction_type: One of booked/rescheduled/cancelled/escalated.
        timestamp: ISO-8601 UTC date-time of the interaction. For a call session
            this is its end time (falling back to its start time); for an
            escalation it is when the escalation was recorded.
        patient_identifier: The associated patient identifier when known
            (patient id, else callback phone, else name), or ``None``.
        source: ``"call_session"`` or ``"escalation"`` — which store produced it.
        source_id: The originating record's id, for traceability and stable
            ordering of same-timestamp entries.
        patient_ref: The full patient reference captured for the interaction,
            when any identity was known.
    """

    interaction_type: InteractionType
    timestamp: ISODateTime
    patient_identifier: str | None
    source: str
    source_id: str
    patient_ref: PatientRef | None = None


def _identifier_from_ref(ref: PatientRef | None) -> str | None:
    """Derive a single patient identifier from a partial reference.

    Prefers the stable ``patient_id``, then the ``callback_phone``, then the
    ``name`` (Req 15.2 "associated patient identifier"). Returns ``None`` when no
    identifying detail was captured.
    """
    if ref is None:
        return None
    for candidate in (ref.patient_id, ref.callback_phone, ref.name):
        if candidate:
            return candidate
    return None


def _entry_from_session(session: CallSession) -> ActivityLogEntry | None:
    """Map a finalized call session to a log entry, or ``None`` if it has no
    booked/rescheduled/cancelled interaction type."""
    if session.outcome is None:
        return None
    interaction = _OUTCOME_TO_INTERACTION.get(session.outcome)
    if interaction is None:
        return None
    # Prefer when the interaction concluded; fall back to session start.
    timestamp = session.ended_at or session.started_at
    return ActivityLogEntry(
        interaction_type=interaction,
        timestamp=timestamp,
        patient_identifier=_identifier_from_ref(session.patient_ref),
        source="call_session",
        source_id=session.id,
        patient_ref=session.patient_ref,
    )


def _entry_from_escalation(escalation: Escalation) -> ActivityLogEntry:
    """Map an escalation to an ``escalated`` log entry (Req 9.6)."""
    return ActivityLogEntry(
        interaction_type=InteractionType.ESCALATED,
        timestamp=escalation.created_at,
        patient_identifier=_identifier_from_ref(escalation.patient_ref),
        source="escalation",
        source_id=escalation.id,
        patient_ref=escalation.patient_ref,
    )


def build_activity_log(
    sessions: Iterable[CallSession],
    escalations: Iterable[Escalation],
) -> list[ActivityLogEntry]:
    """Aggregate call sessions and escalations into a most-recent-first log.

    Pure and deterministic: no I/O, no clock reads, no mutation of the inputs.
    Call sessions contribute booked/rescheduled/cancelled entries (per their
    outcome); every escalation contributes an ``escalated`` entry. The combined
    list is ordered most-recent-first by ``timestamp`` (Req 15.2), with the
    originating record id as a stable tiebreaker for equal timestamps so the
    ordering is fully determined by the inputs.

    Timestamps are ISO-8601 UTC strings, which sort lexicographically in
    chronological order; a descending sort therefore yields most-recent-first.
    """
    entries: list[ActivityLogEntry] = []
    for session in sessions:
        entry = _entry_from_session(session)
        if entry is not None:
            entries.append(entry)
    for escalation in escalations:
        entries.append(_entry_from_escalation(escalation))

    entries.sort(key=lambda e: (e.timestamp, e.source_id), reverse=True)
    return entries


def aggregate_activity_log(
    call_session_store: CallSessionStore,
    escalation_store: EscalationStore,
    limit: int = 50,
) -> StoreResult[list[ActivityLogEntry]]:
    """Read both stores and build the activity log (Req 15.2, 9.6).

    Reads up to ``limit`` most-recent records from each store via ``list_recent``
    and defers all shaping/ordering to :func:`build_activity_log`. A read failure
    from either store is propagated as an ``Err`` (no partial log), consistent
    with the Data_Layer's all-or-nothing contract.
    """
    sessions_result = call_session_store.list_recent(limit)
    if is_err(sessions_result):
        return sessions_result

    escalations_result = escalation_store.list_recent(limit)
    if is_err(escalations_result):
        return escalations_result

    return Ok(
        build_activity_log(sessions_result.value, escalations_result.value)
    )


__all__ = [
    "InteractionType",
    "ActivityLogEntry",
    "build_activity_log",
    "aggregate_activity_log",
]
