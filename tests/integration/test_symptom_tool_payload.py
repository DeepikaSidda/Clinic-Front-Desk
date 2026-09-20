"""What the model is handed when a caller describes a problem.

The router is pure and separately tested. This covers the payload, because that dict
is the entire channel between the doctor's routing and what a caller hears — and the
dangerous failure is not a wrong service, it is the model being left enough room to
invent one.

So: an unmatched problem must come back with no service, and with an explicit
instruction not to guess. An urgent one must come back saying do not book.
"""

from __future__ import annotations

from typing import Any

from clinic_front_desk.data_layer.memory import (
    MemoryAppointmentStore,
    MemoryCallSessionStore,
    MemoryClinicKnowledgeBaseStore,
    MemoryEscalationStore,
    MemoryPatientStore,
    MemoryWaitlistStore,
)
from clinic_front_desk.models import (
    ClinicKnowledgeBase,
    Provider,
    ServiceConfig,
    SymptomRoute,
)
from clinic_front_desk.voice.agent import (
    PATIENT_FACING_TOOL_NAMES,
    VoiceFrontDeskStores,
    build_patient_facing_tools,
)

ENT = "ENT Consultation"


def _tools(routes: list[SymptomRoute]) -> dict[str, Any]:
    knowledge_base = MemoryClinicKnowledgeBaseStore()
    knowledge_base.save(
        ClinicKnowledgeBase(
            location="Tirupati",
            services=[ServiceConfig(name=ENT)],
            providers=[Provider(id="prov-raana", name="Dr Raana", specialty="ENT")],
            symptom_routes=routes,
        )
    )
    stores = VoiceFrontDeskStores(
        appointments=MemoryAppointmentStore(),
        patients=MemoryPatientStore(),
        waitlist=MemoryWaitlistStore(),
        escalations=MemoryEscalationStore(),
        knowledge_base=knowledge_base,
        call_sessions=MemoryCallSessionStore(),
    )
    return build_patient_facing_tools(stores)


def _ask(tools: dict[str, Any], described: str) -> dict[str, Any]:
    tool = tools["suggest_service_for_problem"]
    func = getattr(tool, "__wrapped__", None) or getattr(tool, "func", None) or tool
    result = func(described_problem=described)
    assert isinstance(result, dict)
    return result


NOSE = SymptomRoute(
    phrases=["itching in nose", "blocked nose"],
    service=ENT,
    advice="Dr Raana sees nasal irritation under an ENT consultation.",
)
URGENT = SymptomRoute(
    phrases=["sudden hearing loss"],
    urgent=True,
    urgent_instruction="Please come in today, or go to a hospital if you cannot.",
)


# -- the tool is actually on the agent --------------------------------------


def test_the_tool_is_registered_on_the_agent() -> None:
    assert "suggest_service_for_problem" in PATIENT_FACING_TOOL_NAMES
    assert "suggest_service_for_problem" in _tools([NOSE])


# -- a routed problem -------------------------------------------------------


def test_a_routed_problem_returns_the_doctors_service_and_words() -> None:
    payload = _ask(_tools([NOSE]), "some kind of itching in nose")

    assert payload["matched"] is True
    assert payload["service"] == ENT
    assert payload["advice"] == NOSE.advice
    assert payload["urgent"] is False
    assert "do_not_book" not in payload


# -- an unrouted problem ----------------------------------------------------


def test_an_unrouted_problem_offers_no_service_at_all() -> None:
    """No service field means nothing for the model to book on a guess."""
    payload = _ask(_tools([NOSE]), "sharp pain in my jaw")

    assert payload["matched"] is False
    assert "service" not in payload


def test_an_unrouted_problem_says_not_to_guess() -> None:
    payload = _ask(_tools([NOSE]), "sharp pain in my jaw")
    guidance = payload["guidance"].lower()

    assert "do not suggest" in guidance
    assert "flag_for_human" in payload["guidance"]


def test_with_no_routes_every_problem_is_unmatched() -> None:
    """The feature is opt-in: an unconfigured clinic behaves exactly as before."""
    payload = _ask(_tools([]), "itching in nose")
    assert payload["matched"] is False


# -- urgency ---------------------------------------------------------------


def test_an_urgent_problem_forbids_booking() -> None:
    payload = _ask(_tools([NOSE, URGENT]), "I have sudden hearing loss")

    assert payload["matched"] is True
    assert payload["urgent"] is True
    assert payload["do_not_book"] is True
    assert "come in today" in payload["urgent_instruction"]


def test_the_tool_never_returns_an_error_for_an_unknown_problem() -> None:
    """An unknown problem is a normal answer, not a failure — the agent must keep going."""
    for described in ("", "something odd", "my jaw"):
        assert _ask(_tools([NOSE]), described)["ok"] is True
