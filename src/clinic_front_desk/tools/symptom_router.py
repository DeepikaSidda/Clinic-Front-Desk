"""Route a described problem to a service — using the doctor's rules, not the model's.

A caller who says "there's an itch inside my nose" wants to be told what to book.
Until now the agent refused, because inferring a service from a symptom is a clinical
judgement and the model is not qualified to make one. That refusal was correct and
also unhelpful: the caller hung up no better off.

This closes the gap without weakening anything, by moving the judgement to the person
qualified to make it. The doctor writes the rules
(:class:`~clinic_front_desk.models.SymptomRoute`); this module only *matches* the
caller's words against them and reports what it found. The model never decides.

Three properties hold by construction:

**No rule, no answer.** A problem matching nothing returns "unmatched" and the caller
is escalated to a human, which is exactly what happened before this existed. The
feature can only ever add coverage the doctor has explicitly authored.

**Whole words only.** Phrases match on word boundaries, so a rule for "ear" does not
fire on "hearing" or "clearer". Substring matching on medical words invents
connections, which is the failure this design exists to avoid.

**A rule pointing nowhere is ignored.** If the named service is not one the clinic
offers, the route is skipped rather than booked — a stale rule must not strand a
caller on a service that does not exist.

Urgency is deliberately part of the same table. Some problems need to be seen today,
and the worst thing this system could do is quietly offer next Tuesday to someone
with sudden hearing loss. When the doctor marks a route urgent, the agent is told not
to offer a routine slot and is given her words to say instead.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from clinic_front_desk.models import ClinicKnowledgeBase, SymptomRoute
from clinic_front_desk.tools.service_matcher import (
    normalize_service_name,
    offered_service_names,
)


@dataclass(frozen=True)
class RouteMatch:
    """A described problem matched one of the doctor's routes."""

    service: str
    advice: str
    urgent: bool
    urgent_instruction: str
    #: The phrase that fired, so the agent can reflect it back and the doctor can
    #: see why a route matched when reviewing a call.
    matched_phrase: str
    kind: str = "matched"


@dataclass(frozen=True)
class RouteUnmatched:
    """Nothing the doctor has written covers this. Escalate; do not guess."""

    described: str
    kind: str = "unmatched"


RouteResult = RouteMatch | RouteUnmatched


def _normalize(text: str) -> str:
    """Lower-case, collapse whitespace, and drop punctuation to spaces.

    Punctuation becomes a space rather than vanishing, so "nose-block" is two words
    and matches a rule for "block" — while "nosebleed" stays one word and does not.
    """
    lowered = re.sub(r"[^0-9a-z]+", " ", text.casefold())
    return f" {' '.join(lowered.split())} "


def _phrase_present(haystack: str, phrase: str) -> bool:
    """True when every word of ``phrase`` appears in ``haystack`` as a whole word.

    Order-independent, and that is deliberate. The first version required the words
    to be contiguous, which meant a rule written "blocked nose" did not match a
    caller saying "my nose is blocked since two days" — the same complaint in the
    order people actually speak. Asking the doctor to enumerate every phrasing of
    every symptom is a losing game and would leave silent gaps.

    The rule is therefore: *all these words, anywhere in the sentence*. It is easy to
    explain, easy for the doctor to control, and predictable — add a word to narrow a
    rule, remove one to widen it.

    The cost is accepted knowingly. "ear pain" now matches "pain in my ear" (wanted)
    and could in principle match a sentence mentioning an ear and a pain that are
    unrelated (not wanted). That direction of error is the tolerable one: it books a
    consultation the doctor was going to give anyway, whereas the opposite error
    leaves a caller with no answer. Words still match whole, so "ear" never fires on
    "hearing".
    """
    words = _normalize(phrase).split()
    if not words:
        return False
    return all(f" {word} " in haystack for word in words)


def route_with(
    routes: Sequence[SymptomRoute],
    offered_services: Iterable[str],
    described: str,
) -> RouteResult:
    """Match ``described`` against ``routes``, given the services actually offered.

    The routes-and-services form exists because two callers need this from different
    places. The ``suggest_service_for_problem`` tool has the whole knowledge base to
    hand. The turn-signal extractor does not — and the guardrail policy deliberately
    decides over structured signals rather than raw text, so the routing has to be
    resolved before the policy sees the turn. Both go through this one function so
    they cannot disagree about what a caller's words mean.
    """
    if not described.strip():
        return RouteUnmatched(described=described)

    haystack = _normalize(described)
    offered = {normalize_service_name(name) for name in offered_services}

    for route in routes:
        if not _route_is_usable(route, offered):
            continue
        for phrase in route.phrases:
            if _phrase_present(haystack, phrase):
                return RouteMatch(
                    service=route.service,
                    advice=route.advice,
                    urgent=route.urgent,
                    urgent_instruction=route.urgent_instruction,
                    matched_phrase=phrase,
                )
    return RouteUnmatched(described=described)


def route_described_problem(
    kb: ClinicKnowledgeBase | None, described: str
) -> RouteResult:
    """Match what the caller described against the doctor's routing rules.

    Args:
        kb: The clinic configuration carrying ``symptom_routes``. ``None`` or an
            unconfigured clinic yields ``RouteUnmatched`` — no configuration means no
            authored judgement to apply.
        described: The caller's own words, as transcribed.

    Returns:
        :class:`RouteMatch` for the first rule whose phrase appears, in the order the
        doctor wrote them — so she controls precedence by ordering, and can put an
        urgent rule above a routine one covering the same word.
        :class:`RouteUnmatched` when nothing matches.
    """
    if kb is None:
        return RouteUnmatched(described=described)
    return route_with(kb.symptom_routes, offered_service_names(kb), described)


def _route_is_usable(route: SymptomRoute, offered: set[str]) -> bool:
    """A route is usable only if it names a service the clinic actually offers.

    An urgent route is exempt: its job is to stop a booking and tell the caller to
    come in, so it does not need a bookable service behind it.
    """
    if not route.phrases:
        return False
    if route.urgent:
        return True
    return normalize_service_name(route.service) in offered


__all__ = [
    "RouteMatch",
    "RouteResult",
    "RouteUnmatched",
    "route_described_problem",
    "route_with",
]
