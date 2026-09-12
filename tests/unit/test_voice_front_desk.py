"""Unit tests for the end-to-end Voice_Front_Desk wiring (task 9.2).

These tests wire :class:`~clinic_front_desk.voice.agent.VoiceFrontDeskAgent`
against in-memory fake stores and a fake :class:`VoiceStream`, exercising the
composition without any real Nova Sonic / Bedrock connection. They assert:

- the nine patient-facing tools are registered (and the doctor-approved
  ``fill_gap_from_waitlist`` / autonomous ``analyze_patterns`` are not),
- the administrative-only guardrail system prompt is attached to the agent's
  ``BidiAgent`` (through the ``NovaSonicVoiceStream`` adapter),
- a booking task runs through the :class:`ToolOrchestrator` tool chain end to
  end (Req 2.4, 3.2, 11.2), retaining context on a mid-chain failure (Req 11.3),
- and the barge-in / interpreted-turn / turn-controller hooks are wired to the
  voice stream (Req 12.3, 12.5, 12.7).

No ``pytest-asyncio`` is available, so coroutines are driven with ``asyncio.run``
via :func:`_run` (mirroring ``tests/unit/test_voice_stream.py``).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from clinic_front_desk.data_layer.faults import fail_on, wrap
from clinic_front_desk.data_layer.memory import (
    MemoryAppointmentStore,
    MemoryCallSessionStore,
    MemoryClinicKnowledgeBaseStore,
    MemoryEscalationStore,
    MemoryPatientStore,
    MemoryWaitlistStore,
)
from clinic_front_desk.models import (
    CallOutcome,
    ClinicKnowledgeBase,
    DayHours,
    EscalationReason,
    Provider,
    ServiceConfig,
    Slot,
    SlotStatus,
    is_ok,
)
from clinic_front_desk.voice import (
    ADMINISTRATIVE_ONLY_SYSTEM_PROMPT,
    NovaSonicVoiceStream,
    VoiceFrontDeskAgent,
    VoiceFrontDeskStores,
    create_voice_front_desk_agent,
)
from clinic_front_desk.voice.agent import PATIENT_FACING_TOOL_NAMES
from clinic_front_desk.voice.guardrails import Turn
from clinic_front_desk.voice.stream import BargeInDetected, BargeInStopTiming, InterpretedTurn
from clinic_front_desk.voice.turn_controller import EndSession, Escalate, ReAsk

PROVIDER_ID = "prov-1"
SERVICE = "Hearing Test"


def _run(coro: Any) -> Any:
    """Drive a coroutine to completion (no pytest-asyncio in this project)."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fake voice stream (satisfies the VoiceStream Protocol)
# ---------------------------------------------------------------------------


class FakeVoiceStream:
    """A scripted :class:`VoiceStream` for driving the manager deterministically."""

    def __init__(self, script: list[Any] | None = None) -> None:
        self._script = script or []
        self.started = False
        self.closed = False
        self.stop_playback_calls = 0
        self.sent_text: list[str] = []

    async def start(self) -> None:
        self.started = True

    async def send_audio(
        self,
        audio: str,
        *,
        format: str = "pcm",
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> None:
        return None

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)

    async def stop_playback(self) -> None:
        self.stop_playback_calls += 1

    async def events(self) -> AsyncIterator[Any]:
        for event in self._script:
            yield event

    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Store setup helpers
# ---------------------------------------------------------------------------


def make_stores() -> VoiceFrontDeskStores:
    """A fresh bundle of in-memory fake stores."""
    return VoiceFrontDeskStores(
        appointments=MemoryAppointmentStore(),
        patients=MemoryPatientStore(),
        waitlist=MemoryWaitlistStore(),
        escalations=MemoryEscalationStore(),
        knowledge_base=MemoryClinicKnowledgeBaseStore(),
        call_sessions=MemoryCallSessionStore(),
    )


def seed_clinic(stores: VoiceFrontDeskStores, *, open_slots: int = 2) -> None:
    """Configure one provider + service and seed some open slots."""
    kb = ClinicKnowledgeBase(
        location="123 Main St",
        hours={day: DayHours(open="09:00", close="17:00") for day in range(1, 6)},
        services=[ServiceConfig(name=SERVICE, price=150.0)],
        accepted_insurance=["Aetna"],
        providers=[Provider(id=PROVIDER_ID, name="Dr. Ada", specialty="ENT")],
        configured=True,
        updated_at="2025-01-01T00:00:00Z",
    )
    stores.knowledge_base.save(kb)

    slots = [
        Slot(
            id=f"slot-{i}",
            provider_id=PROVIDER_ID,
            service=SERVICE,
            start=f"2999-01-0{i + 1}T10:00:00",
            end=f"2999-01-0{i + 1}T10:30:00",
            status=SlotStatus.OPEN,
        )
        for i in range(open_slots)
    ]
    assert isinstance(stores.appointments, MemoryAppointmentStore)
    stores.appointments.seed_slots(slots)


# ---------------------------------------------------------------------------
# Tool registration + guardrail prompt attachment
# ---------------------------------------------------------------------------


def test_the_patient_facing_tools_are_registered() -> None:
    """The agent registers exactly the declared patient-facing suite (task 9.2)."""
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())

    assert agent.tool_names == PATIENT_FACING_TOOL_NAMES
    assert set(agent.tool_names) == {
        "match_offered_service",
        "register_patient",
        "check_availability",
        # Without this, reschedule and cancel were unreachable: both need an
        # appointment id, and nothing could produce one from a name and a number.
        # On a live call the agent asked the caller for a "reference number" and
        # offered to pull up her appointment history, which it had no way to do.
        "list_appointments",
        "book_appointment",
        "reschedule",
        "cancel",
        "lookup_patient",
        "answer_faq",
        "add_to_waitlist",
        "flag_for_human",
    }


def test_doctor_and_autonomous_tools_not_registered() -> None:
    """``fill_gap_from_waitlist`` and ``analyze_patterns`` are not patient-facing."""
    stores = make_stores()
    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())
    assert "fill_gap_from_waitlist" not in agent.tool_names
    assert "analyze_patterns" not in agent.tool_names


def test_default_stream_attaches_prompt_and_tools_to_bidi_agent() -> None:
    """The default Nova Sonic stream registers the tools + guardrail prompt."""
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(stores)  # no stream -> default NovaSonicVoiceStream

    stream = agent._make_stream()
    assert isinstance(stream, NovaSonicVoiceStream)
    assert stream._system_prompt == ADMINISTRATIVE_ONLY_SYSTEM_PROMPT
    assert stream._tools == agent.tool_definitions
    assert {t.tool_name for t in stream._tools} == set(PATIENT_FACING_TOOL_NAMES)


def test_agent_exposes_administrative_only_system_prompt() -> None:
    stores = make_stores()
    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())
    assert agent.system_prompt == ADMINISTRATIVE_ONLY_SYSTEM_PROMPT


def test_factory_builds_equivalent_agent() -> None:
    stores = make_stores()
    seed_clinic(stores)
    agent = create_voice_front_desk_agent(stores, stream=FakeVoiceStream())
    assert isinstance(agent, VoiceFrontDeskAgent)
    assert agent.tool_names == PATIENT_FACING_TOOL_NAMES


# ---------------------------------------------------------------------------
# Bound tool definitions are callable and store-bound
# ---------------------------------------------------------------------------


def test_match_offered_service_tool_is_store_bound() -> None:
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())

    matched = agent.tools["match_offered_service"](SERVICE)
    assert matched == {"ok": True, "value": SERVICE}

    unknown = agent.tools["match_offered_service"]("Symptom Guess")
    assert unknown["ok"] is False
    assert unknown["error"]["kind"] == "not_offered"


# ---------------------------------------------------------------------------
# Booking flow through the orchestration (Req 2.4, 3.2, 11.2)
# ---------------------------------------------------------------------------


def test_booking_flow_runs_through_orchestration() -> None:
    """A full booking task chains lookup -> availability -> book (Req 11.2, 2.4, 3.2)."""
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())
    session = agent.start_session("call-1")

    outcome = session.book(
        named_service=SERVICE,
        name="Jane Doe",
        callback_phone="555-0100",
        provider_id=PROVIDER_ID,
    )

    assert outcome.kind == "chain_completed"
    # Every step produced an output, threaded forward.
    assert set(outcome.outputs) == {
        "match_offered_service",
        "lookup_patient",
        "check_availability",
        "book_appointment",
    }

    # Context retained the gathered facts for the session (Req 11.1, 3.2).
    assert session.context.requested_service == SERVICE
    assert session.context.patient_id is not None
    assert session.context.selected_slot_id == "slot-0"
    assert session.context.outcome == CallOutcome.BOOKED

    # A new patient was created (Req 3.4) and the slot is now booked (Req 2.5).
    booking = outcome.value
    appt = booking.appointment
    assert appt.service == SERVICE
    assert appt.provider_id == PROVIDER_ID
    slot = stores.appointments.get_slot("slot-0").value
    assert slot is not None and slot.status == SlotStatus.BOOKED


def test_booking_reuses_existing_patient() -> None:
    """An existing patient match informs the session rather than creating a new one."""
    stores = make_stores()
    seed_clinic(stores)
    # Pre-create a matching patient.
    from clinic_front_desk.models import Patient

    existing = stores.patients.create(
        Patient(id="pat-existing", name="Jane Doe", callback_phone="555-0100")
    ).value

    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())
    session = agent.start_session("call-2")
    outcome = session.book(
        named_service=SERVICE,
        name="Jane Doe",
        callback_phone="555-0100",
        provider_id=PROVIDER_ID,
    )

    assert outcome.kind == "chain_completed"
    assert session.context.patient_id == existing.id


def test_booking_mid_chain_failure_offers_message_and_retains_facts() -> None:
    """A persistence failure mid-chain retains gathered facts and offers a message (Req 11.3)."""
    stores = make_stores()
    seed_clinic(stores)
    # Force the appointment write to fail so book_appointment errors mid-chain.
    stores = VoiceFrontDeskStores(
        appointments=wrap(stores.appointments, fail_on("create")),
        patients=stores.patients,
        waitlist=stores.waitlist,
        escalations=stores.escalations,
        knowledge_base=stores.knowledge_base,
        call_sessions=stores.call_sessions,
    )
    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())
    session = agent.start_session("call-3")

    outcome = session.book(
        named_service=SERVICE,
        name="Jane Doe",
        callback_phone="555-0100",
        provider_id=PROVIDER_ID,
    )

    assert outcome.kind == "offer_to_take_message"
    assert outcome.failed_step == "book_appointment"
    # Facts gathered so far were retained (Req 11.3) — proof they were not cleared.
    assert outcome.retained_facts["requested_service"] == SERVICE
    assert outcome.retained_facts["selected_slot_id"] == "slot-0"
    assert "patient_id" in outcome.retained_facts
    # No booked outcome recorded on a failed chain.
    assert session.context.outcome is None


def test_booking_not_offered_service_stops_chain() -> None:
    """Naming an unoffered service stops the chain before any patient/slot writes."""
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())
    session = agent.start_session("call-4")

    outcome = session.book(
        named_service="Symptom Guess",
        name="Jane Doe",
        callback_phone="555-0100",
        provider_id=PROVIDER_ID,
    )
    assert outcome.kind == "offer_to_take_message"
    assert outcome.failed_step == "match_offered_service"
    assert session.context.requested_service is None


# ---------------------------------------------------------------------------
# Barge-in preserve-and-resume wiring (Req 12.3)
# ---------------------------------------------------------------------------


def test_barge_in_preserves_and_resumes_task_step() -> None:
    """A barge-in preserves the task step; the interruption's turn resumes it (Req 12.3)."""
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())
    session = agent.start_session("call-5")

    session.context.set_step(3)
    # Barge-in: the stream manager already stopped playback; the session captures
    # the current step.
    session._on_barge_in(BargeInStopTiming(latency_ms=50.0, within_budget=True, budget_ms=500.0))
    assert session.barge_in.pending == 1

    # Processing the interruption moves the step index.
    session.context.set_step(9)

    # The interruption's interpreted turn arrives -> resume to the pre-barge step.
    session._on_interpreted_turn(InterpretedTurn(text="wait, change that", role="user"))
    assert session.context.current_step_index == 3
    assert session.barge_in.pending == 0


def test_barge_in_resume_through_stream_manager() -> None:
    """Driving barge-in + interruption through the manager resumes the step (Req 12.3)."""
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(stores, stream=None, stream_factory=lambda: FakeVoiceStream(
        [BargeInDetected(reason="user_speech"), InterpretedTurn(text="sorry, go on", role="user")]
    ))
    session = agent.start_session("call-6")
    session.context.set_step(4)

    _run(session.start())
    _run(session.run())

    # The barge-in was captured and then resumed by the interrupting turn.
    assert session.barge_in.pending == 0
    assert session.context.current_step_index == 4


# ---------------------------------------------------------------------------
# Turn controller + escalation + finalize wiring (Req 12.4, 12.5, 12.7)
# ---------------------------------------------------------------------------


def test_interpreted_user_turn_resets_turn_controller() -> None:
    stores = make_stores()
    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())
    session = agent.start_session("call-7")

    # One failure, then a successful interpreted turn resets the counter.
    session.on_interpretation_failure()
    assert session.turn_controller.consecutive_interpretation_failures == 1
    session._on_interpreted_turn(InterpretedTurn(text="book me in", role="user"))
    assert session.turn_controller.consecutive_interpretation_failures == 0


def test_interpretation_failure_escalates_and_records() -> None:
    """The 2nd consecutive interpretation failure escalates via flag_for_human (Req 12.5)."""
    stores = make_stores()
    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())
    session = agent.start_session("call-8")

    first = session.on_interpretation_failure()
    assert isinstance(first, ReAsk)
    second = session.on_interpretation_failure()
    assert isinstance(second, Escalate)

    # An escalation was recorded through the escalation store (Req 9.4).
    recorded = stores.escalations.list_recent(10).value
    assert len(recorded) == 1
    assert recorded[0].call_session_id == "call-8"
    assert recorded[0].reason == EscalationReason.OUTSIDE_ADMIN_RULES


def test_voice_layer_lost_finalizes_interrupted() -> None:
    """Voice-layer loss ends the session and records it as interrupted (Req 12.7)."""
    stores = make_stores()
    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())
    session = agent.start_session("call-9")

    action = session.on_voice_layer_lost()
    assert isinstance(action, EndSession)
    assert action.outcome == CallOutcome.INTERRUPTED

    recent = stores.call_sessions.list_recent(10).value
    assert len(recent) == 1
    assert recent[0].id == "call-9"
    assert recent[0].outcome == CallOutcome.INTERRUPTED


# ---------------------------------------------------------------------------
# Guardrail wiring (Req 10)
# ---------------------------------------------------------------------------


def test_classify_turn_records_named_service() -> None:
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())
    session = agent.start_session("call-10")

    decision = session.classify_turn(Turn(named_service=SERVICE))
    assert decision.is_administrative is True
    assert session.context.requested_service == SERVICE


def test_classify_turn_escalates_clinical_content() -> None:
    """A clinical-content turn auto-escalates via flag_for_human (Req 10.1, 10.6)."""
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())
    session = agent.start_session("call-11")

    decision = session.classify_turn(Turn(requests_clinical_content=True))
    assert decision.requires_escalation is True

    recorded = stores.escalations.list_recent(10).value
    assert len(recorded) == 1
    assert recorded[0].reason == EscalationReason.CLINICAL_CONTENT
    assert recorded[0].call_session_id == "call-11"


def test_finalize_persists_outcome_and_identity() -> None:
    """Finalizing a booked session persists outcome + patient identity (Req 11.5)."""
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())
    session = agent.start_session("call-12")
    session.book(
        named_service=SERVICE,
        name="Jane Doe",
        callback_phone="555-0100",
        provider_id=PROVIDER_ID,
    )

    result = session.finalize()
    assert result.value.outcome == CallOutcome.BOOKED
    assert result.value.patient_ref is not None
    assert result.value.patient_ref.name == "Jane Doe"


# ---------------------------------------------------------------------------
# Guardrail wired into the live turn path (Req 9.1, 9.2, 9.3, 9.7, 9.8, 10.4, 10.5)
#
# These are the regression tests for a real integration gap: GuardrailPolicy and
# VoiceSession.classify_turn were both correct, but nothing connected them to the
# stream. `_on_interpreted_turn` — the only thing a live call invokes — never ran
# the guardrail, so on a real Nova Sonic call clinical content was declined only
# by the system prompt and no escalation was ever recorded. Verified against
# Bedrock: asking "my ear really hurts and I've been dizzy, what's wrong with me?"
# produced zero escalations in the Data_Layer.
# ---------------------------------------------------------------------------


def test_clinical_turn_from_the_stream_records_an_escalation() -> None:
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(
        stores,
        stream=None,
        stream_factory=lambda: FakeVoiceStream(
            [
                InterpretedTurn(
                    text="My ear really hurts, what is wrong with me?", role="user"
                )
            ]
        ),
    )
    session = agent.start_session("call-clinical")

    _run(session.start())
    _run(session.run())

    recorded = stores.escalations.list_recent(10)
    assert is_ok(recorded)
    assert len(recorded.value) == 1
    escalation = recorded.value[0]
    assert escalation.reason == EscalationReason.CLINICAL_CONTENT
    # The handover context names what was said and why it fired, so a human
    # picking it up does not have to guess.
    assert "what is wrong with me" in escalation.context
    assert "requests_clinical_content" in escalation.context


def test_symptom_only_turn_from_the_stream_escalates_and_selects_no_service() -> None:
    """Req 10.3, 10.4: routing a symptom would require interpreting it."""
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(
        stores,
        stream=None,
        stream_factory=lambda: FakeVoiceStream(
            [InterpretedTurn(text="I have been dizzy for three days", role="user")]
        ),
    )
    session = agent.start_session("call-symptom")

    _run(session.start())
    _run(session.run())

    assert session.context.requested_service is None
    recorded = stores.escalations.list_recent(10)
    assert is_ok(recorded)
    assert len(recorded.value) == 1


def test_administrative_turn_from_the_stream_records_no_escalation() -> None:
    """The precision half: a booking request must not be handed to a human."""
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(
        stores,
        stream=None,
        stream_factory=lambda: FakeVoiceStream(
            [
                InterpretedTurn(
                    text="I would like to book a hearing test", role="user"
                )
            ]
        ),
    )
    session = agent.start_session("call-admin")

    _run(session.start())
    _run(session.run())

    recorded = stores.escalations.list_recent(10)
    assert is_ok(recorded)
    assert recorded.value == []
    # The named offered service was still captured on the session (Req 2.1, 11.1).
    assert session.context.requested_service == SERVICE


def test_repeated_clinical_turns_record_one_escalation_per_reason() -> None:
    """A caller describing symptoms over several turns needs one handover."""
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(
        stores,
        stream=None,
        stream_factory=lambda: FakeVoiceStream(
            [
                InterpretedTurn(text="my ear hurts", role="user"),
                InterpretedTurn(text="it is really painful", role="user"),
                InterpretedTurn(text="do i need antibiotics", role="user"),
            ]
        ),
    )
    session = agent.start_session("call-repeat")

    _run(session.start())
    _run(session.run())

    recorded = stores.escalations.list_recent(10)
    assert is_ok(recorded)
    assert len(recorded.value) == 1


def test_escalated_call_finalizes_as_escalated_not_interrupted() -> None:
    """Req 11.5: `escalated` is a distinct outcome from `interrupted`."""
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(
        stores,
        stream=None,
        stream_factory=lambda: FakeVoiceStream(
            [InterpretedTurn(text="can i speak to a human please", role="user")]
        ),
    )
    session = agent.start_session("call-escalated")

    _run(session.start())
    _run(session.run())
    persisted = session.finalize(session.context.outcome)

    assert session.context.outcome == CallOutcome.ESCALATED
    assert is_ok(persisted)
    assert persisted.value.outcome == CallOutcome.ESCALATED


def test_distress_offers_escalation_then_a_yes_accepts_it() -> None:
    """Req 9.3 then 9.8: offer first, escalate only once the patient accepts."""
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())
    session = agent.start_session("call-distress")

    first = session.apply_guardrail("This is completely unacceptable")
    assert first is not None and first.offer_escalation is True
    assert first.requires_escalation is False
    assert stores.escalations.list_recent(10).value == []

    second = session.apply_guardrail("yes please")
    assert second is not None and second.requires_escalation is True

    recorded = stores.escalations.list_recent(10)
    assert is_ok(recorded)
    assert len(recorded.value) == 1
    assert recorded.value[0].reason == EscalationReason.PATIENT_DISTRESS


def test_bare_yes_without_an_outstanding_offer_does_not_escalate() -> None:
    """Otherwise every confirmation ("yes, 10am works") would page a human."""
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())
    session = agent.start_session("call-yes")

    decision = session.apply_guardrail("yes that time works for me")

    assert decision is not None
    assert decision.requires_escalation is False
    assert stores.escalations.list_recent(10).value == []


def test_assistant_turns_are_not_classified() -> None:
    """Only the patient's speech is guardrailed; the agent's own is not input."""
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())
    session = agent.start_session("call-assistant")

    session._on_interpreted_turn(
        InterpretedTurn(text="what is wrong with me", role="assistant")
    )

    assert session.last_guardrail_decision is None
    assert stores.escalations.list_recent(10).value == []


def test_empty_transcript_is_ignored() -> None:
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())
    session = agent.start_session("call-empty")

    assert session.apply_guardrail("   ") is None
    assert session.last_guardrail_decision is None


def test_yes_to_the_models_own_handover_offer_escalates() -> None:
    """Req 9.8 for an offer the *model* made, not one the guardrail produced.

    Observed live: the model asked "would you like me to connect you with a human
    representative?", the caller said yes, and nothing escalated — the guardrail
    only tracked offers it had generated itself. The agent's own transcript is now
    read to arm the acceptance path.
    """
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(
        stores,
        stream=None,
        stream_factory=lambda: FakeVoiceStream(
            [
                InterpretedTurn(
                    text="Would you like me to connect you with a human representative?",
                    role="assistant",
                ),
                InterpretedTurn(text="yes", role="user"),
            ]
        ),
    )
    session = agent.start_session("call-model-offer")

    _run(session.start())
    _run(session.run())

    recorded = stores.escalations.list_recent(10)
    assert is_ok(recorded)
    assert len(recorded.value) == 1
    assert session.context.outcome == CallOutcome.ESCALATED


def test_yes_after_an_ordinary_agent_reply_does_not_escalate() -> None:
    """Confirming a slot must not be read as accepting a handover."""
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(
        stores,
        stream=None,
        stream_factory=lambda: FakeVoiceStream(
            [
                InterpretedTurn(
                    text="Would you like me to look up the next available slots?",
                    role="assistant",
                ),
                InterpretedTurn(text="yes please", role="user"),
            ]
        ),
    )
    session = agent.start_session("call-ordinary-yes")

    _run(session.start())
    _run(session.run())

    assert stores.escalations.list_recent(10).value == []
    assert session.context.outcome is None


def test_connect_me_with_a_human_agent_escalates_through_the_stream() -> None:
    """Verbatim from the live call that exposed the recall gap."""
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(
        stores,
        stream=None,
        stream_factory=lambda: FakeVoiceStream(
            [
                InterpretedTurn(
                    text="yes, can you please connect with me human agent?",
                    role="user",
                )
            ]
        ),
    )
    session = agent.start_session("call-connect-human")

    _run(session.start())
    _run(session.run())

    recorded = stores.escalations.list_recent(10)
    assert is_ok(recorded)
    assert len(recorded.value) == 1
    assert recorded.value[0].reason == EscalationReason.PATIENT_REQUEST
    assert session.context.outcome == CallOutcome.ESCALATED


def test_escalation_is_deduped_across_the_guardrail_and_the_model() -> None:
    """One request for a human must produce one handover, not two.

    Both paths legitimately call flag_for_human — the deterministic backstop and
    the model choosing the tool itself. Observed live: a single "connect me with a
    human agent" recorded two escalations a second apart, one from each path. The
    dedupe therefore lives on the shared BoundToolset, so whichever fires first
    wins and the second returns the existing record.
    """
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())
    session = agent.start_session("call-dedupe")

    # The guardrail backstop escalates.
    session.apply_guardrail("can you connect me with a human agent")
    # Then the model calls the tool for the same call and reason.
    second = agent.toolset.flag_for_human(
        reason=EscalationReason.PATIENT_REQUEST,
        call_session_id="call-dedupe",
        context="model-initiated handover",
    )

    assert is_ok(second)
    recorded = stores.escalations.list_recent(10)
    assert is_ok(recorded)
    assert len(recorded.value) == 1


def test_the_model_and_the_guardrail_share_one_toolset() -> None:
    """The dedupe only holds if both paths go through the same instance."""
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())

    assert agent.start_session("s1").toolset is agent.toolset


def test_distinct_reasons_each_record_their_own_escalation() -> None:
    """Dedupe is per reason: a clinical question and a human request differ."""
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())

    agent.toolset.flag_for_human(
        reason=EscalationReason.CLINICAL_CONTENT,
        call_session_id="call-two-reasons",
        context="clinical",
    )
    agent.toolset.flag_for_human(
        reason=EscalationReason.PATIENT_REQUEST,
        call_session_id="call-two-reasons",
        context="asked for a human",
    )

    recorded = stores.escalations.list_recent(10)
    assert is_ok(recorded)
    assert len(recorded.value) == 2


def test_separate_calls_each_record_their_own_escalation() -> None:
    """Dedupe is per call: two callers asking for a human need two handovers."""
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())

    for session_id in ("call-a", "call-b"):
        agent.toolset.flag_for_human(
            reason=EscalationReason.PATIENT_REQUEST,
            call_session_id=session_id,
            context="asked for a human",
        )

    recorded = stores.escalations.list_recent(10)
    assert is_ok(recorded)
    assert len(recorded.value) == 2


def test_flag_for_human_tool_does_not_ask_the_model_for_the_session_id() -> None:
    """The model cannot know the Call_Session id, so it must not be a parameter.

    When it was, the model invented one: escalations were filed against a
    Call_Session that did not exist, could not be correlated in the activity log,
    and the per-call dedupe never matched — one request for a human produced two
    handovers, one per id.
    """
    import inspect

    stores = make_stores()
    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())
    tool_def = agent.tools["flag_for_human"]

    func = getattr(tool_def, "_tool_func", None) or getattr(
        tool_def, "__wrapped__", tool_def
    )
    params = set(inspect.signature(func).parameters)

    assert "call_session_id" not in params
    assert {"reason", "context"} <= params


def test_model_initiated_escalation_lands_on_the_active_call_session() -> None:
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())
    agent.start_session("call-bound")

    result = agent.toolset.flag_for_human(
        reason=EscalationReason.PATIENT_REQUEST,
        call_session_id=agent.toolset.session_id,
        context="model-initiated handover",
    )

    assert is_ok(result)
    recorded = stores.escalations.list_recent(10)
    assert is_ok(recorded)
    assert [e.call_session_id for e in recorded.value] == ["call-bound"]


def test_starting_a_session_rebinds_the_toolset() -> None:
    """A reused agent must not file a second call's escalations against the first."""
    stores = make_stores()
    seed_clinic(stores)
    agent = VoiceFrontDeskAgent(stores, stream=FakeVoiceStream())

    agent.start_session("first")
    assert agent.toolset.session_id == "first"
    agent.start_session("second")
    assert agent.toolset.session_id == "second"
