"""Unit tests for the Voice_Front_Desk guardrails (task 7.3).

Covers the two halves of the administrative-only defense-in-depth guardrail
(design "Guardrail enforcement"):

- The prompt-layer guardrail: the administrative-only system prompt forbids
  clinical content and fixes routing to patient-named services (Req 10.1, 10.6).
- The tool-layer guardrail: ``GuardrailPolicy`` classifies each turn, selects a
  service only when an offered service is explicitly named, never infers a
  service from a symptom, and signals escalation with the correct
  ``EscalationReason`` (Req 10.1–10.6, plus the 9.x escalation categories the
  classifier drives).

These are focused example/edge tests. The symptom-never-infers-service property
test lives in task 7.4 and the escalation-classification property test in 7.13.
"""

from __future__ import annotations

import pytest

from clinic_front_desk.models import EscalationReason
from clinic_front_desk.voice.guardrails import (
    GuardrailPolicy,
    Turn,
    TurnClassification,
)
from clinic_front_desk.voice.prompts import ADMINISTRATIVE_ONLY_SYSTEM_PROMPT

OFFERED = ("hearing test", "sinus consultation", "ear cleaning")


# ---------------------------------------------------------------------------
# Prompt-layer guardrail (Req 10.1, 10.6)
# ---------------------------------------------------------------------------


def test_system_prompt_forbids_clinical_content() -> None:
    """Req 10.1: the prompt forbids clinical advice/triage/diagnosis/treatment/
    medication."""
    prompt = ADMINISTRATIVE_ONLY_SYSTEM_PROMPT.lower()
    for forbidden in ("clinical advice", "triage", "diagnosis", "treatment", "medication"):
        assert forbidden in prompt


def test_system_prompt_fixes_routing_to_named_service() -> None:
    """Req 10.2/10.6: the prompt instructs routing only by a patient-named
    service and never by interpreting a symptom."""
    prompt = ADMINISTRATIVE_ONLY_SYSTEM_PROMPT.lower()
    assert "explicitly names" in prompt
    assert "symptom" in prompt
    # It states clinical questions are handled by clinic staff (Req 10.6).
    assert "clinic staff" in prompt


# ---------------------------------------------------------------------------
# Service resolution (Req 2.1, 10.2, 10.3)
# ---------------------------------------------------------------------------


def test_named_offered_service_is_selected() -> None:
    """Req 2.1/10.2: an exactly-named offered service is selected."""
    policy = GuardrailPolicy(OFFERED)
    assert policy.resolve_named_service("hearing test") == "hearing test"


def test_unoffered_named_service_selects_nothing() -> None:
    """Req 2.9: a named service that is not offered selects nothing."""
    policy = GuardrailPolicy(OFFERED)
    assert policy.resolve_named_service("foot massage") is None
    assert policy.resolve_named_service(None) is None


# ---------------------------------------------------------------------------
# Turn classification
# ---------------------------------------------------------------------------


def test_administrative_turn_selects_service_and_does_not_escalate() -> None:
    """A plain booking request naming an offered service is administrative."""
    policy = GuardrailPolicy(OFFERED)
    decision = policy.classify(Turn(named_service="hearing test"))

    assert decision.classification is TurnClassification.ADMINISTRATIVE
    assert decision.is_administrative is True
    assert decision.requires_escalation is False
    assert decision.should_flag_for_human is False
    assert decision.escalation_reason is None
    assert decision.selected_service == "hearing test"


def test_symptom_plus_named_service_routes_by_named_service() -> None:
    """Req 10.2: when a symptom AND an offered service are named, route by the
    named service; no escalation, service selected."""
    policy = GuardrailPolicy(OFFERED)
    decision = policy.classify(Turn(named_service="hearing test", names_symptom=True))

    assert decision.classification is TurnClassification.ADMINISTRATIVE
    assert decision.requires_escalation is False
    assert decision.selected_service == "hearing test"


def test_symptom_only_infers_no_service_and_escalates() -> None:
    """Req 10.3/10.4: a symptom with no named offered service selects no service
    and escalates via flag_for_human as clinical content."""
    policy = GuardrailPolicy(OFFERED)
    decision = policy.classify(Turn(names_symptom=True))

    assert decision.classification is TurnClassification.SYMPTOM_ROUTING
    assert decision.is_administrative is False
    assert decision.selected_service is None
    assert decision.requires_escalation is True
    assert decision.should_flag_for_human is True
    assert decision.escalation_reason is EscalationReason.CLINICAL_CONTENT
    assert decision.decline_clinical_content is True


def test_symptom_with_unoffered_service_escalates_without_selecting() -> None:
    """Req 10.4: a symptom plus a non-offered service still cannot be routed —
    no service is selected and it escalates."""
    policy = GuardrailPolicy(OFFERED)
    decision = policy.classify(Turn(named_service="foot massage", names_symptom=True))

    assert decision.classification is TurnClassification.SYMPTOM_ROUTING
    assert decision.selected_service is None
    assert decision.requires_escalation is True


def test_clinical_content_declined_and_escalated() -> None:
    """Req 10.1/10.6: a clinical-advice request is declined and escalated as
    clinical content, even if an offered service is also named."""
    policy = GuardrailPolicy(OFFERED)
    decision = policy.classify(
        Turn(named_service="hearing test", requests_clinical_content=True)
    )

    assert decision.classification is TurnClassification.CLINICAL_CONTENT
    assert decision.decline_clinical_content is True
    assert decision.requires_escalation is True
    assert decision.escalation_reason is EscalationReason.CLINICAL_CONTENT
    # Req 10.2: a named offered service is still selected.
    assert decision.selected_service == "hearing test"


def test_policy_decision_escalates_as_outside_admin_rules() -> None:
    """Req 10.5: a clinical/policy decision escalates as outside_admin_rules."""
    policy = GuardrailPolicy(OFFERED)
    decision = policy.classify(Turn(requests_policy_decision=True))

    assert decision.classification is TurnClassification.OUTSIDE_ADMIN_RULES
    assert decision.requires_escalation is True
    assert decision.escalation_reason is EscalationReason.OUTSIDE_ADMIN_RULES


def test_out_of_rules_request_escalates() -> None:
    """Req 9.2: a request outside the administrative rules escalates."""
    policy = GuardrailPolicy(OFFERED)
    decision = policy.classify(Turn(outside_admin_rules=True))

    assert decision.requires_escalation is True
    assert decision.escalation_reason is EscalationReason.OUTSIDE_ADMIN_RULES


def test_explicit_human_request_escalates_as_patient_request() -> None:
    """Req 9.7: an explicit request for a human escalates as patient_request."""
    policy = GuardrailPolicy(OFFERED)
    decision = policy.classify(Turn(explicit_human_request=True))

    assert decision.classification is TurnClassification.HUMAN_REQUEST
    assert decision.requires_escalation is True
    assert decision.escalation_reason is EscalationReason.PATIENT_REQUEST


def test_distress_offers_escalation_without_flagging_immediately() -> None:
    """Req 9.3: distress is offered an escalation, not escalated immediately."""
    policy = GuardrailPolicy(OFFERED)
    decision = policy.classify(Turn(expresses_distress=True))

    assert decision.classification is TurnClassification.PATIENT_DISTRESS
    assert decision.requires_escalation is False
    assert decision.should_flag_for_human is False
    assert decision.offer_escalation is True
    assert decision.escalation_reason is EscalationReason.PATIENT_DISTRESS


def test_accepted_escalation_offer_flags_for_human() -> None:
    """Req 9.8: accepting an escalation offer escalates now."""
    policy = GuardrailPolicy(OFFERED)
    decision = policy.classify(Turn(accepts_escalation_offer=True))

    assert decision.classification is TurnClassification.HUMAN_REQUEST
    assert decision.requires_escalation is True
    assert decision.escalation_reason is EscalationReason.PATIENT_DISTRESS


def test_clinical_content_takes_precedence_over_symptom_routing() -> None:
    """Clinical-content refusal is safety-first and outranks other signals."""
    policy = GuardrailPolicy(OFFERED)
    decision = policy.classify(
        Turn(names_symptom=True, requests_clinical_content=True, explicit_human_request=True)
    )

    assert decision.classification is TurnClassification.CLINICAL_CONTENT
    assert decision.escalation_reason is EscalationReason.CLINICAL_CONTENT


@pytest.mark.parametrize(
    "turn, expects_escalation",
    [
        (Turn(named_service="ear cleaning"), False),
        (Turn(names_symptom=True), True),
        (Turn(requests_clinical_content=True), True),
        (Turn(explicit_human_request=True), True),
        (Turn(requests_policy_decision=True), True),
    ],
)
def test_requires_escalation_matches_classification(turn: Turn, expects_escalation: bool) -> None:
    policy = GuardrailPolicy(OFFERED)
    decision = policy.classify(turn)
    assert decision.requires_escalation is expects_escalation
    # should_flag_for_human is the tool-layer alias of requires_escalation.
    assert decision.should_flag_for_human is expects_escalation


# ---------------------------------------------------------------------------
# A caller who cannot name a service is offered the consultation, not a human
# ---------------------------------------------------------------------------
#
# Observed live. A caller said "there is some kind of itching inside my nose,
# which kind of service should I prefer" and the agent replied that it could not
# advise and offered to connect her to a human. Every word of that was within the
# rules and it was still the wrong outcome: the clinic had 22 open slots that day
# and she was sent away.
#
# The line that matters is between two different things:
#   * "itching, therefore Allergy Testing" is triage. Forbidden, permanently. The
#     wrong test delays a real diagnosis.
#   * "I can't advise, but the consultation is where the doctor decides" is not.
#     It is the same answer for every symptom, so it conveys nothing clinical.
#
# The service is still never *selected* by the agent — it is offered, and the
# caller has to accept, at which point they have named it themselves.

GENERAL = "ENT Consultation"
WITH_CONSULT = (GENERAL, "Hearing Test", "Sinus Treatment")


def _policy_with_consultation() -> GuardrailPolicy:
    return GuardrailPolicy(WITH_CONSULT, general_consultation=GENERAL)


def test_a_symptom_offers_the_consultation_instead_of_a_human() -> None:
    decision = _policy_with_consultation().classify(Turn(names_symptom=True))

    assert decision.classification is TurnClassification.UNSURE_WHICH_SERVICE
    assert decision.offer_general_consultation == GENERAL
    assert decision.requires_escalation is False
    assert decision.should_flag_for_human is False
    # Still declines the clinical part, and still selects nothing.
    assert decision.decline_clinical_content is True
    assert decision.selected_service is None


def test_asking_which_service_offers_the_consultation() -> None:
    decision = _policy_with_consultation().classify(Turn(asks_which_service=True))

    assert decision.classification is TurnClassification.UNSURE_WHICH_SERVICE
    assert decision.offer_general_consultation == GENERAL
    assert decision.requires_escalation is False


def test_the_agent_never_selects_the_consultation_itself() -> None:
    """It is offered. The caller has to name it, or Req 10.2 is broken."""
    decision = _policy_with_consultation().classify(
        Turn(names_symptom=True, asks_which_service=True)
    )

    assert decision.selected_service is None


def test_a_named_service_still_wins_over_the_offer() -> None:
    """Someone who knows what they want is not talked into a consultation."""
    decision = _policy_with_consultation().classify(
        Turn(named_service="Hearing Test", names_symptom=True)
    )

    assert decision.selected_service == "Hearing Test"
    assert decision.offer_general_consultation is None
    assert decision.classification is TurnClassification.ADMINISTRATIVE


def test_a_clinical_question_still_escalates_even_with_a_consultation_configured() -> None:
    """"Is this serious" is triage. No consultation offer substitutes for that."""
    decision = _policy_with_consultation().classify(
        Turn(names_symptom=True, requests_clinical_content=True)
    )

    assert decision.classification is TurnClassification.CLINICAL_CONTENT
    assert decision.requires_escalation is True
    assert decision.escalation_reason is EscalationReason.CLINICAL_CONTENT
    assert decision.offer_general_consultation is None


def test_an_upset_caller_with_a_symptom_is_still_offered_a_human() -> None:
    """Distress outranks the consultation offer: they want a person, not a form."""
    decision = _policy_with_consultation().classify(
        Turn(names_symptom=True, expresses_distress=True)
    )

    assert decision.classification is TurnClassification.PATIENT_DISTRESS
    assert decision.offer_escalation is True
    assert decision.offer_general_consultation is None


def test_asking_for_a_human_with_a_symptom_still_escalates() -> None:
    decision = _policy_with_consultation().classify(
        Turn(names_symptom=True, explicit_human_request=True)
    )

    assert decision.requires_escalation is True
    assert decision.escalation_reason is EscalationReason.PATIENT_REQUEST


def test_without_a_configured_consultation_it_falls_back_to_escalating() -> None:
    """The safe default. A clinic with no consultation has nothing to offer."""
    decision = GuardrailPolicy(("Hearing Test",)).classify(Turn(names_symptom=True))

    assert decision.classification is TurnClassification.SYMPTOM_ROUTING
    assert decision.requires_escalation is True
    assert decision.offer_general_consultation is None


def test_a_consultation_the_clinic_does_not_offer_is_ignored() -> None:
    """An offer the clinic cannot honour is worse than no offer at all."""
    policy = GuardrailPolicy(("Hearing Test",), general_consultation="ENT Consultation")

    assert policy.general_consultation is None
    assert policy.classify(Turn(names_symptom=True)).requires_escalation is True
