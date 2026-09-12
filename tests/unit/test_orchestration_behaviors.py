"""Example/edge-case unit tests for Voice_Front_Desk orchestration behaviors (task 7.12).

These cover the concrete clarifying/decline behaviors from the design's Testing
Strategy that complement the property tests, exercised against the real
orchestration components and in-memory fake stores:

- **empty-config response (Req 1.7)** — while no clinic hours and no offered
  services are configured, intake reports the clinic is not yet accepting calls
  and offers to take a message.
- **no-appointment-offer-to-book (Req 4.2)** — a reschedule/cancel for a patient
  with no matching appointment surfaces a not-found signal (the cue to offer to
  book) and leaves nothing to mutate.
- **cancel confirmation prompt (Req 5.4)** — a cancellation is not carried out
  until the patient confirms; declining never invokes the cancel tool.
- **distress escalation offer (Req 9.3)** — an expressed-distress turn is
  *offered* an escalation rather than escalated immediately.
- **human-follow-up message (Req 9.5)** — the bounded interpretation-failure
  escalation signals a human will follow up.
"""

from __future__ import annotations

from clinic_front_desk.data_layer.memory import (
    MemoryAppointmentStore,
    MemoryClinicKnowledgeBaseStore,
)
from clinic_front_desk.models import (
    ClinicKnowledgeBase,
    DayHours,
    EscalationReason,
    NotFound,
    Provider,
    ServiceConfig,
    Slot,
    SlotStatus,
    is_err,
    is_ok,
)
from clinic_front_desk.tools.appointments import book_appointment, cancel, reschedule
from clinic_front_desk.voice.guardrails import GuardrailPolicy, Turn, TurnClassification
from clinic_front_desk.voice.session_context import SessionContext
from clinic_front_desk.voice.session_lifecycle import (
    AcceptingCalls,
    NotAcceptingCalls,
    check_intake_availability,
    evaluate_intake,
)
from clinic_front_desk.voice.tool_orchestrator import (
    MutationCommitted,
    MutationDeclined,
    ToolOrchestrator,
)
from clinic_front_desk.voice.turn_controller import Escalate, ReAsk, TurnController

PROVIDER_ID = "prov-1"
SERVICE = "Hearing Test"


# ---------------------------------------------------------------------------
# Empty-config response (Req 1.7)
# ---------------------------------------------------------------------------


def test_empty_config_is_not_accepting_calls_and_offers_message() -> None:
    """No hours and no services -> not accepting calls, offer a message (Req 1.7)."""
    signal = check_intake_availability(None)
    assert isinstance(signal, NotAcceptingCalls)
    assert signal.offer_message is True

    empty_kb = ClinicKnowledgeBase(location="", hours={}, services=[])
    signal = check_intake_availability(empty_kb)
    assert isinstance(signal, NotAcceptingCalls)
    assert signal.offer_message is True


def test_evaluate_intake_from_unconfigured_store_offers_message() -> None:
    """An unconfigured knowledge-base store yields the not-accepting signal (Req 1.7)."""
    store = MemoryClinicKnowledgeBaseStore()
    signal = evaluate_intake(store)
    assert isinstance(signal, NotAcceptingCalls)


def test_configured_hours_or_services_flip_to_accepting() -> None:
    """Configuring either hours or a service flips intake to accepting (Req 1.7)."""
    with_service = ClinicKnowledgeBase(
        location="123 Main St",
        hours={},
        services=[ServiceConfig(name=SERVICE, price=150.0)],
    )
    assert isinstance(check_intake_availability(with_service), AcceptingCalls)

    with_hours = ClinicKnowledgeBase(
        location="123 Main St",
        hours={1: DayHours(open="09:00", close="17:00")},
        services=[],
    )
    assert isinstance(check_intake_availability(with_hours), AcceptingCalls)


# ---------------------------------------------------------------------------
# No-appointment-offer-to-book (Req 4.2)
# ---------------------------------------------------------------------------


def test_no_matching_appointment_surfaces_not_found_to_offer_booking() -> None:
    """Reschedule/cancel with no matching appointment is not-found (offer to book, Req 4.2)."""
    store = MemoryAppointmentStore()
    # A patient with no appointment on file: the reschedule/cancel lookups fail
    # not-found, which is the orchestration's cue to offer to book instead.
    resched = reschedule(store, appointment_id="missing-appt", new_slot_id="slot-x")
    assert is_err(resched)
    assert isinstance(resched.error, NotFound)

    cancelled = cancel(store, appointment_id="missing-appt")
    assert is_err(cancelled)
    assert isinstance(cancelled.error, NotFound)

    # Booking is available for such a patient: seeding an open slot lets a new
    # appointment be created (the "offer to book" path).
    store.seed_slot(
        Slot(
            id="slot-1",
            provider_id=PROVIDER_ID,
            service=SERVICE,
            start="2999-01-01T10:00:00",
            end="2999-01-01T10:30:00",
            status=SlotStatus.OPEN,
        )
    )
    booked = book_appointment(
        store,
        provider_id=PROVIDER_ID,
        patient_id="pat-1",
        slot_id="slot-1",
        service=SERVICE,
    )
    assert is_ok(booked)


# ---------------------------------------------------------------------------
# Cancel confirmation prompt (Req 5.4)
# ---------------------------------------------------------------------------


def test_cancel_requires_confirmation_before_mutating() -> None:
    """Cancellation is withheld until the patient confirms (Req 5.4, 5.6)."""
    store = MemoryAppointmentStore()
    store.seed_slot(
        Slot(
            id="slot-1",
            provider_id=PROVIDER_ID,
            service=SERVICE,
            start="2999-01-01T10:00:00",
            end="2999-01-01T10:30:00",
            status=SlotStatus.OPEN,
        )
    )
    assert is_ok(
        book_appointment(
            store,
            provider_id=PROVIDER_ID,
            patient_id="pat-1",
            slot_id="slot-1",
            service=SERVICE,
            appointment_id="appt-1",
        )
    )

    orchestrator = ToolOrchestrator(SessionContext(session_id="s"))
    calls = {"count": 0}

    def cancel_tool():  # type: ignore[no-untyped-def]
        calls["count"] += 1
        return cancel(store, appointment_id="appt-1")

    # Before confirmation: the cancel tool is not invoked (the agent first states
    # the appointment and asks the patient to confirm, Req 5.4).
    outcome = orchestrator.cancel(confirmed=False, cancel_tool=cancel_tool)
    assert isinstance(outcome, MutationDeclined)
    assert calls["count"] == 0
    assert store.get_slot("slot-1").unwrap().status == SlotStatus.BOOKED

    # After confirmation: the cancel is carried out.
    committed = orchestrator.cancel(confirmed=True, cancel_tool=cancel_tool)
    assert isinstance(committed, MutationCommitted)
    assert calls["count"] == 1
    assert store.get_slot("slot-1").unwrap().status == SlotStatus.OPEN


# ---------------------------------------------------------------------------
# Distress escalation offer (Req 9.3)
# ---------------------------------------------------------------------------


def test_distress_turn_offers_escalation_rather_than_escalating() -> None:
    """Expressed distress is offered an escalation, not escalated immediately (Req 9.3)."""
    policy = GuardrailPolicy([SERVICE])
    decision = policy.classify(Turn(expresses_distress=True))

    assert decision.classification == TurnClassification.PATIENT_DISTRESS
    assert decision.offer_escalation is True
    assert decision.requires_escalation is False
    assert decision.escalation_reason == EscalationReason.PATIENT_DISTRESS


# ---------------------------------------------------------------------------
# Human-follow-up message (Req 9.5)
# ---------------------------------------------------------------------------


def test_escalation_after_retry_limit_signals_human_follow_up() -> None:
    """The bounded interpretation-failure escalation informs of a human follow-up (Req 9.5)."""
    controller = TurnController()

    first = controller.on_interpretation_failure()
    assert isinstance(first, ReAsk)

    second = controller.on_interpretation_failure()
    assert isinstance(second, Escalate)
    # The escalation instructs the caller to tell the patient a human will follow
    # up (Req 9.5, 12.5).
    assert second.inform_human_follow_up is True
