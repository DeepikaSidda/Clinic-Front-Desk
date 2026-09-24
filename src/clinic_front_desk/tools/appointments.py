"""Appointment lifecycle Strands tools: ``book_appointment``, ``reschedule``,
and ``cancel`` (task 6.4, Req 2.5, 2.6, 2.8, 4.7, 4.8, 4.9, 5.5, 5.7, 5.8).

These three tools sit between the Voice_Front_Desk orchestration and the
:class:`~clinic_front_desk.data_layer.interfaces.AppointmentStore`. Each is a
thin, deterministic function over the store that returns the discriminated
``Result`` shape (``Ok`` | ``Err``) the agent branches on, mapping every
``StoreError`` to the matching :data:`~clinic_front_desk.models.ToolError`
``kind`` from the design's Strands Tool Suite.

Slot-lifecycle contract:

- ``book_appointment`` writes the appointment and sets its slot to ``booked``.
  If either step fails the tool leaves **no partial appointment** — a failure
  to book the slot after the appointment was written is rolled back by removing
  the just-created appointment (Req 2.5, 2.6, 2.8).
- ``reschedule`` moves the appointment onto a new slot; the store performs the
  move atomically so the new slot becomes ``booked`` and the previously held
  slot becomes ``open`` on success, and on failure both slots and the
  appointment are left unchanged (Req 4.7, 4.8, 4.9).
- ``cancel`` removes the appointment and releases its slot to ``open``; on
  failure the appointment is retained unchanged (Req 5.5, 5.7, 5.8).

The ``AppointmentStore.move`` and ``AppointmentStore.remove`` operations are
already atomic across the appointment + slot pair, so ``reschedule`` and
``cancel`` inherit their non-destruction guarantee directly. Only
``book_appointment`` composes two writes, so it is the only tool that performs
an explicit compensating rollback.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from clinic_front_desk.data_layer.interfaces import AppointmentStore
from clinic_front_desk.models import (
    Appointment,
    AppointmentStatus,
    Err,
    ISODateTime,
    NotFound,
    Ok,
    SlotStatus,
    StoreError,
    StoreErrorKind,
    StoreFailure,
    ToolError,
    ToolResult,
    Validation,
    is_err,
)

_STORE_LABEL = "AppointmentStore"


# ---------------------------------------------------------------------------
# Tool result payloads (design "Strands Tool Suite")
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BookingResult:
    """Successful ``book_appointment`` payload: the persisted appointment (Req 2.5, 2.6)."""

    appointment: Appointment


@dataclass(frozen=True)
class RescheduleResult:
    """Successful ``reschedule`` payload: the moved appointment and the released slot (Req 4.7, 4.8)."""

    appointment: Appointment
    released_slot_id: str


@dataclass(frozen=True)
class CancelResult:
    """Successful ``cancel`` payload: the slot returned to ``open`` (Req 5.5, 5.7)."""

    released_slot_id: str


# ---------------------------------------------------------------------------
# Store-error -> tool-error mapping
# ---------------------------------------------------------------------------


def _to_tool_error(error: StoreError) -> ToolError:
    """Translate a Data_Layer :class:`StoreError` into the tool-facing :data:`ToolError`."""
    if error.kind is StoreErrorKind.NOT_FOUND:
        return NotFound(detail=error.detail)
    if error.kind is StoreErrorKind.VALIDATION:
        return Validation(field=error.field or "", detail=error.detail)
    # StoreErrorKind.STORE_FAILURE and any future kinds surface as a store failure.
    return StoreFailure(store=error.store or _STORE_LABEL, detail=error.detail)


def _now_iso() -> ISODateTime:
    """Current UTC timestamp as an ISO-8601 string (``...Z``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# book_appointment (Req 2.5, 2.6, 2.8)
# ---------------------------------------------------------------------------


def book_appointment(
    store: AppointmentStore,
    *,
    provider_id: str,
    patient_id: str,
    slot_id: str,
    service: str,
    appointment_id: str | None = None,
    now: ISODateTime | None = None,
) -> ToolResult[BookingResult]:
    """Write an appointment to the provider's calendar and book its slot.

    Steps: resolve the slot, create the appointment, then mark the slot
    ``booked``. ``provider_id`` is required (Req 16.7). On any failure the tool
    leaves no partial appointment (Req 2.8): if the slot-status write fails after
    the appointment was created, the appointment is removed as compensation.

    Returns:
        ``Ok(BookingResult)`` on success, or ``Err(ToolError)`` with the mapped
        failure ``kind``.
    """
    if not provider_id:
        return Err(Validation(field="provider_id", detail="provider_id is required (Req 16.7)"))
    if not patient_id:
        return Err(Validation(field="patient_id", detail="patient_id is required"))
    if not service:
        return Err(Validation(field="service", detail="service is required"))

    # Resolve the target slot up front so a missing slot fails before any write.
    slot_result = store.get_slot(slot_id)
    if is_err(slot_result):
        return Err(_to_tool_error(slot_result.error))
    slot = slot_result.value
    if slot is None:
        return Err(NotFound(detail=f"slot {slot_id!r} not found"))

    # Claim the slot *before* writing the appointment, and conditionally.
    #
    # This used to read the slot, create the appointment, then overwrite the slot's
    # status to booked — without ever checking it was open. Booking an already-booked
    # half hour therefore succeeded: the second appointment was written, the slot was
    # re-stamped booked, and the first patient kept an appointment pointing at the
    # same time. Two people, one slot, and nothing in the data admitting it. The
    # doctor would have found out when they both arrived.
    #
    # Claiming first also makes the slot the point of mutual exclusion, so two
    # simultaneous callers cannot both get an appointment row; the loser is told the
    # slot went, which is true and actionable.
    claim_result = store.claim_slot(slot_id)
    if is_err(claim_result):
        return Err(_to_tool_error(claim_result.error))

    timestamp = now or _now_iso()
    appointment = Appointment(
        id=appointment_id or str(uuid.uuid4()),
        provider_id=provider_id,
        patient_id=patient_id,
        service=service,
        slot_id=slot_id,
        date=slot.start[:10],
        time=slot.start[11:16],
        status=AppointmentStatus.BOOKED,
        created_at=timestamp,
        updated_at=timestamp,
    )

    create_result = store.create(appointment)
    if is_err(create_result):
        # Compensate the claim: release the slot rather than leaving it booked
        # against an appointment that was never written, which would silently
        # withdraw a half hour from the calendar (Req 2.8).
        store.set_slot_status(slot_id, SlotStatus.OPEN)
        return Err(_to_tool_error(create_result.error))
    created = create_result.value

    return Ok(BookingResult(appointment=created))


# ---------------------------------------------------------------------------
# reschedule (Req 4.7, 4.8, 4.9)
# ---------------------------------------------------------------------------


def reschedule(
    store: AppointmentStore,
    *,
    appointment_id: str,
    new_slot_id: str,
) -> ToolResult[RescheduleResult]:
    """Move an appointment to ``new_slot_id``, releasing its previous slot.

    The store's ``move`` performs the appointment update and both slot-status
    changes atomically: on success the new slot is ``booked`` and the previously
    held slot is ``open`` (Req 4.7, 4.8); on failure the appointment and both
    slots are left unchanged (Req 4.9).

    Returns:
        ``Ok(RescheduleResult)`` carrying the moved appointment and the released
        slot id, or ``Err(ToolError)`` on failure.
    """
    # Capture the currently held slot so we can report what was released.
    lookup = store.get(appointment_id)
    if is_err(lookup):
        return Err(_to_tool_error(lookup.error))
    existing = lookup.value
    if existing is None:
        return Err(NotFound(detail=f"appointment {appointment_id!r} not found"))
    previous_slot_id = existing.slot_id

    move_result = store.move(appointment_id, new_slot_id)
    if is_err(move_result):
        return Err(_to_tool_error(move_result.error))

    released_slot_id = previous_slot_id if previous_slot_id != new_slot_id else new_slot_id
    return Ok(
        RescheduleResult(
            appointment=move_result.value,
            released_slot_id=released_slot_id,
        )
    )


# ---------------------------------------------------------------------------
# cancel (Req 5.5, 5.7, 5.8)
# ---------------------------------------------------------------------------


def cancel(
    store: AppointmentStore,
    *,
    appointment_id: str,
) -> ToolResult[CancelResult]:
    """Remove an appointment and release its slot to ``open``.

    The store's ``remove`` drops the appointment and releases its slot
    atomically (Req 5.5, 5.7); on failure the appointment is retained unchanged
    (Req 5.8).

    Returns:
        ``Ok(CancelResult)`` carrying the released slot id, or ``Err(ToolError)``
        on failure.
    """
    remove_result = store.remove(appointment_id)
    if is_err(remove_result):
        return Err(_to_tool_error(remove_result.error))
    return Ok(CancelResult(released_slot_id=remove_result.value.released_slot_id))


__all__ = [
    "BookingResult",
    "RescheduleResult",
    "CancelResult",
    "book_appointment",
    "reschedule",
    "cancel",
]
