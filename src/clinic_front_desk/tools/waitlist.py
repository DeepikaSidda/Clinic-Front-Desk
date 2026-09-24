"""Waitlist Strands tools: ``add_to_waitlist`` and ``fill_gap_from_waitlist``.

Task 6.10 (Req 7.1, 7.2, 7.4, 7.5, 8.2, 8.3, 8.4, 8.5, 8.6).

Both tools are pure-ish functions over the Data_Layer interfaces: deterministic
given the store state and the (injectable) id/clock inputs, returning a
discriminated :data:`~clinic_front_desk.models.ToolResult`
(``Ok`` | ``Err``) so the agent/caller can branch on the failure ``kind``
(design "Strands Tool Suite").

``add_to_waitlist`` (Req 7):
    Records a waitlist entry with active-duplicate suppression. If the patient
    already holds an active entry for the same service *and* preferred slot type,
    it declines with a :class:`~clinic_front_desk.models.Duplicate` error and
    creates no second entry (Req 7.5). On success the returned
    :class:`~clinic_front_desk.models.WaitlistEntry` carries the confirmation
    fields the voice layer reads back — the requested service and preferred slot
    type (Req 7.2). A store failure surfaces as a
    :class:`~clinic_front_desk.models.StoreFailure` and, because the store write
    is atomic, no partial entry is retained (Req 7.4).

``fill_gap_from_waitlist`` (Req 8):
    Selects the earliest matching waitlisted patient for an open slot's service
    (the store returns entries ascending by ``added_at``/``seq``, so the first is
    earliest — Req 8.2), books them into the slot as an appointment (Req 8.3),
    and removes their waitlist entry (Req 8.4). When no waitlisted patient
    requests the slot's service, it takes no action and returns a
    :class:`~clinic_front_desk.models.NotFound` so the caller can leave the slot
    open and record no fill (Req 8.6). Every persistence failure path is
    compensated so the slot is left open and the waitlist entry is left unchanged
    (Req 8.5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable
from uuid import uuid4

from clinic_front_desk.data_layer.interfaces import AppointmentStore, WaitlistStore
from clinic_front_desk.models import (
    Appointment,
    AppointmentStatus,
    Duplicate,
    Err,
    ISODateTime,
    NotFound,
    Ok,
    SlotStatus,
    StoreFailure,
    ToolResult,
    WaitlistEntry,
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


@dataclass(frozen=True)
class GapFillResult:
    """Successful result of :func:`fill_gap_from_waitlist`.

    Mirrors the design's ``{ appointment, removedWaitlistEntryId }`` shape
    (design "Strands Tool Suite").
    """

    appointment: Appointment
    removed_waitlist_entry_id: str


def add_to_waitlist(
    waitlist_store: WaitlistStore,
    *,
    patient_id: str,
    service: str,
    preferred_slot_type: str,
    entry_id: str | None = None,
    added_at: ISODateTime | None = None,
    clock: Clock = _default_clock,
    id_gen: IdGen = _default_id,
) -> ToolResult[WaitlistEntry]:
    """Record a waitlist entry, suppressing an active duplicate (Req 7.1, 7.2, 7.4, 7.5).

    Args:
        waitlist_store: The waitlist data-access interface.
        patient_id: The patient requesting waitlist placement.
        service: The requested (already offered-matched) service.
        preferred_slot_type: The slot type the patient prefers.
        entry_id: Optional explicit id for the new entry (defaults to a fresh id).
        added_at: Optional explicit ``added_at`` timestamp (defaults to now).
        clock: Injectable clock for ``added_at`` when not supplied.
        id_gen: Injectable id generator for ``entry_id`` when not supplied.

    Returns:
        ``Ok(WaitlistEntry)`` on success — the persisted entry carrying the
        service and preferred slot type to confirm to the patient (Req 7.2).
        ``Err(Duplicate)`` when the patient already has an active entry for this
        service and slot type (Req 7.5). ``Err(StoreFailure)`` on a lookup or
        write failure; on a write failure the store is atomic so no partial entry
        is retained (Req 7.4).
    """
    # Active-duplicate suppression (Req 7.5): look before writing so we never
    # create a second active entry for the same patient/service/slot type.
    existing = waitlist_store.find_active(patient_id, service, preferred_slot_type)
    if is_err(existing):
        return Err(StoreFailure(store="WaitlistStore", detail=existing.error.detail))
    if existing.value is not None:
        return Err(Duplicate(entry_id=existing.value.id))

    # Build the entry. The store assigns its own monotonic ``seq`` (Req 7.3), so
    # the value supplied here is only a suggestion.
    entry = WaitlistEntry(
        id=entry_id if entry_id is not None else id_gen(),
        patient_id=patient_id,
        service=service,
        preferred_slot_type=preferred_slot_type,
        added_at=added_at if added_at is not None else clock(),
        seq=0,
        active=True,
    )

    added = waitlist_store.add(entry)
    if is_err(added):
        # Atomic store: a failed add leaves no partial entry (Req 7.4).
        return Err(StoreFailure(store="WaitlistStore", detail=added.error.detail))

    return Ok(added.value)


def fill_gap_from_waitlist(
    waitlist_store: WaitlistStore,
    appointment_store: AppointmentStore,
    *,
    slot_id: str,
    appointment_id: str | None = None,
    clock: Clock = _default_clock,
    id_gen: IdGen = _default_id,
) -> ToolResult[GapFillResult]:
    """Fill an open slot with the earliest matching waitlisted patient (Req 8.2–8.6).

    Selects the matching patient holding the earliest waitlist position for the
    slot's service (Req 8.2), books them into the slot as an appointment
    (Req 8.3), and removes their waitlist entry (Req 8.4).

    Args:
        waitlist_store: The waitlist data-access interface.
        appointment_store: The appointment/slot data-access interface.
        slot_id: The open slot to fill.
        appointment_id: Optional explicit id for the created appointment.
        clock: Injectable clock for the appointment timestamps.
        id_gen: Injectable id generator for the appointment id.

    Returns:
        ``Ok(GapFillResult)`` with the created appointment and the removed
        waitlist entry id on success. ``Err(NotFound)`` when the slot does not
        exist or no waitlisted patient requests the slot's service — the caller
        leaves the slot open and records no fill (Req 8.6). ``Err(StoreFailure)``
        on any persistence failure; every such path is compensated so the slot is
        left open and the waitlist entry is left unchanged (Req 8.5).
    """
    # Resolve the slot; it defines the service to match and the provider to book.
    slot_result = appointment_store.get_slot(slot_id)
    if is_err(slot_result):
        return Err(StoreFailure(store="AppointmentStore", detail=slot_result.error.detail))
    slot = slot_result.value
    if slot is None:
        return Err(NotFound(detail=f"slot {slot_id!r} not found"))

    # Earliest matching waitlisted patient for the slot's service (Req 8.2). The
    # store returns entries ascending by added_at/seq, so index 0 is earliest.
    ordered = waitlist_store.list_by_service_ordered(slot.service)
    if is_err(ordered):
        return Err(StoreFailure(store="WaitlistStore", detail=ordered.error.detail))
    if not ordered.value:
        # No match: leave the slot open, record no fill action (Req 8.6).
        return Err(
            NotFound(detail=f"no waitlisted patient requests service {slot.service!r}")
        )
    selected = ordered.value[0]

    now = clock()
    appointment = Appointment(
        id=appointment_id if appointment_id is not None else id_gen(),
        provider_id=slot.provider_id,
        patient_id=selected.patient_id,
        service=slot.service,
        slot_id=slot.id,
        date=slot.start[:10],
        time=slot.start[11:16],
        status=AppointmentStatus.BOOKED,
        created_at=now,
        updated_at=now,
    )

    # Claim the slot first, and conditionally (Req 8.3). The doctor approves a
    # gap-fill from a Decision that may have been detected minutes ago, so the slot
    # can easily have been taken by a caller in between — this is the path most
    # likely to race of the three, not the least. Overwriting the status here would
    # have booked the waitlisted patient on top of whoever rang in.
    claimed = appointment_store.claim_slot(slot.id)
    if is_err(claimed):
        return Err(StoreFailure(store="AppointmentStore", detail=claimed.error.detail))

    created = appointment_store.create(appointment)
    if is_err(created):
        # Release the slot: it is held for an appointment that was never written.
        appointment_store.set_slot_status(slot.id, SlotStatus.OPEN)
        return Err(StoreFailure(store="AppointmentStore", detail=created.error.detail))
    booked = created.value

    # Remove the filled patient's waitlist entry (Req 8.4).
    removed = waitlist_store.remove(selected.id)
    if is_err(removed):
        # Compensate: undo the appointment (releasing the slot to open) so both
        # the slot and the waitlist entry are left unchanged (Req 8.5).
        appointment_store.remove(booked.id)
        return Err(StoreFailure(store="WaitlistStore", detail=removed.error.detail))

    return Ok(GapFillResult(appointment=booked, removed_waitlist_entry_id=selected.id))


__all__ = [
    "Clock",
    "IdGen",
    "GapFillResult",
    "add_to_waitlist",
    "fill_gap_from_waitlist",
]
