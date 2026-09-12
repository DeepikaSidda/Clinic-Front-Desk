"""Offered-service matcher (task 6.1, Req 2.1, 2.9).

The Voice_Front_Desk never infers a service from a symptom or a fuzzy phrase
(Req 10.2, 10.3); it only accepts a service the patient *names* that equals a
service the clinic offers. This module is that single decision point.

Matching is **exact name equality, compared case- and whitespace-insensitively**
against the configured offered-service names:

    *For any* set of offered services and any spoken service name, the matcher
    resolves to an offered service **iff** the name equals an offered service,
    and resolves to that exact service; a name matching no offered service
    yields a not-offered result and no service selection. (Property 3)

Why normalize
-------------
"Equals" is about the *name the patient said*, not its transcription. Speech
recognition returns lower-cased text with variable spacing, so a patient asking
for a "Hearing Test" is transcribed ``"hearing test"``. Comparing raw bytes made
every real spoken booking fail as not-offered — the clinic offers ``"Hearing
Test"`` and the caller was told it did not. Normalizing case and collapsing
whitespace fixes that without weakening anything:

- It is still **exact**: no substring, prefix, fuzzy, or synonym matching. ``"ear
  test"`` does not match ``"Hearing Test"``.
- It cannot infer a service from a symptom (Req 10.2, 10.3, Property 14): a
  symptom phrase does not normalize to a service name, so the guardrail's
  no-symptom-to-service path is untouched.
- The returned value is always the **exact configured string**, so everything
  downstream (slot lookup, pricing, prep instructions) keys off the canonical
  name rather than whatever casing the caller's audio produced.

On a match the matcher returns ``Ok(<offered service name>)`` — the exact
offered string, which becomes the "requested service" the rest of the booking
flow uses (Req 2.1). On no match it returns ``Err(NotOffered(...))`` carrying the
name the patient said verbatim, and selects nothing (Req 2.9); the orchestrator
then asks the patient to name an offered service.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

#: Runs of any whitespace, collapsed to a single space during normalization.
_WHITESPACE_RUN = re.compile(r"\s+")

from clinic_front_desk.models import (
    ClinicKnowledgeBase,
    Err,
    NotOffered,
    Ok,
    ToolResult,
)


def offered_service_names(kb: ClinicKnowledgeBase) -> list[str]:
    """Return the names of the services the clinic offers (design ``ServiceConfig``).

    Order follows the configured service order so downstream matching and any
    listing is deterministic.
    """
    return [service.name for service in kb.services]


def normalize_service_name(name: str) -> str:
    """Return the comparison form of a service name.

    Strips surrounding whitespace, collapses internal whitespace runs to single
    spaces, and case-folds. ``casefold`` rather than ``lower`` so non-ASCII
    clinic service names compare correctly.

    This is the *only* transformation applied — it makes two spellings of the
    same name compare equal, and nothing else.
    """
    return _WHITESPACE_RUN.sub(" ", name.strip()).casefold()


def match_offered_service(
    named_service: str, offered_services: Iterable[str]
) -> ToolResult[str]:
    """Resolve a patient-named service to an offered service, or not-offered.

    Args:
        named_service: The service name the patient explicitly named, as
            transcribed.
        offered_services: The clinic's offered-service names (e.g. from
            :func:`offered_service_names`).

    Returns:
        ``Ok(service)`` with the **exact configured** offered-service string when
        ``named_service`` names an offered service, compared via
        :func:`normalize_service_name` (Req 2.1); otherwise
        ``Err(NotOffered(named_service=...))`` carrying the name verbatim, with no
        service selected (Req 2.9). The first offered service whose normalized
        name matches is returned; offered names are expected to be unique.
    """
    target = normalize_service_name(named_service)
    for offered in offered_services:
        if normalize_service_name(offered) == target:
            return Ok(offered)
    return Err(NotOffered(named_service=named_service))


__all__ = [
    "offered_service_names",
    "normalize_service_name",
    "match_offered_service",
]
