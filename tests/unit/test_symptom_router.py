"""Symptom routing uses the doctor's rules, and only ever the doctor's rules.

A caller describing a problem should be told what to book. The judgement behind that
answer has to be the doctor's, because a model inferring a service from a symptom is
practising medicine unsupervised on a recorded line.

So the properties under test are mostly about restraint:

*   nothing matches unless the doctor wrote a rule for it;
*   phrases match whole words, never fragments, because substring matching on
    medical words invents connections;
*   a rule naming a service the clinic does not offer is ignored rather than booked;
*   urgent rules win, and they stop a booking rather than allowing one.
"""

from __future__ import annotations

import pytest

from clinic_front_desk.models import (
    ClinicKnowledgeBase,
    Provider,
    ServiceConfig,
    SymptomRoute,
)
from clinic_front_desk.tools.symptom_router import (
    RouteMatch,
    RouteUnmatched,
    route_described_problem,
)

ENT = "ENT Consultation"
HEARING = "Hearing Test"


def _kb(routes: list[SymptomRoute], services: tuple[str, ...] = (ENT, HEARING)) -> ClinicKnowledgeBase:
    return ClinicKnowledgeBase(
        location="Tirupati",
        services=[ServiceConfig(name=name) for name in services],
        providers=[Provider(id="prov-raana", name="Dr Raana", specialty="ENT")],
        symptom_routes=routes,
    )


NOSE = SymptomRoute(
    phrases=["itching in nose", "itch inside my nose", "blocked nose", "sneezing"],
    service=ENT,
    advice="Dr Raana sees nasal irritation under an ENT consultation.",
)

SUDDEN_LOSS = SymptomRoute(
    phrases=["sudden hearing loss", "lost my hearing"],
    service="",
    urgent=True,
    urgent_instruction="Please come in today, or go to a hospital if you cannot.",
)


# -- the case that motivated this -------------------------------------------


def test_the_nose_itch_that_used_to_get_no_answer() -> None:
    outcome = route_described_problem(
        _kb([NOSE]), "actually there is some kind of itching in nose"
    )

    assert isinstance(outcome, RouteMatch)
    assert outcome.service == ENT
    assert "ENT consultation" in outcome.advice
    assert outcome.urgent is False


def test_the_doctors_wording_is_returned_verbatim() -> None:
    """The agent speaks her words, not a paraphrase of them."""
    outcome = route_described_problem(_kb([NOSE]), "my nose keeps sneezing")
    assert isinstance(outcome, RouteMatch)
    assert outcome.advice == NOSE.advice


# -- restraint: no rule, no answer ------------------------------------------


def test_an_unwritten_symptom_is_not_guessed() -> None:
    """The thing this feature must never do."""
    outcome = route_described_problem(_kb([NOSE]), "I have a sharp pain in my jaw")
    assert isinstance(outcome, RouteUnmatched)


def test_no_routes_configured_behaves_exactly_as_before() -> None:
    """With an empty table the agent is back to escalating every symptom."""
    outcome = route_described_problem(_kb([]), "itching in nose")
    assert isinstance(outcome, RouteUnmatched)


def test_no_clinic_configuration_matches_nothing() -> None:
    assert isinstance(route_described_problem(None, "itching in nose"), RouteUnmatched)


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_an_empty_description_matches_nothing(blank: str) -> None:
    assert isinstance(route_described_problem(_kb([NOSE]), blank), RouteUnmatched)


# -- whole words only -------------------------------------------------------


def test_a_phrase_does_not_fire_on_a_longer_word() -> None:
    """A rule for "ear" must not match "hearing" — that is an invented connection."""
    ear = SymptomRoute(phrases=["ear"], service=ENT)
    outcome = route_described_problem(_kb([ear]), "I want a hearing test")
    assert isinstance(outcome, RouteUnmatched)


def test_a_phrase_does_fire_as_its_own_word() -> None:
    ear = SymptomRoute(phrases=["ear"], service=ENT)
    outcome = route_described_problem(_kb([ear]), "my ear hurts")
    assert isinstance(outcome, RouteMatch)


def test_punctuation_between_words_still_matches() -> None:
    """"nose-block" is two words; a rule for "block" should see it."""
    block = SymptomRoute(phrases=["block"], service=ENT)
    outcome = route_described_problem(_kb([block]), "I have nose-block since Monday")
    assert isinstance(outcome, RouteMatch)


def test_a_joined_word_is_not_split() -> None:
    """"nosebleed" is one word and must not match a rule for "nose"."""
    nose_only = SymptomRoute(phrases=["nose"], service=ENT)
    outcome = route_described_problem(_kb([nose_only]), "nosebleed")
    assert isinstance(outcome, RouteUnmatched)


@pytest.mark.parametrize(
    "spoken",
    [
        "ITCHING IN NOSE",
        "there's  itching   in nose, doctor",
        "Itching in nose.",
    ],
)
def test_matching_survives_transcription_noise(spoken: str) -> None:
    assert isinstance(route_described_problem(_kb([NOSE]), spoken), RouteMatch)


# -- a rule must point somewhere real --------------------------------------


def test_a_route_naming_an_unoffered_service_is_ignored() -> None:
    """A stale rule must not strand a caller on a service that does not exist."""
    stale = SymptomRoute(phrases=["itching in nose"], service="Septoplasty")
    outcome = route_described_problem(_kb([stale]), "itching in nose")
    assert isinstance(outcome, RouteUnmatched)


def test_a_route_with_no_phrases_is_ignored() -> None:
    empty = SymptomRoute(phrases=[], service=ENT)
    assert isinstance(route_described_problem(_kb([empty]), "anything"), RouteUnmatched)


# -- urgency ----------------------------------------------------------------


def test_an_urgent_route_stops_a_booking() -> None:
    outcome = route_described_problem(_kb([SUDDEN_LOSS]), "I have sudden hearing loss")

    assert isinstance(outcome, RouteMatch)
    assert outcome.urgent is True
    assert "come in today" in outcome.urgent_instruction


def test_an_urgent_route_needs_no_bookable_service() -> None:
    """Its job is to prevent a booking, so it is exempt from the offered-service check."""
    outcome = route_described_problem(_kb([SUDDEN_LOSS]), "lost my hearing yesterday")
    assert isinstance(outcome, RouteMatch)
    assert outcome.service == ""


def test_order_decides_precedence_so_the_doctor_controls_it() -> None:
    """An urgent rule placed above a routine one covering the same words wins."""
    routine = SymptomRoute(phrases=["hearing"], service=HEARING)
    urgent_first = route_described_problem(
        _kb([SUDDEN_LOSS, routine]), "sudden hearing loss since today"
    )
    routine_first = route_described_problem(
        _kb([routine, SUDDEN_LOSS]), "sudden hearing loss since today"
    )

    assert isinstance(urgent_first, RouteMatch) and urgent_first.urgent is True
    assert isinstance(routine_first, RouteMatch) and routine_first.urgent is False


def test_the_matched_phrase_is_reported_for_review() -> None:
    """So the doctor can see why a route fired when reading a call back."""
    outcome = route_described_problem(_kb([NOSE]), "blocked nose for three days")
    assert isinstance(outcome, RouteMatch)
    assert outcome.matched_phrase == "blocked nose"


# -- word order, and the tradeoff it costs ---------------------------------
#
# Matching requires every word of a phrase, anywhere in the sentence, rather than
# the words being contiguous. Found by checking live config: a rule written
# "blocked nose" missed "my nose is blocked since two days" — the same complaint in
# the order people actually speak. Enumerating every phrasing of every symptom is a
# losing game, so the rule widened instead.


def test_a_rule_matches_however_the_caller_orders_the_words() -> None:
    blocked = SymptomRoute(phrases=["blocked nose"], service=ENT)

    for spoken in (
        "my nose is blocked since two days",
        "blocked nose",
        "nose feels blocked",
        "I think the nose is a bit blocked",
    ):
        assert isinstance(route_described_problem(_kb([blocked]), spoken), RouteMatch), spoken


def test_a_rule_still_needs_every_one_of_its_words() -> None:
    """Widening word order must not widen which rules fire."""
    blocked = SymptomRoute(phrases=["blocked nose"], service=ENT)

    # "blocked" alone is not this rule, and neither is "nose" alone.
    assert isinstance(route_described_problem(_kb([blocked]), "my ear is blocked"), RouteUnmatched)
    assert isinstance(route_described_problem(_kb([blocked]), "my nose hurts"), RouteUnmatched)


def test_the_words_are_still_matched_whole() -> None:
    """Order-independence must not turn into substring matching."""
    ear_pain = SymptomRoute(phrases=["ear pain"], service=ENT)
    # "hearing" contains "ear" but is a different word.
    outcome = route_described_problem(_kb([ear_pain]), "hearing is painful")
    assert isinstance(outcome, RouteUnmatched)


def test_the_accepted_cost_of_order_independence() -> None:
    """Documents the known false positive, so it is a decision and not a surprise.

    An unrelated ear and an unrelated pain in one sentence will route to a
    consultation. Accepted: that books a visit the doctor was going to give anyway,
    whereas the opposite error leaves a caller with no answer at all.
    """
    ear_pain = SymptomRoute(phrases=["ear pain"], service=ENT)
    outcome = route_described_problem(
        _kb([ear_pain]), "my ear is completely fine but I have pain in my knee"
    )
    assert isinstance(outcome, RouteMatch)
