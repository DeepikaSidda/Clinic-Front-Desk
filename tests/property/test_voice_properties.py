"""Property-based tests for the Voice_Front_Desk orchestration + voice layer.

These validate design correctness Properties 8, 13, 14, 15, 16, and 17 against
the pure orchestration components (``SessionContext``, ``GuardrailPolicy``,
``ToolOrchestrator``, ``TurnController``, ``BargeInHandler``) and in-memory fake
stores — no real Nova Sonic stream is involved. Each property is implemented as
a single Hypothesis test running >=100 iterations and is tagged with a comment
in the design's required format.

Covered:
    - Property 8  (task 7.6)  — Reschedule/cancel confirmation semantics preserve
                                state (Req 4.5, 5.6)
    - Property 13 (task 7.13) — Escalation classification and faithful recording
                                (Req 9.1, 9.2, 9.4, 9.7, 9.8, 10.5)
    - Property 14 (task 7.4)  — Symptom inputs never infer a service
                                (Req 10.1, 10.2, 10.3, 10.4, 10.6)
    - Property 15 (task 7.2)  — Session context retention and outcome persistence
                                (Req 11.1, 11.4, 11.5, 12.7)
    - Property 16 (task 7.8)  — Interpretation-failure retries bounded then
                                escalate (Req 12.4, 12.5)
    - Property 17 (task 7.10) — Barge-in preserves and resumes task step (Req 12.3)

Generators follow the design's "Turns" note: labeled turn categories
(administrative, clinical, symptom-only, symptom+named-service, distress,
explicit-human, outside-admin) drive the guardrail/escalation properties.
"""

from __future__ import annotations

import string

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from clinic_front_desk.data_layer.memory import (
    MemoryAppointmentStore,
    MemoryCallSessionStore,
    MemoryEscalationStore,
)
from clinic_front_desk.models import (
    AppointmentStatus,
    CallOutcome,
    CallSession,
    EscalationReason,
    PatientRef,
    Slot,
    SlotStatus,
    is_ok,
)
from clinic_front_desk.tools.appointments import book_appointment, cancel, reschedule
from clinic_front_desk.tools.escalation import flag_for_human
from clinic_front_desk.tools.service_matcher import normalize_service_name
from clinic_front_desk.voice.barge_in import BargeInHandler
from clinic_front_desk.voice.guardrails import GuardrailPolicy, Turn, TurnClassification
from clinic_front_desk.voice.session_context import RETAINED_FACTS, SessionContext
from clinic_front_desk.voice.session_lifecycle import finalize_session
from clinic_front_desk.voice.tool_orchestrator import (
    MutationDeclined,
    NoAlternativeSlots,
    ToolOrchestrator,
)
from clinic_front_desk.voice.turn_controller import (
    Escalate,
    NoAction,
    ReAsk,
    TurnController,
)

pytestmark = pytest.mark.property

# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

_ident = st.text(alphabet=string.ascii_letters + string.digits, min_size=1, max_size=8)
_service = st.text(alphabet=string.ascii_letters + string.digits, min_size=1, max_size=8)
_short_text = st.text(min_size=1, max_size=12)

# The full set of persisted Call_Session outcomes (Req 11.5, 12.7) — the
# property asserts the persisted outcome is drawn only from this set.
_ALLOWED_OUTCOMES = set(CallOutcome)

# The full set of escalation reasons (Req 9.4) — a recorded escalation must
# carry a reason drawn only from this set.
_ALLOWED_REASONS = set(EscalationReason)


# ===========================================================================
# Property 15: Session context retention and outcome persistence
# (Req 11.1, 11.4, 11.5, 12.7)
# ===========================================================================


@settings(max_examples=150)
@given(
    name=st.one_of(st.none(), _short_text),
    phone=st.one_of(st.none(), _short_text),
    patient_id=st.one_of(st.none(), _ident),
    service=st.one_of(st.none(), _service),
    date=st.one_of(st.none(), _short_text),
    time=st.one_of(st.none(), _short_text),
    slot=st.one_of(st.none(), _ident),
    outcome=st.one_of(st.none(), st.sampled_from(list(CallOutcome))),
)
def test_property_15_session_context_retention_and_outcome_persistence(
    name: str | None,
    phone: str | None,
    patient_id: str | None,
    service: str | None,
    date: str | None,
    time: str | None,
    slot: str | None,
    outcome: CallOutcome | None,
) -> None:
    # Feature: clinic-front-desk-agent, Property 15: For any facts a patient
    # provides during a call (identifying details, requested service, date, time,
    # slot selection), those facts remain retrievable for the remainder of the
    # session and are used without re-prompting. When a session ends, its
    # persisted outcome is drawn only from {booked, rescheduled, cancelled,
    # waitlisted, escalated, no_action, interrupted} and the patient-provided
    # identifying information is preserved.
    # Validates: Requirements 11.1, 11.4, 11.5, 12.7
    store = MemoryCallSessionStore()
    session_id = "sess-1"
    assert is_ok(store.create(CallSession(id=session_id, started_at="2025-01-01T00:00:00Z")))

    ctx = SessionContext(session_id=session_id)
    ctx.set_identity(name=name, callback_phone=phone, patient_id=patient_id)
    if service is not None:
        ctx.set_requested_service(service)
    ctx.set_requested_datetime(date=date, time=time)
    if slot is not None:
        ctx.select_slot(slot)

    # The exact facts we provided this session (Req 11.1).
    provided: dict[str, object] = {}
    if patient_id is not None:
        provided["patient_id"] = patient_id
    if name is not None:
        provided["name"] = name
    if phone is not None:
        provided["callback_phone"] = phone
    if service is not None:
        provided["requested_service"] = service
    if date is not None:
        provided["requested_date"] = date
    if time is not None:
        provided["requested_time"] = time
    if slot is not None:
        provided["selected_slot_id"] = slot

    # Retention without re-prompting (Req 11.4): every provided fact remains
    # retrievable and unchanged; nothing we did not provide is "remembered".
    for fact in RETAINED_FACTS:
        if fact in provided:
            assert ctx.remembers(fact) is True
            assert ctx.recall(fact) == provided[fact]
        else:
            assert ctx.remembers(fact) is False
    assert ctx.known_facts() == provided

    # Outcome persistence on session end (Req 11.5, 12.7).
    if outcome is not None:
        ctx.record_outcome(outcome)
    result = finalize_session(store, ctx)
    assert is_ok(result)
    persisted = result.value

    # Persisted outcome drawn only from the allowed set; NO_ACTION when none set.
    assert persisted.outcome in _ALLOWED_OUTCOMES
    expected = outcome if outcome is not None else CallOutcome.NO_ACTION
    assert persisted.outcome == expected

    # Patient-provided identifying information preserved (Req 11.5).
    assert persisted.patient_ref is not None
    assert persisted.patient_ref.name == name
    assert persisted.patient_ref.callback_phone == phone
    assert persisted.patient_ref.patient_id == patient_id


# ===========================================================================
# Property 14: Symptom inputs never infer a service
# (Req 10.1, 10.2, 10.3, 10.4, 10.6)
# ===========================================================================


@settings(max_examples=200)
@given(
    offered=st.lists(_service, unique=True, min_size=1, max_size=5),
    named=st.one_of(st.none(), _service),
    names_symptom=st.booleans(),
    requests_clinical=st.booleans(),
    data=st.data(),
)
def test_property_14_symptom_never_infers_service(
    offered: list[str],
    named: str | None,
    names_symptom: bool,
    requests_clinical: bool,
    data: st.DataObject,
) -> None:
    # Feature: clinic-front-desk-agent, Property 14: For any patient input that
    # names a symptom, a service is selected iff the patient also explicitly
    # names an offered service; when only a symptom is provided (routing would
    # require interpreting it), no service is selected and flag_for_human is
    # invoked. For any clinical-advice, triage, diagnosis, or medication input,
    # the response contains no clinical guidance and escalates.
    # Validates: Requirements 10.1, 10.2, 10.3, 10.4, 10.6
    policy = GuardrailPolicy(offered)
    # Half the time draw a name that IS offered so the symptom+named-service
    # branch is exercised (design's "symptom+named-service" turn category).
    if data.draw(st.booleans(), label="pick_offered"):
        named = data.draw(st.sampled_from(offered), label="named_offered")
    turn = Turn(
        named_service=named,
        names_symptom=names_symptom,
        requests_clinical_content=requests_clinical,
    )
    decision = policy.classify(turn)

    # A service is selected iff an offered service was explicitly named (Req 10.2);
    # there is no symptom->service inference path (Req 10.3).
    #
    # "Named an offered service" is resolved by the matcher, which compares names
    # ignoring transcription casing/spacing and returns the exact configured
    # string — so the expectation is the first offered name that normalizes to the
    # named one, not the named string itself. (Hypothesis happily generates
    # offered lists like ["K", "k"], which are the same service name.)
    expected_service: str | None = None
    if named is not None:
        target = normalize_service_name(named)
        expected_service = next(
            (name for name in offered if normalize_service_name(name) == target), None
        )
    assert decision.selected_service == expected_service

    if requests_clinical:
        # Clinical content: escalate, decline, and never carry clinical guidance
        # (Req 10.1, 10.6). The decision is a pure classification — it exposes no
        # advice/triage/diagnosis, only the decline + escalation signals.
        assert decision.requires_escalation is True
        assert decision.should_flag_for_human is True
        assert decision.decline_clinical_content is True
        assert decision.escalation_reason == EscalationReason.CLINICAL_CONTENT
    elif names_symptom and expected_service is None:
        # Symptom-only, no offered service named: routing would require
        # interpreting the symptom, so no service is selected and we escalate
        # (Req 10.3, 10.4).
        assert decision.selected_service is None
        assert decision.requires_escalation is True
        assert decision.should_flag_for_human is True
        assert decision.escalation_reason == EscalationReason.CLINICAL_CONTENT
        assert decision.classification == TurnClassification.SYMPTOM_ROUTING
    elif names_symptom and expected_service is not None:
        # Symptom accompanied by an explicitly named offered service: the service
        # comes from the named service, not from interpreting the symptom (Req 10.2).
        assert decision.selected_service == expected_service


# ===========================================================================
# Property 13: Escalation classification and faithful recording
# (Req 9.1, 9.2, 9.4, 9.7, 9.8, 10.5)
# ===========================================================================

# Labeled turn categories (design "Generators": Turns) mapped to the turn they
# build and the escalation they should (or should not) drive.
_ESCALATING_LABELS: dict[str, EscalationReason] = {
    "clinical": EscalationReason.CLINICAL_CONTENT,
    "outside_admin": EscalationReason.OUTSIDE_ADMIN_RULES,
    "policy_decision": EscalationReason.OUTSIDE_ADMIN_RULES,
    "explicit_human": EscalationReason.PATIENT_REQUEST,
    "accept_offer": EscalationReason.PATIENT_DISTRESS,
}


def _build_turn(label: str, named: str | None) -> Turn:
    if label == "administrative":
        return Turn(named_service=named)
    if label == "clinical":
        return Turn(named_service=named, requests_clinical_content=True)
    if label == "outside_admin":
        return Turn(named_service=named, outside_admin_rules=True)
    if label == "policy_decision":
        return Turn(named_service=named, requests_policy_decision=True)
    if label == "explicit_human":
        return Turn(named_service=named, explicit_human_request=True)
    if label == "accept_offer":
        return Turn(named_service=named, accepts_escalation_offer=True)
    if label == "distress":
        return Turn(named_service=named, expresses_distress=True)
    raise AssertionError(f"unknown label {label!r}")


@settings(max_examples=150)
@given(
    offered=st.lists(_service, unique=True, min_size=1, max_size=4),
    label=st.sampled_from(
        [
            "administrative",
            "clinical",
            "outside_admin",
            "policy_decision",
            "explicit_human",
            "accept_offer",
            "distress",
        ]
    ),
    patient_known=st.booleans(),
    context_text=_short_text,
    data=st.data(),
)
def test_property_13_escalation_classification_and_recording(
    offered: list[str],
    label: str,
    patient_known: bool,
    context_text: str,
    data: st.DataObject,
) -> None:
    # Feature: clinic-front-desk-agent, Property 13: For any patient turn
    # categorized as clinical content, outside administrative rules, explicit
    # human request, or accepted escalation offer, flag_for_human is invoked; and
    # for any escalation, the persisted record carries a reason drawn only from
    # {clinical_content, outside_admin_rules, patient_distress, patient_request}
    # together with the call-session context and the patient identity when known.
    # Validates: Requirements 9.1, 9.2, 9.4, 9.7, 9.8, 10.5
    policy = GuardrailPolicy(offered)
    named = data.draw(st.one_of(st.none(), st.sampled_from(offered)), label="named")
    turn = _build_turn(label, named)
    decision = policy.classify(turn)

    if label in _ESCALATING_LABELS:
        expected_reason = _ESCALATING_LABELS[label]
        # Classification requires flag_for_human now (Req 9.1, 9.2, 9.7, 9.8, 10.5).
        assert decision.requires_escalation is True
        assert decision.should_flag_for_human is True
        assert decision.escalation_reason == expected_reason

        # Faithful recording: invoking flag_for_human persists an escalation with
        # a valid reason, the call-session context, and identity when known (Req 9.4).
        store = MemoryEscalationStore()
        ref = (
            PatientRef(patient_id="p1", name="Bob", callback_phone="555-0100")
            if patient_known
            else None
        )
        recorded = flag_for_human(
            store,
            reason=decision.escalation_reason,
            call_session_id="cs-1",
            context=context_text,
            patient_ref=ref,
            escalation_id="esc-1",
            created_at="2025-01-01T00:00:00Z",
        )
        assert is_ok(recorded)
        escalation = recorded.value
        assert escalation.reason in _ALLOWED_REASONS
        assert escalation.reason == expected_reason
        assert escalation.call_session_id == "cs-1"
        assert escalation.context == context_text
        if patient_known:
            assert escalation.patient_ref == ref
        else:
            assert escalation.patient_ref is None
        # The escalation is durably persisted (surfaced to the activity log).
        listed = store.list_recent(10)
        assert is_ok(listed)
        assert any(e.id == "esc-1" for e in listed.value)

    elif label == "distress":
        # Distress alone is *offered* escalation, not an immediate flag (Req 9.3).
        assert decision.requires_escalation is False
        assert decision.offer_escalation is True
        assert decision.escalation_reason == EscalationReason.PATIENT_DISTRESS
        assert decision.classification == TurnClassification.PATIENT_DISTRESS
    else:  # administrative
        assert decision.is_administrative is True
        assert decision.requires_escalation is False


# ===========================================================================
# Property 16: Interpretation-failure retries are bounded then escalate
# (Req 12.4, 12.5)
# ===========================================================================


@settings(max_examples=150)
@given(runs=st.lists(st.integers(min_value=1, max_value=7), min_size=1, max_size=5))
def test_property_16_interpretation_failures_bounded_then_escalate(
    runs: list[int],
) -> None:
    # Feature: clinic-front-desk-agent, Property 16: For any run of consecutive
    # uninterpretable turns for the same request, the agent re-asks at most twice,
    # and upon the second consecutive failure it invokes flag_for_human; the
    # number of re-ask prompts never exceeds two.
    # Validates: Requirements 12.4, 12.5
    controller = TurnController()
    bound = TurnController.MAX_INTERPRETATION_ATTEMPTS

    for run in runs:
        # A successfully interpreted turn ends the previous "same request" run and
        # resets the bounds, so limits apply per request (Req 12.4).
        controller.on_interpretable_turn()

        actions = [controller.on_interpretation_failure() for _ in range(run)]
        reasks = [a for a in actions if isinstance(a, ReAsk)]
        escalates = [a for a in actions if isinstance(a, Escalate)]

        # Re-ask prompts never exceed the bound (Req 12.4).
        assert len(reasks) <= bound

        if run >= bound:
            # The second consecutive failure escalates via flag_for_human exactly
            # once (Req 12.5), and any further failure yields no further action.
            assert len(escalates) == 1
            assert isinstance(actions[bound - 1], Escalate)
            assert actions[bound - 1].inform_human_follow_up is True
            for later in actions[bound:]:
                assert isinstance(later, NoAction)
            assert controller.escalated is True
        else:
            # Below the bound: only re-asks, no escalation yet.
            assert escalates == []
            assert len(reasks) == run
            assert controller.escalated is False


# ===========================================================================
# Property 17: Barge-in preserves and resumes task step (Req 12.3)
# ===========================================================================


@settings(max_examples=150)
@given(
    initial_step=st.integers(min_value=0, max_value=20),
    during_steps=st.lists(st.integers(min_value=0, max_value=50), min_size=1, max_size=5),
    name=st.one_of(st.none(), _short_text),
    phone=st.one_of(st.none(), _short_text),
    service=st.one_of(st.none(), _service),
)
def test_property_17_barge_in_preserves_and_resumes_task_step(
    initial_step: int,
    during_steps: list[int],
    name: str | None,
    phone: str | None,
    service: str | None,
) -> None:
    # Feature: clinic-front-desk-agent, Property 17: For any task at any step, a
    # barge-in interruption preserves the accumulated session context and the
    # current step index, so the task resumes from its pre-interruption step
    # after the interruption is processed.
    # Validates: Requirements 12.3
    ctx = SessionContext(session_id="s")
    ctx.set_identity(name=name, callback_phone=phone)
    if service is not None:
        ctx.set_requested_service(service)
    ctx.set_step(initial_step)
    pre_facts = dict(ctx.known_facts())

    handler = BargeInHandler()

    # Simulate (possibly nested) barge-ins: each captures the current step, then
    # the interruption processing moves the step index arbitrarily.
    expected_steps: list[int] = []
    for step in during_steps:
        expected_steps.append(ctx.current_step_index)
        handler.on_barge_in(ctx)
        ctx.set_step(step)  # interruption processing moves the step

    assert handler.pending == len(during_steps)

    # Resuming in LIFO order restores each captured pre-interruption step (Req 12.3).
    for expected in reversed(expected_steps):
        handler.resume(ctx)
        assert ctx.current_step_index == expected

    # Fully unwound: the original pre-interruption task step is restored.
    assert ctx.current_step_index == initial_step
    assert handler.pending == 0

    # Accumulated context was preserved untouched throughout (Req 12.3).
    for fact, value in pre_facts.items():
        assert ctx.recall(fact) == value
    assert ctx.known_facts() == pre_facts


# ===========================================================================
# Property 8: Reschedule/cancel confirmation semantics preserve state
# (Req 4.5, 5.6)
# ===========================================================================


@settings(max_examples=150)
@given(
    provider_id=_ident,
    patient_id=_ident,
    service=_service,
    scenario=st.sampled_from(["cancel_declined", "reschedule_no_alt", "reschedule_declined"]),
)
def test_property_8_reschedule_cancel_confirmation_preserve_state(
    provider_id: str,
    patient_id: str,
    service: str,
    scenario: str,
) -> None:
    # Feature: clinic-front-desk-agent, Property 8: For any located appointment,
    # declining the cancellation confirmation leaves the appointment and its slot
    # unchanged, and finding no alternative slots during a reschedule leaves the
    # original appointment unchanged.
    # Validates: Requirements 4.5, 5.6
    store = MemoryAppointmentStore()
    store.seed_slot(
        Slot(
            id="slot1",
            provider_id=provider_id,
            service=service,
            start="2999-01-01T10:00:00",
            end="2999-01-01T10:30:00",
            status=SlotStatus.OPEN,
        )
    )
    store.seed_slot(
        Slot(
            id="slot2",
            provider_id=provider_id,
            service=service,
            start="2999-01-02T10:00:00",
            end="2999-01-02T10:30:00",
            status=SlotStatus.OPEN,
        )
    )
    booked = book_appointment(
        store,
        provider_id=provider_id,
        patient_id=patient_id,
        slot_id="slot1",
        service=service,
        appointment_id="appt1",
    )
    assert is_ok(booked)

    ctx = SessionContext(session_id="s")
    orchestrator = ToolOrchestrator(ctx)

    # The mutating tools are wrapped so we can prove they are never invoked on a
    # declined/no-alternative path (state is unchanged by construction).
    invocations = {"count": 0}

    def cancel_tool():  # type: ignore[no-untyped-def]
        invocations["count"] += 1
        return cancel(store, appointment_id="appt1")

    def reschedule_tool():  # type: ignore[no-untyped-def]
        invocations["count"] += 1
        return reschedule(store, appointment_id="appt1", new_slot_id="slot2")

    if scenario == "cancel_declined":
        outcome = orchestrator.cancel(confirmed=False, cancel_tool=cancel_tool)
        assert isinstance(outcome, MutationDeclined)
        assert outcome.action == "cancel"
    elif scenario == "reschedule_no_alt":
        outcome = orchestrator.reschedule(
            alternative_slots=[], confirmed=True, reschedule_tool=reschedule_tool
        )
        assert isinstance(outcome, NoAlternativeSlots)
    else:  # reschedule_declined
        outcome = orchestrator.reschedule(
            alternative_slots=["slot2"], confirmed=False, reschedule_tool=reschedule_tool
        )
        assert isinstance(outcome, MutationDeclined)
        assert outcome.action == "reschedule"

    # The mutating tool was never called on any of these paths (Req 4.5, 5.6).
    assert invocations["count"] == 0

    # Appointment and both slots are unchanged.
    stored = store.get("appt1")
    assert is_ok(stored) and stored.value is not None
    assert stored.value.slot_id == "slot1"
    assert stored.value.status == AppointmentStatus.BOOKED
    assert store.get_slot("slot1").unwrap().status == SlotStatus.BOOKED
    assert store.get_slot("slot2").unwrap().status == SlotStatus.OPEN
    # No terminal outcome was recorded for a non-mutating decision.
    assert ctx.outcome is None
