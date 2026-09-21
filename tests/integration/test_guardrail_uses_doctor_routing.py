"""The guardrail must answer a symptom with the doctor's rule, not a generic offer.

This closes a gap that made the routing feature look finished while doing nothing on
a real call. The symptom-routing tool existed and was tested, but the guardrail
intercepts symptom turns *before* the model gets a say — and it answered every symptom
with the same general consultation, or escalated. So the doctor could write "itching in
nose -> ENT Consultation, and here is what to tell them" and a caller describing
exactly that would still hear the generic answer.

The fix routes the doctor's rules into the policy as a turn *signal*, because the
policy decides over structured signals and never over raw text. These tests pin the
resulting order of preference:

1. a named service always wins — a caller who asked for a hearing test gets one;
2. then an urgent rule, which forbids offering any appointment;
3. then the doctor's specific route, with her wording;
4. then the generic consultation;
5. and with nothing configured, exactly the old behaviour.
"""

from __future__ import annotations

from clinic_front_desk.models import SymptomRoute
from clinic_front_desk.voice.guardrails import GuardrailPolicy
from clinic_front_desk.voice.turn_signals import extract_turn

ENT = "ENT Consultation"
HEARING = "Hearing Test"
OFFERED = (ENT, HEARING)

NOSE = SymptomRoute(
    phrases=["itching in nose", "blocked nose"],
    service=ENT,
    advice="Dr Raana sees nasal irritation under an ENT consultation.",
)
REDUCED_HEARING = SymptomRoute(
    phrases=["cannot hear properly"],
    service=HEARING,
    advice="The clinic starts with a hearing test.",
)
URGENT = SymptomRoute(
    phrases=["sudden hearing loss"],
    urgent=True,
    urgent_instruction="Please come in today, or go to a hospital if you cannot.",
)


def _decide(
    said: str,
    routes: tuple[SymptomRoute, ...] = (),
    *,
    general: str | None = ENT,
) -> object:
    policy = GuardrailPolicy(OFFERED, general_consultation=general)
    extracted = extract_turn(said, OFFERED, symptom_routes=list(routes))
    return policy.classify(extracted.turn)


# -- the gap this fixes -----------------------------------------------------


def test_a_routed_symptom_gets_the_doctors_service() -> None:
    decision = _decide("there is some kind of itching in nose", (REDUCED_HEARING, NOSE))

    assert decision.offer_general_consultation == ENT  # type: ignore[attr-defined]
    assert decision.routed_advice == NOSE.advice  # type: ignore[attr-defined]
    assert decision.requires_escalation is False  # type: ignore[attr-defined]


def test_a_routed_symptom_can_reach_a_different_service() -> None:
    """Proves it is really routing and not just returning the general consultation."""
    decision = _decide("I cannot hear properly on the left", (REDUCED_HEARING, NOSE))

    assert decision.offer_general_consultation == HEARING  # type: ignore[attr-defined]
    assert "hearing test" in decision.routed_advice.lower()  # type: ignore[attr-defined]


def test_without_routing_every_symptom_got_the_same_answer() -> None:
    """The old behaviour, kept as the fallback and documented as the contrast."""
    decision = _decide("I cannot hear properly on the left", ())

    assert decision.offer_general_consultation == ENT  # type: ignore[attr-defined]
    assert decision.routed_advice == ""  # type: ignore[attr-defined]


# -- urgency outranks everything in this branch ----------------------------


def test_an_urgent_rule_forbids_offering_an_appointment() -> None:
    decision = _decide("I have sudden hearing loss", (REDUCED_HEARING, URGENT))

    assert decision.routed_urgent is True  # type: ignore[attr-defined]
    assert "come in today" in decision.routed_urgent_instruction  # type: ignore[attr-defined]
    assert decision.offer_general_consultation is None  # type: ignore[attr-defined]


def test_urgency_wins_even_when_another_rule_also_matches() -> None:
    """"sudden hearing loss" also contains "hearing"; the urgent rule must win."""
    decision = _decide("sudden hearing loss since today", (URGENT, REDUCED_HEARING))
    assert decision.routed_urgent is True  # type: ignore[attr-defined]


# -- a named service still beats any rule ---------------------------------


def test_naming_a_service_is_never_overridden_by_a_rule() -> None:
    """A caller who asked for a hearing test gets one, not whatever a rule infers."""
    decision = _decide("I would like to book a hearing test", (NOSE, REDUCED_HEARING))

    assert decision.selected_service == HEARING  # type: ignore[attr-defined]
    assert decision.is_administrative is True  # type: ignore[attr-defined]


# -- restraint holds -------------------------------------------------------


#: A phrase the symptom detector genuinely recognises, and which none of the routes in
#: this module cover. "sharp pain in my jaw" was the obvious choice and the wrong one:
#: it does not trip ``names_symptom`` at all, so it is handled as a plain
#: administrative turn and proves nothing about the fallback.
UNROUTED_SYMPTOM = "my ear hurts"


def test_an_unrouted_symptom_falls_back_unchanged() -> None:
    decision = _decide(UNROUTED_SYMPTOM, (NOSE, REDUCED_HEARING))

    assert decision.offer_general_consultation == ENT  # type: ignore[attr-defined]
    assert decision.routed_advice == ""  # type: ignore[attr-defined]


def test_an_unrouted_symptom_escalates_when_there_is_nothing_to_offer() -> None:
    decision = _decide(UNROUTED_SYMPTOM, (NOSE,), general=None)

    assert decision.requires_escalation is True  # type: ignore[attr-defined]


def test_a_clinical_question_is_still_declined_before_any_routing() -> None:
    """Routing must not become a way to get a clinical question answered."""
    decision = _decide("what is wrong with me", (NOSE,))

    assert decision.decline_clinical_content is True  # type: ignore[attr-defined]
    assert decision.routed_urgent is False  # type: ignore[attr-defined]


def test_asking_for_a_human_still_gets_a_human() -> None:
    """A routed symptom must never override an explicit request for a person."""
    decision = _decide("can I speak to a human please", (NOSE,))
    assert decision.requires_escalation is True  # type: ignore[attr-defined]
