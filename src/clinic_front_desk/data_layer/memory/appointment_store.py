"""In-memory :class:`AppointmentStore` fake (task 3.2, Req 2, 4, 5, 16).

Owns both appointments and the slot state they move through. It is the strictest
enforcer of the provider-id rule (Req 16.7) and of write atomicity across the
appointment + slot pair (Req 4.9, 5.8, 16.6).

Because the interface exposes no ``create_slot`` operation (slots originate from
provider schedule configuration, not patient calls), the fake provides a
non-interface :meth:`seed_slot` / :meth:`seed_slots` helper so tests and callers
can populate the calendar. Seeding is setup, not a mutation, and emits no event.
"""

from __future__ import annotations

from collections.abc import Iterable

from clinic_front_desk.data_layer.events import ChangeEmitter, ChangeEntity, ChangeKind
from collections.abc import Sequence

from clinic_front_desk.data_layer.interfaces import (
    AppointmentStore,
    NewAppointment,
    OpenSlotSpan,
    SlotRelease,
)
from clinic_front_desk.models import (
    Appointment,
    AppointmentStatus,
    ISODate,
    Ok,
    Slot,
    SlotStatus,
    StoreResult,
)

from ._support import MemoryStoreBase, not_found_err, validation_err

_STORE = "MemoryAppointmentStore"


class MemoryAppointmentStore(AppointmentStore, MemoryStoreBase):
    """A dict-backed :class:`AppointmentStore` honouring the full store contract."""

    def __init__(self, emitter: ChangeEmitter | None = None) -> None:
        MemoryStoreBase.__init__(self, emitter)
        self._appointments: dict[str, Appointment] = {}
        self._slots: dict[str, Slot] = {}

    # -- test/setup seeding (not part of the interface) --------------------

    def seed_slot(self, slot: Slot) -> None:
        """Populate a bookable slot on the calendar (setup only, no event)."""
        self._slots[slot.id] = self._copy(slot)

    def seed_slots(self, slots: Iterable[Slot]) -> None:
        """Populate many slots at once (setup only, no event)."""
        for slot in slots:
            self.seed_slot(slot)

    # -- appointment reads/writes ------------------------------------------

    def create(self, a: NewAppointment) -> StoreResult[Appointment]:
        # Provider-id enforcement (Req 16.7): reject before any state changes so
        # a bad write is fully non-destructive (Req 2.8, 16.6).
        if not a.provider_id:
            return validation_err(_STORE, "provider_id", "appointment provider_id is required")
        self._appointments[a.id] = self._copy(a)
        self._emit(ChangeEntity.APPOINTMENT, a.id, ChangeKind.CREATED)
        return Ok(self._copy(self._appointments[a.id]))

    def get(self, id: str) -> StoreResult[Appointment | None]:
        found = self._appointments.get(id)
        return Ok(self._copy(found) if found is not None else None)

    def list_by_provider_and_day(
        self, provider_id: str, day: ISODate
    ) -> StoreResult[list[Appointment]]:
        matches = [
            self._copy(ap)
            for ap in self._appointments.values()
            if ap.provider_id == provider_id and ap.date == day
        ]
        matches.sort(key=lambda ap: (ap.time, ap.id))
        return Ok(matches)

    def list_by_patient(self, patient_id: str) -> StoreResult[list[Appointment]]:
        matches = [
            self._copy(ap)
            for ap in self._appointments.values()
            if ap.patient_id == patient_id
        ]
        matches.sort(key=lambda ap: (ap.date, ap.time, ap.id))
        return Ok(matches)

    def move(self, id: str, new_slot_id: str) -> StoreResult[Appointment]:
        # Validate everything before mutating so a failed reschedule leaves the
        # appointment and both slots unchanged (Req 4.9, 16.6).
        appt = self._appointments.get(id)
        if appt is None:
            return not_found_err(_STORE, f"appointment {id!r} not found")
        if not appt.provider_id:
            return validation_err(_STORE, "provider_id", "appointment provider_id is required")

        new_slot = self._slots.get(new_slot_id)
        if new_slot is None:
            return not_found_err(_STORE, f"slot {new_slot_id!r} not found")
        if not new_slot.provider_id:
            return validation_err(_STORE, "provider_id", "slot provider_id is required")

        old_slot_id = appt.slot_id
        # Moving onto a slot someone else holds is the same double-booking as
        # booking one directly, and this path had the same gap: it stamped the
        # target booked without asking whether it already was. Re-seating onto the
        # appointment's own slot stays allowed, since that changes nothing.
        if new_slot_id != old_slot_id and new_slot.status != SlotStatus.OPEN:
            return validation_err(
                _STORE,
                "new_slot_id",
                f"slot {new_slot_id!r} is {new_slot.status.value}, not open; "
                "the appointment cannot be moved onto it",
            )

        old_slot = self._slots.get(old_slot_id)

        # Commit: new slot -> booked, previously held slot -> open, appointment
        # points at the new slot (Req 4.7, 4.8).
        new_slot.status = SlotStatus.BOOKED
        if old_slot is not None and old_slot_id != new_slot_id:
            old_slot.status = SlotStatus.OPEN
        appt.slot_id = new_slot_id
        appt.status = AppointmentStatus.RESCHEDULED

        self._emit(ChangeEntity.APPOINTMENT, appt.id, ChangeKind.UPDATED)
        self._emit(ChangeEntity.SLOT, new_slot_id, ChangeKind.UPDATED)
        if old_slot is not None and old_slot_id != new_slot_id:
            self._emit(ChangeEntity.SLOT, old_slot_id, ChangeKind.UPDATED)
        return Ok(self._copy(appt))

    def remove(self, id: str) -> StoreResult[SlotRelease]:
        appt = self._appointments.get(id)
        if appt is None:
            return not_found_err(_STORE, f"appointment {id!r} not found")

        released_slot_id = appt.slot_id
        # Commit: drop the appointment and release its slot (Req 5.5, 5.7).
        del self._appointments[id]
        released = self._slots.get(released_slot_id)
        if released is not None:
            released.status = SlotStatus.OPEN

        self._emit(ChangeEntity.APPOINTMENT, id, ChangeKind.REMOVED)
        if released is not None:
            self._emit(ChangeEntity.SLOT, released_slot_id, ChangeKind.UPDATED)
        return Ok(SlotRelease(released_slot_id=released_slot_id))

    # -- slot reads/writes -------------------------------------------------

    def add_slots(self, slots: list[Slot]) -> StoreResult[list[Slot]]:
        for slot in slots:
            if not slot.provider_id:
                return validation_err(
                    _STORE, "provider_id", "slot provider_id is required"
                )
        # Validate every slot before writing any, so a bad entry cannot leave the
        # calendar half-published (Req 16.6).
        written: list[Slot] = []
        for slot in slots:
            existing = self._slots.get(slot.id)
            if existing is not None and existing.status in AppointmentStore.PRESERVED_ON_REPUBLISH:
                # Republishing must not strand a patient's appointment by reopening
                # a booked slot, nor hand back time the doctor blocked.
                continue
            self._slots[slot.id] = self._copy(slot)
            written.append(self._copy(slot))
        for slot in written:
            self._emit(ChangeEntity.SLOT, slot.id, ChangeKind.CREATED)
        return Ok(written)

    def list_slots_for_day(
        self, provider_id: str, day: ISODate
    ) -> StoreResult[list[Slot]]:
        matches = [
            self._copy(s)
            for s in self._slots.values()
            if s.provider_id == provider_id and s.start[:10] == day
        ]
        matches.sort(key=lambda s: (s.start, s.id))
        return Ok(matches)

    def get_slot(self, slot_id: str) -> StoreResult[Slot | None]:
        found = self._slots.get(slot_id)
        return Ok(self._copy(found) if found is not None else None)

    def list_open_slots(
        self,
        provider_id: str,
        service: str,
        from_date: ISODate,
        *,
        limit: int | None = None,
    ) -> StoreResult[list[Slot]]:
        matches = [
            self._copy(s)
            for s in self._slots.values()
            if s.provider_id == provider_id
            and s.service == service
            and s.status == SlotStatus.OPEN
            # Compared whole rather than by date prefix so a fuller bound
            # ("2026-09-10T15:00") starts partway through a day. A date-only
            # bound behaves exactly as before, because a start begins with its
            # own date.
            and s.start >= from_date
        ]
        matches.sort(key=lambda s: (s.start, s.id))
        if limit is not None:
            matches = matches[: max(0, limit)]
        return Ok(matches)

    def list_open_slots_for_provider(
        self,
        provider_id: str,
        from_bound: str,
        *,
        limit: int | None = None,
    ) -> StoreResult[list[Slot]]:
        matches = [
            self._copy(s)
            for s in self._slots.values()
            if s.provider_id == provider_id
            and s.status == SlotStatus.OPEN
            and s.start >= from_bound
        ]
        matches.sort(key=lambda s: (s.start, s.id))
        if limit is not None:
            matches = matches[: max(0, limit)]
        return Ok(matches)

    def open_slot_span(
        self, provider_id: str, from_date: ISODate
    ) -> StoreResult[OpenSlotSpan | None]:
        starts = [
            s.start
            for s in self._slots.values()
            if s.provider_id == provider_id
            and s.status == SlotStatus.OPEN
            and s.start >= from_date
        ]
        if not starts:
            return Ok(None)
        return Ok(OpenSlotSpan(earliest_start=min(starts), latest_start=max(starts)))

    def set_slot_status(self, slot_id: str, status: SlotStatus) -> StoreResult[Slot]:
        slot = self._slots.get(slot_id)
        if slot is None:
            return not_found_err(_STORE, f"slot {slot_id!r} not found")
        if not slot.provider_id:
            return validation_err(_STORE, "provider_id", "slot provider_id is required")
        slot.status = status
        self._emit(ChangeEntity.SLOT, slot_id, ChangeKind.UPDATED)
        return Ok(self._copy(slot))

    def claim_slot(self, slot_id: str) -> StoreResult[Slot]:
        """Atomically take an open slot for a booking.

        Atomic here by construction: the check and the write are one uninterrupted
        step within this method, and the store is a plain in-process dict. The
        DynamoDB implementation needs a condition expression to get the same
        property, which is why this is its own store method rather than a check the
        calling tool performs.
        """
        slot = self._slots.get(slot_id)
        if slot is None:
            return not_found_err(_STORE, f"slot {slot_id!r} not found")
        if not slot.provider_id:
            return validation_err(_STORE, "provider_id", "slot provider_id is required")
        if slot.status != SlotStatus.OPEN:
            return validation_err(
                _STORE,
                "slot_id",
                f"slot {slot_id!r} is {slot.status.value}, not open; "
                "it cannot be booked",
            )
        slot.status = SlotStatus.BOOKED
        self._emit(ChangeEntity.SLOT, slot_id, ChangeKind.UPDATED)
        return Ok(self._copy(slot))

    def remove_slots(self, slots: Sequence[Slot]) -> StoreResult[list[str]]:
        for slot in slots:
            if not slot.provider_id:
                return validation_err(_STORE, "provider_id", "slot provider_id is required")
            if slot.status == SlotStatus.BOOKED:
                return validation_err(
                    _STORE,
                    "status",
                    f"slot {slot.id!r} is booked; cancel the appointment before "
                    "removing the slot",
                )

        removed: list[str] = []
        for slot in slots:
            # An absent id is not an error: removal is safe to re-run.
            if self._slots.pop(slot.id, None) is not None:
                removed.append(slot.id)
        for slot_id in removed:
            self._emit(ChangeEntity.SLOT, slot_id, ChangeKind.REMOVED)
        return Ok(removed)

    def set_slot_statuses(
        self, slots: Sequence[Slot], status: SlotStatus
    ) -> StoreResult[list[Slot]]:
        for slot in slots:
            if not slot.provider_id:
                return validation_err(_STORE, "provider_id", "slot provider_id is required")

        updated: list[Slot] = []
        for slot in slots:
            held = self._slots.get(slot.id)
            if held is None:
                return not_found_err(_STORE, f"slot {slot.id!r} not found")
            held.status = status
            updated.append(self._copy(held))
        for slot in updated:
            self._emit(ChangeEntity.SLOT, slot.id, ChangeKind.UPDATED)
        return Ok(updated)


__all__ = ["MemoryAppointmentStore"]
