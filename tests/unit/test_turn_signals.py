"""Unit tests for transcript → guardrail signal extraction (Req 9.x, 10.x).

Two things have to hold at once, and they pull in opposite directions:

- **Recall.** Clinical questions, symptom descriptions, distress, and human
  requests must be detected, because this is the layer that guarantees an
  escalation is recorded regardless of what the model decides to do.
- **Precision.** Ordinary bookings and FAQ questions must *not* escalate. An
  over-eager matcher is not "safely conservative" — it hands every caller to a
  human and makes the agent pointless.

The admin-phrase tests below are the precision half, and they are the ones that
would catch a regression from someone loosening a pattern.
"""

from __future__ import annotations

import pytest

from clinic_front_desk.models import EscalationReason
from clinic_front_desk.voice.guardrails import GuardrailPolicy, TurnClassification
from clinic_front_desk.voice.turn_signals import (
    extract_turn,
    normalize_transcript,
    offers_escalation,
)

OFFERED = ("Hearing Test", "Sinus Consultation", "Allergy Screening")


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("What's wrong with me?", "whats wrong with me"),
        ("I  can't   hear.", "i cant hear"),
        ("Hello — my EAR hurts!", "hello my ear hurts"),
        ("It\u2019s an emergency", "its an emergency"),
        ("", ""),
    ],
)
def test_normalize_transcript(raw: str, expected: str) -> None:
    assert normalize_transcript(raw) == expected


# ---------------------------------------------------------------------------
# Precision: administrative turns must stay administrative
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "transcript",
    [
        "What are your clinic hours?",
        "Where are you located?",
        "How much is a hearing test?",
        "I would like to book a hearing test",
        "Can I book a sinus consultation for next Tuesday?",
        "I need to reschedule my appointment",
        "Please cancel my appointment on Friday",
        "Do I need to bring anything with me?",
        "What should I bring to my appointment?",
        "Do you accept Acme Health?",
        "Can you put me on the waitlist?",
        "My name is Dana Ellis and my number is 555 0100",
        "What time is my appointment?",
        "Is there anything I should do to prepare?",
    ],
)
def test_administrative_turns_do_not_escalate(transcript: str) -> None:
    """The agent must handle these itself — escalating them would be a failure."""
    decision = GuardrailPolicy(OFFERED).classify(
        extract_turn(transcript, OFFERED).turn
    )

    assert decision.is_administrative is True, transcript
    assert decision.requires_escalation is False, transcript
    assert decision.offer_escalation is False, transcript


def test_booking_a_hearing_test_selects_the_service_without_escalating() -> None:
    """"hearing" must not read as a symptom just because "hearing loss" does."""
    extracted = extract_turn("I would like to book a hearing test", OFFERED)

    assert extracted.turn.named_service == "Hearing Test"
    assert extracted.turn.names_symptom is False
    decision = GuardrailPolicy(OFFERED).classify(extracted.turn)
    assert decision.selected_service == "Hearing Test"
    assert decision.requires_escalation is False


def test_service_name_is_matched_regardless_of_transcription_casing() -> None:
    extracted = extract_turn("id like to book a HEARING TEST please", OFFERED)

    assert extracted.turn.named_service == "Hearing Test"


def test_longer_service_names_win_over_shorter_ones() -> None:
    extracted = extract_turn(
        "can i book an allergy screening", ("Allergy", "Allergy Screening")
    )

    assert extracted.turn.named_service == "Allergy Screening"


# ---------------------------------------------------------------------------
# Recall: clinical content (Req 10.1, 10.6, 9.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "transcript",
    [
        "What's wrong with me?",
        "My ear hurts, what is wrong with me?",
        "Is this serious?",
        "Do I need surgery?",
        "Do I need antibiotics for this?",
        "What treatment do I need?",
        "Can you prescribe me something?",
        "Should I take ibuprofen for the pain?",
        "What dose should I take?",
        "How do I stop the ringing?",
        "Is it safe to fly with a blocked ear?",
        "What could it be?",
        "Should I be worried?",
        "What are the side effects?",
    ],
)
def test_clinical_requests_decline_and_escalate(transcript: str) -> None:
    decision = GuardrailPolicy(OFFERED).classify(
        extract_turn(transcript, OFFERED).turn
    )

    assert decision.requires_escalation is True, transcript
    assert decision.should_flag_for_human is True, transcript
    assert decision.decline_clinical_content is True, transcript
    assert decision.escalation_reason == EscalationReason.CLINICAL_CONTENT, transcript


@pytest.mark.parametrize(
    "transcript",
    [
        "It's an emergency, I can't breathe",
        "I have chest pain",
        "My ear won't stop bleeding",
        "I think I need an ambulance",
    ],
)
def test_emergencies_escalate_immediately_as_clinical_content(transcript: str) -> None:
    """An emergency must escalate now, not merely be *offered* an escalation."""
    decision = GuardrailPolicy(OFFERED).classify(
        extract_turn(transcript, OFFERED).turn
    )

    assert decision.requires_escalation is True, transcript
    assert decision.classification == TurnClassification.CLINICAL_CONTENT, transcript
    assert decision.offer_escalation is False, transcript


# ---------------------------------------------------------------------------
# Recall: symptom routing (Req 10.3, 10.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "transcript",
    [
        "My ear really hurts",
        "I've been dizzy for three days",
        "I have a sore throat",
        "I can't hear out of my left ear",
        "There's a ringing in my ears",
        "My sinuses hurt and my nose is blocked",
        "I have hearing loss",
    ],
)
def test_symptom_only_never_infers_a_service_and_escalates(transcript: str) -> None:
    """Req 10.3: routing from a symptom would require interpreting it."""
    extracted = extract_turn(transcript, OFFERED)
    decision = GuardrailPolicy(OFFERED).classify(extracted.turn)

    assert extracted.turn.names_symptom is True, transcript
    assert decision.selected_service is None, transcript
    assert decision.requires_escalation is True, transcript


def test_symptom_plus_named_service_books_rather_than_escalating() -> None:
    """A caller may explain why *and* name the service; the name is what routes."""
    extracted = extract_turn(
        "My ear hurts so I would like to book a hearing test", OFFERED
    )
    decision = GuardrailPolicy(OFFERED).classify(extracted.turn)

    assert extracted.turn.names_symptom is True
    assert decision.selected_service == "Hearing Test"
    assert decision.requires_escalation is False


# ---------------------------------------------------------------------------
# Recall: human requests, distress, policy, out-of-rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "transcript",
    [
        "Can I speak to a human?",
        "I want to talk to someone",
        "Please transfer me",
        "Put me through to the doctor",
        "Can I speak to a real person",
        # Every phrasing below was missed by the first, hand-listed pattern set.
        # The first one is verbatim from a live call: the model *said* it had
        # escalated, the backstop stayed silent, and the call ended `interrupted`.
        "yes, can you please connect with me human agent?",
        "can you connect me with a human agent",
        "connect me to a human",
        "I want to speak with a human agent",
        "can I get a human agent",
        "human agent please",
        "I need a human",
        "let me speak with an agent",
        "can you connect me to someone",
        "I'd like to speak to a representative",
        "please escalate me",
        "can someone call me back",
    ],
)
def test_explicit_human_request_escalates(transcript: str) -> None:
    decision = GuardrailPolicy(OFFERED).classify(
        extract_turn(transcript, OFFERED).turn
    )

    assert decision.requires_escalation is True, transcript
    assert decision.escalation_reason == EscalationReason.PATIENT_REQUEST, transcript


@pytest.mark.parametrize(
    "transcript",
    [
        "This is unacceptable",
        "I am absolutely furious about this",
        "I'm really frustrated with your clinic",
        "This is ridiculous",
    ],
)
def test_distress_offers_escalation_rather_than_escalating(transcript: str) -> None:
    """Req 9.3: distress is *offered* a handover, not forced into one."""
    decision = GuardrailPolicy(OFFERED).classify(
        extract_turn(transcript, OFFERED).turn
    )

    assert decision.offer_escalation is True, transcript
    assert decision.requires_escalation is False, transcript
    assert decision.escalation_reason == EscalationReason.PATIENT_DISTRESS, transcript


def test_acceptance_only_counts_when_an_offer_is_outstanding() -> None:
    """A bare "yes" otherwise just agrees with whatever was asked (Req 9.8)."""
    without_offer = extract_turn("yes please", OFFERED, escalation_offered=False)
    with_offer = extract_turn("yes please", OFFERED, escalation_offered=True)

    assert without_offer.turn.accepts_escalation_offer is False
    assert with_offer.turn.accepts_escalation_offer is True
    decision = GuardrailPolicy(OFFERED).classify(with_offer.turn)
    assert decision.requires_escalation is True


@pytest.mark.parametrize(
    ("transcript", "reason"),
    [
        ("Can you make an exception for me?", EscalationReason.OUTSIDE_ADMIN_RULES),
        # "Which test do I need?" deliberately no longer belongs here — it is a
        # routine front-desk question, answered by offering the consultation.
        ("I want a refund", EscalationReason.OUTSIDE_ADMIN_RULES),
        ("Can I get my test results?", EscalationReason.OUTSIDE_ADMIN_RULES),
        ("I need a sick note", EscalationReason.OUTSIDE_ADMIN_RULES),
        ("I'd like to make a complaint", EscalationReason.OUTSIDE_ADMIN_RULES),
    ],
)
def test_policy_and_out_of_rules_requests_escalate(
    transcript: str, reason: EscalationReason
) -> None:
    decision = GuardrailPolicy(OFFERED).classify(
        extract_turn(transcript, OFFERED).turn
    )

    assert decision.requires_escalation is True, transcript
    assert decision.escalation_reason == reason, transcript


@pytest.mark.parametrize(
    "transcript",
    [
        "Would you like me to connect you with a human representative?",
        "I can connect you to a member of staff",
        "Would you like me to have a nurse call you?",
        "Shall I transfer you to the receptionist?",
        "I'll escalate this to a human agent",
        "I can take a message for you",
    ],
)
def test_agent_offers_of_a_handover_are_recognized(transcript: str) -> None:
    """So a caller's "yes" to the *model's own* offer still escalates (Req 9.8).

    Observed live: the model offered a handover on its own initiative, the caller
    said "yes", and nothing escalated because the guardrail had no record of an
    outstanding offer.
    """
    assert offers_escalation(transcript) is True, transcript


@pytest.mark.parametrize(
    "transcript",
    [
        "Our clinic hours are Monday to Friday, 9am to 5pm",
        "The cost for a Hearing Test is $180.00",
        "Would you like me to look up the next available slots?",
        "I have booked you in for Tuesday at 11:30",
    ],
)
def test_ordinary_agent_replies_are_not_read_as_offers(transcript: str) -> None:
    """Otherwise any following "yes" would escalate a normal booking."""
    assert offers_escalation(transcript) is False, transcript


def test_choosing_between_services_is_its_own_signal_not_a_policy_decision() -> None:
    """"Which test do I need" is the most ordinary question a front desk gets.

    Grouped with policy decisions — waivers, discounts, insurance rulings — it
    escalated to a human, which turned a routine question into a handover. It is
    answerable without interpreting anything, by offering the general consultation,
    so it carries its own signal.
    """
    extracted = extract_turn("which test do i need for my ear", OFFERED)

    assert extracted.turn.asks_which_service is True
    assert extracted.turn.requests_policy_decision is False
    # And the evidence still names the phrase, for the handover if one happens.
    assert "asks_which_service" in extracted.evidence


@pytest.mark.parametrize(
    "transcript",
    [
        "which service should I book",
        "what kind of appointment do I need",
        "I'm not sure which one to pick",
        "I don't know which service",
        "can you recommend a service",
        "help me choose",
        "which would you recommend",
    ],
)
def test_a_caller_who_cannot_choose_is_recognised(transcript: str) -> None:
    assert extract_turn(transcript, OFFERED).turn.asks_which_service is True, transcript


@pytest.mark.parametrize(
    "transcript",
    [
        "I would like to book a Hearing Test",
        "can I move my appointment to Tuesday",
        "what are your opening hours",
        "cancel my appointment please",
    ],
)
def test_ordinary_requests_do_not_read_as_being_unsure(transcript: str) -> None:
    """Precision: a caller who knows what they want must not trigger the offer."""
    assert extract_turn(transcript, OFFERED).turn.asks_which_service is False, transcript


# ---------------------------------------------------------------------------
# Evidence, for the human picking up the handover
# ---------------------------------------------------------------------------


def test_evidence_records_the_phrase_that_triggered_each_signal() -> None:
    extracted = extract_turn("my ear hurts, do i need antibiotics?", OFFERED)

    assert extracted.turn.requests_clinical_content is True
    assert "requests_clinical_content" in extracted.evidence
    assert extracted.evidence["names_symptom"] == "hurts"
    described = extracted.describe()
    assert "do i need antibiotics" in described
    assert "names_symptom='hurts'" in described


def test_administrative_turn_has_no_evidence() -> None:
    extracted = extract_turn("what are your hours", OFFERED)

    assert extracted.matched_any is False
    assert extracted.evidence == {}


def test_empty_transcript_yields_an_administrative_turn() -> None:
    extracted = extract_turn("", OFFERED)

    assert extracted.matched_any is False
    assert GuardrailPolicy(OFFERED).classify(extracted.turn).is_administrative is True
