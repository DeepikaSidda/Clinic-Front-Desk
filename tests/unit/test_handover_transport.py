"""A handover must be recorded even when it cannot be delivered.

``flag_for_human`` writing a row is not a handover — nothing reaches a person until
a transport carries it. These tests pin the two properties that matter more than
delivery working:

*   the escalation survives a transport that fails, or raises, or is absent;
*   the agent is never handed wording that promises a callback nobody dispatched.

The Amazon Connect transport is exercised against an injected fake client, because
``connect:CreateInstance`` is refused outright on AWS accounts billed through AISPL
— an account-type restriction, not IAM and not a quota. The integration is
therefore tested without an instance, and activates by configuration in an account
that permits one.
"""

from __future__ import annotations

from typing import Any

import pytest

from clinic_front_desk.handover import (
    ConnectHandoverTransport,
    FailingHandoverTransport,
    HandoverDelivered,
    HandoverFailed,
    HandoverRequest,
    MemoryHandoverTransport,
    RaisingHandoverTransport,
)
from clinic_front_desk.models import Escalation, EscalationReason, PatientRef

INSTANCE = "11111111-2222-3333-4444-555555555555"
FLOW = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _escalation() -> Escalation:
    return Escalation(
        id="esc-1",
        reason=EscalationReason.PATIENT_REQUEST,
        call_session_id="call-1",
        context="Caller asked to speak to a person.",
        created_at="2026-09-15T09:05:00+00:00",
        patient_ref=PatientRef(
            patient_id="pat-1", name="Sailaja Devi", callback_phone="9900012307"
        ),
    )


def _request() -> HandoverRequest:
    return HandoverRequest(
        escalation=_escalation(),
        summary="Caller asked for a person",
        patient_name="Sailaja Devi",
        callback_phone="9900012307",
        transcript_tail="patient: can I speak to someone\nagent: of course",
    )


class FakeConnect:
    """Records the call Connect would have received."""

    def __init__(self, contact_id: str = "contact-abc", raises: Exception | None = None):
        self.contact_id = contact_id
        self.raises = raises
        self.task_calls: list[dict[str, Any]] = []
        self.voice_calls: list[dict[str, Any]] = []

    def start_task_contact(self, **kwargs: Any) -> dict[str, Any]:
        if self.raises is not None:
            raise self.raises
        self.task_calls.append(kwargs)
        return {"ContactId": self.contact_id}

    def start_outbound_voice_contact(self, **kwargs: Any) -> dict[str, Any]:
        if self.raises is not None:
            raise self.raises
        self.voice_calls.append(kwargs)
        return {"ContactId": self.contact_id}


# -- the Connect transport --------------------------------------------------


def test_a_task_is_created_and_the_contact_id_comes_back() -> None:
    client = FakeConnect()
    transport = ConnectHandoverTransport(
        instance_id=INSTANCE, contact_flow_id=FLOW, client=client
    )

    outcome = transport.deliver(_request())

    assert isinstance(outcome, HandoverDelivered)
    assert outcome.reference == "contact-abc"
    assert len(client.task_calls) == 1


def test_the_task_carries_the_context_a_human_needs() -> None:
    """The whole point: whoever picks it up must not have to ask again."""
    client = FakeConnect()
    ConnectHandoverTransport(
        instance_id=INSTANCE, contact_flow_id=FLOW, client=client
    ).deliver(_request())

    attributes = client.task_calls[0]["Attributes"]
    assert attributes["patientName"] == "Sailaja Devi"
    assert attributes["callbackPhone"] == "9900012307"
    assert attributes["patientId"] == "pat-1"
    assert attributes["escalationReason"] == str(EscalationReason.PATIENT_REQUEST)
    assert attributes["callSessionId"] == "call-1"
    assert "can I speak to someone" in attributes["transcriptTail"]


def test_the_escalation_id_is_the_idempotency_token() -> None:
    """A retried tool call must not create a second task for one escalation."""
    client = FakeConnect()
    ConnectHandoverTransport(
        instance_id=INSTANCE, contact_flow_id=FLOW, client=client
    ).deliver(_request())

    assert client.task_calls[0]["ClientToken"] == "esc-1"


def test_no_queue_is_passed_because_the_flow_routes_the_task() -> None:
    client = FakeConnect()
    ConnectHandoverTransport(
        instance_id=INSTANCE, contact_flow_id=FLOW, client=client
    ).deliver(_request())

    assert "QueueId" not in client.task_calls[0]
    assert client.task_calls[0]["ContactFlowId"] == FLOW


def test_a_connect_error_becomes_a_failure_not_an_exception() -> None:
    """A contact centre being down must never end the caller's call."""
    client = FakeConnect(raises=RuntimeError("Connect is unavailable"))
    transport = ConnectHandoverTransport(
        instance_id=INSTANCE, contact_flow_id=FLOW, client=client
    )

    outcome = transport.deliver(_request())

    assert isinstance(outcome, HandoverFailed)
    assert "unavailable" in outcome.detail


def test_a_missing_contact_id_is_a_failure() -> None:
    """Connect answering without a ContactId means nothing is tracked."""

    class NoId(FakeConnect):
        def start_task_contact(self, **kwargs: Any) -> dict[str, Any]:
            return {}

    outcome = ConnectHandoverTransport(
        instance_id=INSTANCE, contact_flow_id=FLOW, client=NoId()
    ).deliver(_request())

    assert isinstance(outcome, HandoverFailed)


def test_a_failed_handover_never_promises_a_callback() -> None:
    """The sentence the agent is given must not claim someone will ring back."""
    client = FakeConnect(raises=RuntimeError("nope"))
    outcome = ConnectHandoverTransport(
        instance_id=INSTANCE, contact_flow_id=FLOW, client=client
    ).deliver(_request())

    spoken = outcome.spoken_detail.lower()
    assert "could not reach anyone" in spoken
    assert "will call you back" not in spoken


def test_a_long_transcript_is_trimmed_not_rejected() -> None:
    """Connect caps attribute size; losing old turns beats losing the handover."""
    client = FakeConnect()
    request = HandoverRequest(
        escalation=_escalation(),
        summary="x",
        transcript_tail="y" * 5000,
    )

    outcome = ConnectHandoverTransport(
        instance_id=INSTANCE, contact_flow_id=FLOW, client=client
    ).deliver(request)

    assert isinstance(outcome, HandoverDelivered)
    assert len(client.task_calls[0]["Attributes"]["transcriptTail"]) <= 1000


# -- the outbound-call path -------------------------------------------------


def test_a_callback_needs_a_source_number() -> None:
    """Outbound voice needs provisioning a task does not; say so rather than fail oddly."""
    transport = ConnectHandoverTransport(
        instance_id=INSTANCE, contact_flow_id=FLOW, client=FakeConnect()
    )

    outcome = transport.call_back(_request())

    assert isinstance(outcome, HandoverFailed)
    assert "source phone number" in outcome.detail


def test_a_callback_needs_a_number_to_call() -> None:
    transport = ConnectHandoverTransport(
        instance_id=INSTANCE,
        contact_flow_id=FLOW,
        client=FakeConnect(),
        source_phone_number="+15550100",
    )
    request = HandoverRequest(escalation=_escalation(), summary="s")

    outcome = transport.call_back(request)

    assert isinstance(outcome, HandoverFailed)
    assert "callback number" in outcome.detail


def test_a_callback_places_an_outbound_contact() -> None:
    client = FakeConnect()
    transport = ConnectHandoverTransport(
        instance_id=INSTANCE,
        contact_flow_id=FLOW,
        client=client,
        source_phone_number="+15550100",
    )

    outcome = transport.call_back(_request())

    assert isinstance(outcome, HandoverDelivered)
    assert client.voice_calls[0]["DestinationPhoneNumber"] == "9900012307"
    assert client.voice_calls[0]["SourcePhoneNumber"] == "+15550100"


# -- the fakes honour the same contract ------------------------------------


def test_memory_transport_reports_delivery() -> None:
    transport = MemoryHandoverTransport()
    outcome = transport.deliver(_request())
    assert isinstance(outcome, HandoverDelivered)
    assert transport.delivered[0].escalation.id == "esc-1"


def test_failing_transport_reports_failure_without_raising() -> None:
    transport = FailingHandoverTransport()
    outcome = transport.deliver(_request())
    assert isinstance(outcome, HandoverFailed)
    assert transport.attempts, "the attempt should still be observable"


def test_a_raising_transport_violates_the_contract_on_purpose() -> None:
    """Kept so callers can be shown to hold a misbehaving transport at arm's length."""
    with pytest.raises(RuntimeError):
        RaisingHandoverTransport().deliver(_request())


def test_every_transport_names_itself() -> None:
    """The dashboard records which transport handled a handover."""
    assert ConnectHandoverTransport(
        instance_id=INSTANCE, contact_flow_id=FLOW, client=FakeConnect()
    ).name == "amazon-connect"
    assert MemoryHandoverTransport().name == "memory"
    assert FailingHandoverTransport().name == "failing"
