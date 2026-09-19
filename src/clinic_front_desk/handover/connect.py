"""Amazon Connect handover transport.

Turns a recorded escalation into real work in a contact centre, so a caller who
asks for a person reaches one instead of waiting for the doctor to notice a row on
a dashboard.

Two Connect APIs, because a handover means different things depending on whether
the caller is still there:

``StartTaskContact``
    Creates a work item on the clinic's queue, carrying the escalation as contact
    attributes. It appears in the Connect agent workspace with the patient's name,
    number, reason and the tail of the transcript already attached — so whoever
    picks it up starts informed. Needs **no phone number**, which makes it the
    transport that works on a freshly created instance.

``StartOutboundVoiceContact``
    Places an actual call. Right when the caller has already hung up and is owed a
    callback. Requires a claimed source phone number and the destination country to
    be permitted on the instance, both of which are provisioning concerns rather
    than code ones.

Nothing here raises. A contact centre being unreachable must degrade the promise
made to the caller, never fail the call — see
:class:`~clinic_front_desk.handover.transport.HandoverTransport`.

Provisioning note
-----------------
This code is complete and tested, but it cannot run against an Amazon Connect
instance in every account. ``connect:CreateInstance`` is refused outright on
accounts billed through AISPL (Amazon's Indian entity) with
``InvalidRequestException``, which is an account-type restriction — not IAM, not a
service quota, so there is nothing to grant or raise. Configure
``CLINIC_CONNECT_INSTANCE_ID`` and ``CLINIC_CONNECT_CONTACT_FLOW_ID`` in an account
that permits Connect and this transport activates with no code change.
"""

from __future__ import annotations

import logging
from typing import Any

from clinic_front_desk.handover.transport import (
    HandoverDelivered,
    HandoverFailed,
    HandoverOutcome,
    HandoverRequest,
    HandoverTransport,
)

logger = logging.getLogger(__name__)

#: Contact attributes must be strings, and Connect caps the whole attribute map.
#: The transcript is the only field that can be long, so it is the one that gets
#: trimmed — losing the earliest turns rather than dropping the patient's number.
MAX_ATTRIBUTE_CHARS = 1_000

#: Connect rejects a task with no name, and a queue full of "Escalation" tells the
#: person picking it up nothing.
DEFAULT_TASK_NAME = "Clinic call needs a person"


class ConnectHandoverTransport(HandoverTransport):
    """Deliver a handover into Amazon Connect as a task, or as an outbound call.

    Args:
        instance_id: The Connect instance the clinic's queue belongs to.
        contact_flow_id: The flow that runs when the contact starts. For a task
            this is the flow that routes it to the clinic queue.
        client: A boto3 ``connect`` client. Injected so the whole path is testable
            without an instance, which matters because an instance cannot be
            created at all in some accounts.
        source_phone_number: Required only for outbound voice. A claimed number on
            the instance.
    """

    def __init__(
        self,
        *,
        instance_id: str,
        contact_flow_id: str,
        client: Any | None = None,
        source_phone_number: str | None = None,
        region: str | None = None,
    ) -> None:
        self._instance_id = instance_id
        self._contact_flow_id = contact_flow_id
        self._source_phone_number = source_phone_number
        self._region = region
        self._client = client

    @property
    def name(self) -> str:
        return "amazon-connect"

    def _connect(self) -> Any:
        """The boto3 client, created lazily.

        Lazy so importing this module costs nothing and needs no credentials — the
        rest of the suite imports it while testing with an injected fake.
        """
        if self._client is None:
            # boto3 ships no py.typed marker; matches the local-ignore convention
            # used by the other lazy AWS clients in this package.
            import boto3  # type: ignore[import-untyped]

            self._client = boto3.client("connect", region_name=self._region)
        return self._client

    def _attributes(self, request: HandoverRequest) -> dict[str, str]:
        """The escalation, flattened into Connect contact attributes.

        This is the whole point of the integration. Without these the human gets a
        ringing phone and has to ask the caller everything they already told a
        machine, which is a worse experience than not transferring at all.
        """
        escalation = request.escalation
        attributes: dict[str, str] = {
            "escalationId": escalation.id,
            "escalationReason": str(escalation.reason),
            "callSessionId": escalation.call_session_id,
            "summary": request.summary[:MAX_ATTRIBUTE_CHARS],
        }
        if request.patient_name:
            attributes["patientName"] = request.patient_name
        if request.callback_phone:
            attributes["callbackPhone"] = request.callback_phone
        if escalation.patient_ref is not None and escalation.patient_ref.patient_id:
            attributes["patientId"] = escalation.patient_ref.patient_id
        if request.transcript_tail:
            attributes["transcriptTail"] = request.transcript_tail[:MAX_ATTRIBUTE_CHARS]
        for key, value in request.extra.items():
            attributes[key] = str(value)[:MAX_ATTRIBUTE_CHARS]
        return attributes

    def deliver(self, request: HandoverRequest) -> HandoverOutcome:
        """Create a Connect task for the escalation.

        A task rather than a call: it needs no claimed phone number, so it works on
        an instance the moment it exists, and it queues rather than ringing out
        when the doctor is already busy — which is the common case in a
        single-clinician practice.
        """
        name = request.summary[:512] or DEFAULT_TASK_NAME
        try:
            # No queue parameter here on purpose: StartTaskContact has none. The
            # contact flow decides which queue the task lands in, which is why the
            # flow id is the thing this transport is configured with.
            response = self._connect().start_task_contact(
                InstanceId=self._instance_id,
                ContactFlowId=self._contact_flow_id,
                Name=name,
                Description=request.summary[:4096],
                Attributes=self._attributes(request),
                # Connect deduplicates on this within a rolling window, so a
                # retried tool call cannot create two tasks for one escalation.
                ClientToken=request.escalation.id,
            )
        except Exception as exc:  # noqa: BLE001 - a transport must not raise
            logger.warning("Connect handover failed: %s", exc)
            return HandoverFailed(detail=f"{type(exc).__name__}: {exc}")

        contact_id = str(response.get("ContactId") or "")
        if not contact_id:
            return HandoverFailed(detail="Connect returned no ContactId")

        logger.info(
            "Connect task %s created for escalation %s",
            contact_id,
            request.escalation.id,
        )
        return HandoverDelivered(
            reference=contact_id,
            spoken_detail=(
                "I've passed this to a member of our team with your details, "
                "and someone will pick it up."
            ),
        )

    def call_back(self, request: HandoverRequest) -> HandoverOutcome:
        """Place an outbound call instead of queuing a task.

        Separate from :meth:`deliver` because it needs provisioning that a task does
        not — a claimed source number, and the destination country permitted on the
        instance. Calling an Indian mobile from a new instance is blocked until both
        are arranged, so this is the deliberate upgrade path rather than the default.
        """
        if not request.callback_phone:
            return HandoverFailed(detail="no callback number was captured")
        if not self._source_phone_number:
            return HandoverFailed(detail="no source phone number is configured")
        try:
            response = self._connect().start_outbound_voice_contact(
                InstanceId=self._instance_id,
                ContactFlowId=self._contact_flow_id,
                DestinationPhoneNumber=request.callback_phone,
                SourcePhoneNumber=self._source_phone_number,
                Attributes=self._attributes(request),
                ClientToken=request.escalation.id,
            )
        except Exception as exc:  # noqa: BLE001 - a transport must not raise
            logger.warning("Connect callback failed: %s", exc)
            return HandoverFailed(detail=f"{type(exc).__name__}: {exc}")

        contact_id = str(response.get("ContactId") or "")
        if not contact_id:
            return HandoverFailed(detail="Connect returned no ContactId")
        return HandoverDelivered(
            reference=contact_id,
            spoken_detail="We're calling you back now.",
        )


__all__ = ["ConnectHandoverTransport", "MAX_ATTRIBUTE_CHARS"]
