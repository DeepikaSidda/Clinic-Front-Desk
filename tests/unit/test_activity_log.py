"""Unit tests for the call-activity-log aggregation (task 12.8, Req 15.2, 9.6).

Covers :mod:`clinic_front_desk.dashboard.activity_log`:

- Call-session outcomes map to the booked/rescheduled/cancelled interaction
  types; escalations map to ``escalated`` (Req 15.2, 9.6).
- Outcomes with no dashboard interaction type (waitlisted, no_action,
  interrupted) and un-finalized sessions produce no entry.
- Each entry exposes an interaction type, a date-time, and the associated
  patient identifier (Req 15.2).
- The combined log is ordered most-recent-first, mixing sessions and
  escalations by timestamp (Req 15.2).
- The store-reading wrapper reads both stores and propagates a read failure.

The property test for content and ordering lives in task 12.9.
"""

from __future__ import annotations

import pytest

from clinic_front_desk.dashboard.activity_log import (
    ActivityLogEntry,
    InteractionType,
    aggregate_activity_log,
    build_activity_log,
)
from clinic_front_desk.data_layer.faults import fail_on, wrap
from clinic_front_desk.data_layer.memory import (
    MemoryCallSessionStore,
    MemoryEscalationStore,
)
from clinic_front_desk.models import (
    CallOutcome,
    CallSession,
    Escalation,
    EscalationReason,
    PatientRef,
    StoreError,
    is_err,
    is_ok,
)


def _session(
    id: str,
    outcome: CallOutcome | None,
    *,
    started_at: str,
    ended_at: str | None = None,
    patient_ref: PatientRef | None = None,
) -> CallSession:
    return CallSession(
        id=id,
        started_at=started_at,
        ended_at=ended_at,
        outcome=outcome,
        patient_ref=patient_ref,
    )


def _escalation(
    id: str,
    *,
    created_at: str,
    patient_ref: PatientRef | None = None,
) -> Escalation:
    return Escalation(
        id=id,
        reason=EscalationReason.CLINICAL_CONTENT,
        call_session_id="cs-" + id,
        context="ctx",
        created_at=created_at,
        patient_ref=patient_ref,
    )


# ---------------------------------------------------------------------------
# Outcome -> interaction type mapping (Req 15.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (CallOutcome.BOOKED, InteractionType.BOOKED),
        (CallOutcome.RESCHEDULED, InteractionType.RESCHEDULED),
        (CallOutcome.CANCELLED, InteractionType.CANCELLED),
    ],
)
def test_session_outcome_maps_to_interaction_type(
    outcome: CallOutcome, expected: InteractionType
) -> None:
    """Req 15.2: booked/rescheduled/cancelled outcomes surface with the matching
    interaction type."""
    log = build_activity_log(
        [_session("s1", outcome, started_at="2025-06-01T09:00:00Z")], []
    )
    assert len(log) == 1
    assert log[0].interaction_type == expected


@pytest.mark.parametrize(
    "outcome",
    [CallOutcome.WAITLISTED, CallOutcome.NO_ACTION, CallOutcome.INTERRUPTED],
)
def test_session_without_interaction_type_is_excluded(outcome: CallOutcome) -> None:
    """Outcomes with no dashboard interaction type produce no log entry."""
    log = build_activity_log(
        [_session("s1", outcome, started_at="2025-06-01T09:00:00Z")], []
    )
    assert log == []


def test_unfinalized_session_is_excluded() -> None:
    """A session with no persisted outcome (still open) produces no entry."""
    log = build_activity_log([_session("s1", None, started_at="2025-06-01T09:00:00Z")], [])
    assert log == []


def test_escalated_session_not_double_counted_with_escalation() -> None:
    """An escalated call session yields no session entry; the escalated entry is
    sourced from the EscalationStore instead (Req 9.6), avoiding duplicates."""
    log = build_activity_log(
        [_session("s1", CallOutcome.ESCALATED, started_at="2025-06-01T09:00:00Z")],
        [_escalation("e1", created_at="2025-06-01T09:00:05Z")],
    )
    assert len(log) == 1
    assert log[0].source == "escalation"
    assert log[0].interaction_type == InteractionType.ESCALATED


# ---------------------------------------------------------------------------
# Entry content: type, date-time, patient identifier (Req 15.2)
# ---------------------------------------------------------------------------


def test_escalation_maps_to_escalated_entry_with_timestamp_and_identifier() -> None:
    """Req 9.6, 15.2: each escalation is an escalated entry carrying its
    date-time and patient identifier."""
    esc = _escalation(
        "e1",
        created_at="2025-06-01T12:00:00Z",
        patient_ref=PatientRef(patient_id="p9"),
    )
    log = build_activity_log([], [esc])
    assert len(log) == 1
    entry = log[0]
    assert entry.interaction_type == InteractionType.ESCALATED
    assert entry.timestamp == "2025-06-01T12:00:00Z"
    assert entry.patient_identifier == "p9"


def test_entry_timestamp_prefers_session_end_over_start() -> None:
    """A session's interaction date-time is its end time when finalized."""
    log = build_activity_log(
        [
            _session(
                "s1",
                CallOutcome.BOOKED,
                started_at="2025-06-01T09:00:00Z",
                ended_at="2025-06-01T09:03:00Z",
            )
        ],
        [],
    )
    assert log[0].timestamp == "2025-06-01T09:03:00Z"


def test_entry_timestamp_falls_back_to_start_when_no_end() -> None:
    log = build_activity_log(
        [_session("s1", CallOutcome.BOOKED, started_at="2025-06-01T09:00:00Z")], []
    )
    assert log[0].timestamp == "2025-06-01T09:00:00Z"


def test_patient_identifier_prefers_id_then_phone_then_name() -> None:
    id_first = build_activity_log(
        [
            _session(
                "s1",
                CallOutcome.BOOKED,
                started_at="2025-06-01T09:00:00Z",
                patient_ref=PatientRef(patient_id="p1", callback_phone="555", name="Jo"),
            )
        ],
        [],
    )
    assert id_first[0].patient_identifier == "p1"

    phone_next = build_activity_log(
        [
            _session(
                "s2",
                CallOutcome.BOOKED,
                started_at="2025-06-01T09:00:00Z",
                patient_ref=PatientRef(callback_phone="555-0100", name="Jo"),
            )
        ],
        [],
    )
    assert phone_next[0].patient_identifier == "555-0100"

    name_last = build_activity_log(
        [
            _session(
                "s3",
                CallOutcome.BOOKED,
                started_at="2025-06-01T09:00:00Z",
                patient_ref=PatientRef(name="Jo"),
            )
        ],
        [],
    )
    assert name_last[0].patient_identifier == "Jo"


def test_patient_identifier_none_when_unknown() -> None:
    log = build_activity_log(
        [_session("s1", CallOutcome.BOOKED, started_at="2025-06-01T09:00:00Z")], []
    )
    assert log[0].patient_identifier is None


# ---------------------------------------------------------------------------
# Ordering: most-recent-first across both sources (Req 15.2)
# ---------------------------------------------------------------------------


def test_log_is_ordered_most_recent_first_across_sources() -> None:
    """Req 15.2: the combined log interleaves sessions and escalations ordered
    most-recent-first by date-time."""
    sessions = [
        _session("s_old", CallOutcome.BOOKED, started_at="2025-06-01T08:00:00Z"),
        _session("s_new", CallOutcome.CANCELLED, started_at="2025-06-01T14:00:00Z"),
    ]
    escalations = [
        _escalation("e_mid", created_at="2025-06-01T10:00:00Z"),
    ]

    log = build_activity_log(sessions, escalations)

    timestamps = [e.timestamp for e in log]
    assert timestamps == sorted(timestamps, reverse=True)
    assert [e.source_id for e in log] == ["s_new", "e_mid", "s_old"]


def test_equal_timestamps_use_source_id_as_stable_tiebreak() -> None:
    """Equal timestamps get a deterministic order (source id, descending)."""
    ts = "2025-06-01T09:00:00Z"
    escalations = [_escalation("e_a", created_at=ts), _escalation("e_b", created_at=ts)]
    log = build_activity_log([], escalations)
    assert [e.source_id for e in log] == ["e_b", "e_a"]


def test_empty_inputs_produce_empty_log() -> None:
    assert build_activity_log([], []) == []


def test_build_activity_log_is_pure_and_does_not_mutate_inputs() -> None:
    """Calling the aggregation twice on the same inputs yields identical output."""
    sessions = [_session("s1", CallOutcome.BOOKED, started_at="2025-06-01T09:00:00Z")]
    escalations = [_escalation("e1", created_at="2025-06-01T10:00:00Z")]

    first = build_activity_log(sessions, escalations)
    second = build_activity_log(sessions, escalations)

    assert first == second
    assert len(sessions) == 1 and len(escalations) == 1


# ---------------------------------------------------------------------------
# Store-reading wrapper (Req 15.2, 9.6)
# ---------------------------------------------------------------------------


def test_aggregate_reads_both_stores_and_builds_log() -> None:
    call_store = MemoryCallSessionStore()
    esc_store = MemoryEscalationStore()

    call_store.create(_session("s1", None, started_at="2025-06-01T09:00:00Z"))
    # `ended_at` explicit: the log orders a call by when it ended, and finalize
    # defaults that to now, which would otherwise make this ordering depend on the
    # wall clock.
    call_store.finalize(
        "s1",
        CallOutcome.BOOKED,
        PatientRef(patient_id="p1"),
        ended_at="2025-06-01T09:05:00Z",
    )
    esc_store.create(_escalation("e1", created_at="2025-06-01T11:00:00Z"))

    result = aggregate_activity_log(call_store, esc_store, limit=10)

    assert is_ok(result)
    log = result.value
    assert [e.source_id for e in log] == ["e1", "s1"]
    assert log[0].interaction_type == InteractionType.ESCALATED
    assert log[1].interaction_type == InteractionType.BOOKED
    assert log[1].patient_identifier == "p1"


def test_aggregate_propagates_call_session_read_failure() -> None:
    call_store = wrap(MemoryCallSessionStore(), fail_on("list_recent"))
    esc_store = MemoryEscalationStore()

    result = aggregate_activity_log(call_store, esc_store)

    assert is_err(result)
    assert isinstance(result.error, StoreError)


def test_aggregate_propagates_escalation_read_failure() -> None:
    call_store = MemoryCallSessionStore()
    esc_store = wrap(MemoryEscalationStore(), fail_on("list_recent"))

    result = aggregate_activity_log(call_store, esc_store)

    assert is_err(result)
    assert isinstance(result.error, StoreError)


def test_entry_is_immutable() -> None:
    """Entries are frozen dataclasses (safe to fan out to dashboard clients)."""
    entry = build_activity_log(
        [_session("s1", CallOutcome.BOOKED, started_at="2025-06-01T09:00:00Z")], []
    )[0]
    with pytest.raises(Exception):
        entry.timestamp = "changed"  # type: ignore[misc]
    assert isinstance(entry, ActivityLogEntry)
