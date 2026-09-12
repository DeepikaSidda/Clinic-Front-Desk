"""Unit tests for Call_Session lifecycle helpers (task 7.11).

Covers the two pieces of :mod:`clinic_front_desk.voice.session_lifecycle`:

1. Call_Session outcome persistence on session end (Req 11.5, 12.7) via
   :func:`finalize_session` over the in-memory :class:`MemoryCallSessionStore`
   and the fault-injection wrapper for the persistence-failure path.
2. Empty-config intake behaviour (Req 1.7) via
   :func:`check_intake_availability` / :func:`evaluate_intake`.
"""

from __future__ import annotations

from clinic_front_desk.data_layer.faults import fail_on, wrap
from clinic_front_desk.data_layer.memory import (
    MemoryCallSessionStore,
    MemoryClinicKnowledgeBaseStore,
)
from clinic_front_desk.models import (
    CallOutcome,
    CallSession,
    ClinicKnowledgeBase,
    DayHours,
    ServiceConfig,
    is_err,
    is_ok,
)
from clinic_front_desk.voice.session_context import SessionContext
from clinic_front_desk.voice.session_lifecycle import (
    AcceptingCalls,
    NotAcceptingCalls,
    check_intake_availability,
    evaluate_intake,
    finalize_session,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_session(store: MemoryCallSessionStore, session_id: str) -> None:
    """Create an open call-session record so it can later be finalized."""
    result = store.create(CallSession(id=session_id, started_at="2025-06-01T09:00:00Z"))
    assert is_ok(result)


def _kb(*, hours: bool, services: bool) -> ClinicKnowledgeBase:
    """Build a knowledge base with/without configured hours and services."""
    return ClinicKnowledgeBase(
        location="123 Main St" if hours or services else "",
        hours={0: DayHours(open="09:00", close="17:00")} if hours else {},
        services=[ServiceConfig(name="hearing test")] if services else [],
    )


# ---------------------------------------------------------------------------
# 1. Outcome persistence (Req 11.5, 12.7)
# ---------------------------------------------------------------------------


def test_finalize_session_persists_explicit_outcome_and_identity() -> None:
    store = MemoryCallSessionStore()
    _open_session(store, "sess-1")

    ctx = SessionContext(session_id="sess-1")
    ctx.set_identity(name="Ada Lovelace", callback_phone="555-0100", patient_id="pat-1")

    result = finalize_session(store, ctx, CallOutcome.BOOKED)

    assert is_ok(result)
    saved = result.value
    assert saved.outcome is CallOutcome.BOOKED
    assert saved.patient_ref is not None
    assert saved.patient_ref.name == "Ada Lovelace"
    assert saved.patient_ref.callback_phone == "555-0100"
    assert saved.patient_ref.patient_id == "pat-1"
    # Context and persisted record agree.
    assert ctx.outcome is CallOutcome.BOOKED


def test_finalize_session_uses_outcome_recorded_on_context() -> None:
    store = MemoryCallSessionStore()
    _open_session(store, "sess-2")

    ctx = SessionContext(session_id="sess-2")
    ctx.record_outcome(CallOutcome.WAITLISTED)

    result = finalize_session(store, ctx)  # no explicit outcome

    assert is_ok(result)
    assert result.value.outcome is CallOutcome.WAITLISTED


def test_finalize_session_defaults_to_no_action() -> None:
    store = MemoryCallSessionStore()
    _open_session(store, "sess-3")

    ctx = SessionContext(session_id="sess-3")  # nothing recorded

    result = finalize_session(store, ctx)

    assert is_ok(result)
    assert result.value.outcome is CallOutcome.NO_ACTION
    assert ctx.outcome is CallOutcome.NO_ACTION


def test_finalize_session_persists_interrupted_outcome() -> None:
    # Voice-layer loss path (Req 12.7): the TurnController signals interrupted.
    store = MemoryCallSessionStore()
    _open_session(store, "sess-4")

    ctx = SessionContext(session_id="sess-4")
    ctx.set_identity(name="Grace Hopper")

    result = finalize_session(store, ctx, CallOutcome.INTERRUPTED)

    assert is_ok(result)
    assert result.value.outcome is CallOutcome.INTERRUPTED
    assert result.value.patient_ref is not None
    assert result.value.patient_ref.name == "Grace Hopper"


def test_finalize_session_explicit_outcome_overrides_context() -> None:
    store = MemoryCallSessionStore()
    _open_session(store, "sess-5")

    ctx = SessionContext(session_id="sess-5")
    ctx.record_outcome(CallOutcome.NO_ACTION)

    result = finalize_session(store, ctx, CallOutcome.ESCALATED)

    assert is_ok(result)
    assert result.value.outcome is CallOutcome.ESCALATED
    assert ctx.outcome is CallOutcome.ESCALATED


def test_finalize_session_returns_error_on_persistence_failure() -> None:
    # A failed finalize leaves prior records unchanged (Req 16.6) and the
    # failure is surfaced to the caller.
    store = wrap(MemoryCallSessionStore(), fail_on("finalize"))
    _open_session(store, "sess-6")  # type: ignore[arg-type]

    ctx = SessionContext(session_id="sess-6")
    ctx.record_outcome(CallOutcome.CANCELLED)

    result = finalize_session(store, ctx)

    assert is_err(result)


# ---------------------------------------------------------------------------
# 2. Empty-config intake behaviour (Req 1.7)
# ---------------------------------------------------------------------------


def test_intake_not_accepting_when_unconfigured() -> None:
    # No stored config at all (empty-init state, Req 16.4).
    signal = check_intake_availability(None)
    assert isinstance(signal, NotAcceptingCalls)
    assert signal.offer_message is True


def test_intake_not_accepting_when_no_hours_and_no_services() -> None:
    signal = check_intake_availability(_kb(hours=False, services=False))
    assert isinstance(signal, NotAcceptingCalls)


def test_intake_accepting_when_hours_configured() -> None:
    signal = check_intake_availability(_kb(hours=True, services=False))
    assert isinstance(signal, AcceptingCalls)


def test_intake_accepting_when_services_configured() -> None:
    signal = check_intake_availability(_kb(hours=False, services=True))
    assert isinstance(signal, AcceptingCalls)


def test_intake_accepting_when_both_configured() -> None:
    signal = check_intake_availability(_kb(hours=True, services=True))
    assert isinstance(signal, AcceptingCalls)


def test_intake_ignores_all_closed_days_as_no_hours() -> None:
    # A hours mapping present but with every day closed (None) is "no hours".
    kb = ClinicKnowledgeBase(location="", hours={0: None, 1: None}, services=[])
    signal = check_intake_availability(kb)
    assert isinstance(signal, NotAcceptingCalls)


def test_evaluate_intake_reads_empty_store() -> None:
    store = MemoryClinicKnowledgeBaseStore()  # empty-init returns Ok(None)
    signal = evaluate_intake(store)
    assert isinstance(signal, NotAcceptingCalls)


def test_evaluate_intake_reads_configured_store() -> None:
    store = MemoryClinicKnowledgeBaseStore()
    saved = store.save(_kb(hours=True, services=True))
    assert is_ok(saved)

    signal = evaluate_intake(store)
    assert isinstance(signal, AcceptingCalls)


def test_evaluate_intake_conservative_on_store_failure() -> None:
    store = wrap(MemoryClinicKnowledgeBaseStore(), fail_on("get"))
    signal = evaluate_intake(store)  # type: ignore[arg-type]
    assert isinstance(signal, NotAcceptingCalls)
