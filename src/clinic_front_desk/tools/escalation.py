"""Escalation Strands tool: ``flag_for_human``.

Task 6.13 (Req 9.4, 9.9).

Like the other tools in this suite, ``flag_for_human`` is a pure-ish function
over the Data_Layer: deterministic given the store state and the (injectable)
id/clock inputs, returning a discriminated
:data:`~clinic_front_desk.models.ToolResult` (``Ok`` | ``Err``) so the
agent/caller can branch on the failure ``kind`` (design "Strands Tool Suite").

``flag_for_human`` (Req 9):
    Persists an :class:`~clinic_front_desk.models.Escalation` carrying the
    escalation ``reason`` — one of the four
    :class:`~clinic_front_desk.models.EscalationReason` categories
    {``clinical_content``, ``outside_admin_rules``, ``patient_distress``,
    ``patient_request``} — the originating ``call_session_id`` and free-text
    ``context``, and the patient identity when known (Req 9.4). A failed write
    surfaces as a :class:`~clinic_front_desk.models.StoreFailure` and, because
    the store write is atomic, no partial escalation is retained; the caller
    keeps the Call_Session context and offers to take a message (Req 9.9).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable
from uuid import uuid4

from clinic_front_desk.data_layer.interfaces import EscalationStore
from clinic_front_desk.models import (
    Err,
    Escalation,
    EscalationReason,
    ISODateTime,
    Ok,
    PatientRef,
    StoreFailure,
    ToolResult,
    is_err,
)

#: A clock returning the current time as an ISO-8601 UTC string. Injectable so
#: tests can pin timestamps deterministically.
Clock = Callable[[], str]

#: An id generator returning a fresh unique string. Injectable for deterministic
#: tests.
IdGen = Callable[[], str]


def _default_clock() -> str:
    return datetime.now(UTC).isoformat()


def _default_id() -> str:
    return uuid4().hex


def flag_for_human(
    escalation_store: EscalationStore,
    *,
    reason: EscalationReason,
    call_session_id: str,
    context: str,
    patient_ref: PatientRef | None = None,
    escalation_id: str | None = None,
    created_at: ISODateTime | None = None,
    clock: Clock = _default_clock,
    id_gen: IdGen = _default_id,
) -> ToolResult[Escalation]:
    """Record an escalation routing a request to a human (Req 9.4, 9.9).

    Args:
        escalation_store: The escalation data-access interface.
        reason: The escalation category — one of the four
            :class:`~clinic_front_desk.models.EscalationReason` values
            {clinical_content, outside_admin_rules, patient_distress,
            patient_request} (Req 9.4).
        call_session_id: The originating Call_Session, recorded as context so a
            human can pick the request up (Req 9.4).
        context: Free-text Call_Session context describing the escalated request.
        patient_ref: The patient identity when known (Req 9.4); omitted when the
            patient has not yet been identified.
        escalation_id: Optional explicit id for the new record (defaults to a
            fresh id).
        created_at: Optional explicit ``created_at`` timestamp (defaults to now).
        clock: Injectable clock for ``created_at`` when not supplied.
        id_gen: Injectable id generator for ``escalation_id`` when not supplied.

    Returns:
        ``Ok(Escalation)`` on success — the persisted escalation carrying the
        reason, call-session context, and known patient identity (Req 9.4).
        ``Err(StoreFailure)`` when the write fails; the store is atomic so no
        partial escalation is retained and the caller keeps the Call_Session
        context to offer a message (Req 9.9).
    """
    escalation = Escalation(
        id=escalation_id if escalation_id is not None else id_gen(),
        reason=reason,
        call_session_id=call_session_id,
        context=context,
        created_at=created_at if created_at is not None else clock(),
        patient_ref=patient_ref,
    )

    created = escalation_store.create(escalation)
    if is_err(created):
        # Atomic store: a failed create leaves no partial escalation (Req 9.9).
        return Err(StoreFailure(store="EscalationStore", detail=created.error.detail))

    return Ok(created.value)


__all__ = [
    "Clock",
    "IdGen",
    "flag_for_human",
]
