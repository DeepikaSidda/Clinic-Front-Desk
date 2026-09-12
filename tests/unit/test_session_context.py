"""Unit tests for ``SessionContext`` (task 7.1).

Covers fact retention for the duration of a Call_Session (Req 11.1), recall of
earlier-provided information without re-prompting (Req 11.4), and the current
task step index used for barge-in resume (Req 12.3 via task 7.9). The universal
retention/outcome property lives in task 7.2 (Property 15).
"""

from __future__ import annotations

import pytest

from clinic_front_desk.models import CallOutcome, PatientRef
from clinic_front_desk.voice.session_context import RETAINED_FACTS, SessionContext


def test_new_context_has_no_facts_and_starts_at_step_zero() -> None:
    ctx = SessionContext(session_id="cs1")

    assert ctx.known_facts() == {}
    assert ctx.current_step_index == 0
    assert ctx.outcome is None
    assert all(not ctx.remembers(fact) for fact in RETAINED_FACTS)


def test_retains_identifying_details_service_datetime_and_slot() -> None:
    """Req 11.1: identifying details, requested service, requested date/time,
    and slot selection are retained for the session."""
    ctx = SessionContext(session_id="cs1")

    ctx.set_identity(name="Jane Doe", callback_phone="555-0100")
    ctx.set_requested_service("hearing test")
    ctx.set_requested_datetime(date="2025-06-02", time="14:30")
    ctx.select_slot("slot-7")

    assert ctx.name == "Jane Doe"
    assert ctx.callback_phone == "555-0100"
    assert ctx.requested_service == "hearing test"
    assert ctx.requested_date == "2025-06-02"
    assert ctx.requested_time == "14:30"
    assert ctx.selected_slot_id == "slot-7"


def test_identity_is_built_incrementally_without_clobbering() -> None:
    """Req 11.1: identity can be provided across turns without losing prior values."""
    ctx = SessionContext(session_id="cs1")

    ctx.set_identity(name="Jane Doe")
    ctx.set_identity(callback_phone="555-0100")
    ctx.set_identity(extra_identifiers={"dob": "1990-01-01"})
    ctx.set_identity(extra_identifiers={"insurance_id": "X123"})
    ctx.set_identity(patient_id="p1")

    assert ctx.name == "Jane Doe"
    assert ctx.callback_phone == "555-0100"
    assert ctx.patient_id == "p1"
    # Extra identifiers accumulate (merge) rather than replace.
    assert ctx.extra_identifiers == {"dob": "1990-01-01", "insurance_id": "X123"}


def test_patient_ref_reflects_captured_identity() -> None:
    ctx = SessionContext(session_id="cs1")
    ctx.set_identity(name="Jane Doe", callback_phone="555-0100", patient_id="p1")

    assert ctx.patient_ref == PatientRef(
        patient_id="p1", name="Jane Doe", callback_phone="555-0100"
    )


def test_recall_returns_earlier_facts_without_reprompting() -> None:
    """Req 11.4: earlier-provided information can be retrieved for reuse."""
    ctx = SessionContext(session_id="cs1")
    ctx.set_requested_service("hearing test")
    ctx.select_slot("slot-7")

    assert ctx.remembers("requested_service") is True
    assert ctx.recall("requested_service") == "hearing test"
    assert ctx.remembers("selected_slot_id") is True
    assert ctx.recall("selected_slot_id") == "slot-7"

    # Facts not yet provided are known-absent (so the caller knows to ask once).
    assert ctx.remembers("callback_phone") is False
    assert ctx.recall("callback_phone") is None

    assert ctx.known_facts() == {
        "requested_service": "hearing test",
        "selected_slot_id": "slot-7",
    }


def test_recall_and_remembers_reject_unknown_fact_names() -> None:
    ctx = SessionContext(session_id="cs1")

    with pytest.raises(KeyError):
        ctx.recall("not_a_fact")
    with pytest.raises(KeyError):
        ctx.remembers("not_a_fact")


def test_task_step_index_advances_and_survives_for_resume() -> None:
    """Req 12.3 (task 7.9): the step index is mutable and persists so a task can
    resume from its pre-interruption step."""
    ctx = SessionContext(session_id="cs1")

    assert ctx.advance_step() == 1
    assert ctx.advance_step() == 2
    assert ctx.current_step_index == 2

    # A barge-in does not reset context; facts + step index remain intact.
    ctx.set_requested_service("hearing test")
    assert ctx.current_step_index == 2
    assert ctx.recall("requested_service") == "hearing test"

    # The orchestrator can explicitly restore a step.
    ctx.set_step(1)
    assert ctx.current_step_index == 1


def test_set_step_rejects_negative_index() -> None:
    ctx = SessionContext(session_id="cs1")
    with pytest.raises(ValueError):
        ctx.set_step(-1)


def test_record_outcome_holds_terminal_outcome() -> None:
    """Req 11.5: the outcome is held on the context for the orchestrator to persist."""
    ctx = SessionContext(session_id="cs1")
    assert ctx.outcome is None

    ctx.record_outcome(CallOutcome.BOOKED)
    assert ctx.outcome == CallOutcome.BOOKED
