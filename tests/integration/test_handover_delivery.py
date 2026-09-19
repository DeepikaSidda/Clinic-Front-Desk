"""Escalation is recorded first, delivered second, and never over-promised.

The ordering is the property under test. A caller who asks for a person must end up
on the doctor's dashboard whatever the transport does — and the agent must only be
handed wording that matches what actually happened. Telling someone "we'll ring you
back" when nothing was dispatched is the one outcome worse than saying the handover
failed.
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
from clinic_front_desk.handover import (
    FailingHandoverTransport,
    MemoryHandoverTransport,
    RaisingHandoverTransport,
)
from clinic_front_desk.models import is_ok
from clinic_front_desk.voice.agent import (
    BoundToolset,
    VoiceFrontDeskStores,
    build_patient_facing_tools,
)

SESSION = "call-handover-1"


def _stores() -> VoiceFrontDeskStores:
    return VoiceFrontDeskStores(
        appointments=MemoryAppointmentStore(),
        patients=MemoryPatientStore(),
        waitlist=MemoryWaitlistStore(),
        escalations=MemoryEscalationStore(),
        knowledge_base=MemoryClinicKnowledgeBaseStore(),
        call_sessions=MemoryCallSessionStore(),
    )


def _tools(transport: Any | None) -> tuple[dict[str, Any], BoundToolset]:
    stores = _stores()
    toolset = BoundToolset(stores, handover_transport=transport)
    toolset.bind_session(SESSION) if hasattr(toolset, "bind_session") else None
    return build_patient_facing_tools(stores, toolset=toolset), toolset


def _escalate(tools: dict[str, Any]) -> dict[str, Any]:
    tool = tools["flag_for_human"]
    func = getattr(tool, "__wrapped__", None) or getattr(tool, "func", None) or tool
    result = func(reason="patient_request", context="Caller asked for a person")
    assert isinstance(result, dict)
    return result


# -- delivery succeeds ------------------------------------------------------


def test_a_delivered_handover_reports_the_reference() -> None:
    transport = MemoryHandoverTransport()
    tools, _ = _tools(transport)

    payload = _escalate(tools)

    assert payload["ok"] is True
    assert payload["handover_delivered"] is True
    assert payload["handover_reference"]
    assert payload["handover_transport"] == "memory"
    assert len(transport.delivered) == 1


def test_the_transport_receives_the_escalation_that_was_persisted() -> None:
    """The reference must point at a real row, not a copy made for the transport."""
    transport = MemoryHandoverTransport()
    tools, toolset = _tools(transport)

    _escalate(tools)

    delivered = transport.delivered[0].escalation
    recorded = toolset.stores.escalations.list_recent(10)
    assert is_ok(recorded)
    assert delivered.id in {e.id for e in recorded.value}


# -- delivery fails ---------------------------------------------------------


def test_a_failed_delivery_still_records_the_escalation() -> None:
    """The caller asked for help. That fact cannot be lost to a transport outage."""
    tools, toolset = _tools(FailingHandoverTransport())

    payload = _escalate(tools)

    assert payload["ok"] is True, "the escalation itself must still succeed"
    recorded = toolset.stores.escalations.list_recent(10)
    assert is_ok(recorded)
    assert len(recorded.value) == 1


def test_a_failed_delivery_is_reported_not_hidden() -> None:
    tools, _ = _tools(FailingHandoverTransport())

    payload = _escalate(tools)

    assert payload["handover_delivered"] is False
    assert payload["handover_failure"]


def test_a_failed_delivery_never_promises_a_callback() -> None:
    tools, _ = _tools(FailingHandoverTransport())

    spoken = _escalate(tools)["say_to_caller"].lower()

    assert "could not reach anyone" in spoken
    assert "will call you back" not in spoken
    assert "someone will call" not in spoken


def test_a_transport_that_raises_does_not_end_the_call() -> None:
    """A transport is forbidden from raising. One that does is held at arm's length."""
    tools, toolset = _tools(RaisingHandoverTransport())

    payload = _escalate(tools)

    assert payload["ok"] is True
    assert payload["handover_delivered"] is False
    recorded = toolset.stores.escalations.list_recent(10)
    assert is_ok(recorded)
    assert len(recorded.value) == 1


# -- no transport configured ------------------------------------------------


def test_with_no_transport_nothing_is_claimed() -> None:
    """The pre-transport behaviour: recorded and shown, with no one notified."""
    tools, toolset = _tools(None)

    payload = _escalate(tools)

    assert payload["ok"] is True
    assert payload["handover_delivered"] is False
    assert payload["handover_transport"] is None
    spoken = payload["say_to_caller"].lower()
    assert "recorded this for the clinic" in spoken
    assert "will call you back" not in spoken

    recorded = toolset.stores.escalations.list_recent(10)
    assert is_ok(recorded)
    assert len(recorded.value) == 1


def test_the_agent_is_always_given_something_to_say() -> None:
    """Whatever happens, there is wording — the agent never has to improvise here."""
    for transport in (MemoryHandoverTransport(), FailingHandoverTransport(), None):
        tools, _ = _tools(transport)
        payload = _escalate(tools)
        assert payload["say_to_caller"].strip()


# -- the escalation hook ----------------------------------------------------
#
# Separate from the transport. A transport delivers a handover and says whether it
# worked; this hook just tells interested parties a call is waiting, which is how
# the live-call console knows which call needs a person.


def test_the_escalation_hook_reports_the_session_and_reason() -> None:
    seen: list[tuple[str, str]] = []
    stores = _stores()
    toolset = BoundToolset(stores, on_escalation=lambda sid, reason: seen.append((sid, reason)))
    tools = build_patient_facing_tools(stores, toolset=toolset)

    _escalate(tools)

    assert len(seen) == 1
    assert seen[0][1] == "patient_request"


def test_the_hook_fires_once_per_call_and_reason() -> None:
    """Escalating twice for the same reason must not flag the same call twice."""
    seen: list[tuple[str, str]] = []
    stores = _stores()
    toolset = BoundToolset(stores, on_escalation=lambda sid, reason: seen.append((sid, reason)))
    tools = build_patient_facing_tools(stores, toolset=toolset)

    _escalate(tools)
    _escalate(tools)

    assert len(seen) == 1


def test_a_failing_hook_never_costs_the_escalation() -> None:
    """The escalation is already written by the time the hook runs."""

    def explode(_sid: str, _reason: str) -> None:
        raise RuntimeError("listener is broken")

    stores = _stores()
    toolset = BoundToolset(stores, on_escalation=explode)
    tools = build_patient_facing_tools(stores, toolset=toolset)

    payload = _escalate(tools)

    assert payload["ok"] is True
    recorded = toolset.stores.escalations.list_recent(10)
    assert is_ok(recorded)
    assert len(recorded.value) == 1
