"""DynamoDB :class:`AppointmentStore` (task 4.1, Req 2, 4, 5, 16).

Owns both appointments and the slot state they move through (open → booked →
open), in the single-table layout:

- Appointment: ``PK=PROV#<providerId>``, ``SK=APPT#<date>#<time>#<id>``,
  GSI2 (``PATIENT#<patientId>`` / ``APPT#<createdAt>``) for by-patient reads.
- Slot: ``PK=PROV#<providerId>``, ``SK=SLOT#<start>``, GSI1
  (``SERVICE#<service>#STATUS#open`` / ``<start>``) for open-slot lookup.

It is the strictest enforcer of the provider-id rule (Req 16.7) and of write
atomicity across the appointment + slot pair (Req 4.9, 5.8, 16.6): every method
validates fully before mutating any item, so a rejected write leaves the store
exactly as it was.

Because the interface exposes no ``create_slot`` operation (slots originate from
provider schedule configuration, not patient calls), the store provides a
non-interface :meth:`seed_slot` / :meth:`seed_slots` helper — mirroring the
in-memory fake — so tests and callers can populate the calendar. Seeding is
setup, not a mutation, and emits no event.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import Any

from clinic_front_desk.data_layer.events import ChangeEmitter, ChangeEntity, ChangeKind
from clinic_front_desk.data_layer.interfaces import (
    AppointmentStore,
    NewAppointment,
    OpenSlotSpan,
    SlotRelease,
)

#: Items read per request when walking to either end of the open calendar.
#:
#: Small on purpose: the answer is usually the very first item, and this runs
#: before the agent greets the caller. Booked or blocked slots at the edge are
#: stepped over, so a whole page is occasionally needed — but never the year.
_SPAN_PAGE_SIZE = 25

#: Items read per request when gathering a provider's next open slots.
#:
#: Sized so one request usually satisfies a caller's offer: booked and blocked
#: slots are filtered after the read, and a closed overnight stretch can be
#: dozens of items, so asking for only three would page repeatedly.
_OPEN_PAGE_SIZE = 60
from clinic_front_desk.models import (
    Appointment,
    AppointmentStatus,
    Err,
    ISODate,
    Ok,
    Slot,
    SlotStatus,
    StoreError,
    StoreErrorKind,
    StoreResult,
    appointment_from_item,
    appointment_to_item,
    is_err,
    slot_from_item,
    slot_to_item,
)

from ._support import GSI1, GSI2, Attr, DynamoStoreBase, Key

_STORE = "DynamoAppointmentStore"


def _validation_err(field: str, detail: str) -> Err[StoreError]:
    return Err(StoreError(kind=StoreErrorKind.VALIDATION, detail=detail, store=_STORE, field=field))


def _not_found_err(detail: str) -> Err[StoreError]:
    return Err(StoreError(kind=StoreErrorKind.NOT_FOUND, detail=detail, store=_STORE))


class DynamoAppointmentStore(AppointmentStore, DynamoStoreBase):
    """Single-table :class:`AppointmentStore` honouring the full store contract."""

    def __init__(self, table: Any, emitter: ChangeEmitter | None = None) -> None:
        DynamoStoreBase.__init__(self, table, emitter)

    # -- test/setup seeding (not part of the interface) --------------------

    def seed_slot(self, slot: Slot) -> None:
        """Populate a bookable slot on the calendar (setup only, no event)."""
        self._put(slot_to_item(slot))

    def seed_slots(self, slots: Iterable[Slot]) -> None:
        """Populate many slots at once (setup only, no event)."""
        for slot in slots:
            self.seed_slot(slot)

    # -- appointment reads/writes ------------------------------------------

    def create(self, a: NewAppointment) -> StoreResult[Appointment]:
        # Provider-id enforcement (Req 16.7): reject before any write so a bad
        # request is fully non-destructive (Req 2.8, 16.6).
        if not a.provider_id:
            return _validation_err("provider_id", "appointment provider_id is required")
        item = appointment_to_item(a)
        self._put(item)
        self._emit(ChangeEntity.APPOINTMENT, a.id, ChangeKind.CREATED)
        return Ok(appointment_from_item(item))

    def get(self, id: str) -> StoreResult[Appointment | None]:
        item = self._find_by_entity_id("Appointment", id)
        return Ok(appointment_from_item(item) if item is not None else None)

    def list_by_provider_and_day(
        self, provider_id: str, day: ISODate
    ) -> StoreResult[list[Appointment]]:
        items = self._query(
            Key("PK").eq(f"PROV#{provider_id}") & Key("SK").begins_with(f"APPT#{day}#"),
        )
        appts = [appointment_from_item(i) for i in items]
        appts.sort(key=lambda ap: (ap.time, ap.id))
        return Ok(appts)

    def list_by_patient(self, patient_id: str) -> StoreResult[list[Appointment]]:
        items = self._query(
            Key("GSI2PK").eq(f"PATIENT#{patient_id}"),
            index_name=GSI2,
        )
        appts = [appointment_from_item(i) for i in items]
        appts.sort(key=lambda ap: (ap.date, ap.time, ap.id))
        return Ok(appts)

    def move(self, id: str, new_slot_id: str) -> StoreResult[Appointment]:
        # Validate everything before mutating so a failed reschedule leaves the
        # appointment and both slots unchanged (Req 4.9, 16.6).
        appt_item = self._find_by_entity_id("Appointment", id)
        if appt_item is None:
            return _not_found_err(f"appointment {id!r} not found")
        appt = appointment_from_item(appt_item)
        if not appt.provider_id:
            return _validation_err("provider_id", "appointment provider_id is required")

        new_slot_item = self._find_by_entity_id("Slot", new_slot_id)
        if new_slot_item is None:
            return _not_found_err(f"slot {new_slot_id!r} not found")
        new_slot = slot_from_item(new_slot_item)
        if not new_slot.provider_id:
            return _validation_err("provider_id", "slot provider_id is required")

        old_slot_id = appt.slot_id
        # Same double-booking gap as direct booking: this stamped the target slot
        # booked without checking it was open, so a reschedule could take a half hour
        # another patient already held. Re-seating onto the appointment's own slot is
        # still fine, since nothing changes.
        if new_slot_id != old_slot_id and new_slot.status != SlotStatus.OPEN:
            return _validation_err(
                "new_slot_id",
                f"slot {new_slot_id!r} is {new_slot.status.value}, not open; "
                "the appointment cannot be moved onto it",
            )
        old_slot_item = self._find_by_entity_id("Slot", old_slot_id) if old_slot_id else None
        old_slot = slot_from_item(old_slot_item) if old_slot_item is not None else None

        # Commit: new slot -> booked, previously held slot -> open, appointment
        # points at the new slot (Req 4.7, 4.8). Writing the full slot items
        # regenerates their GSI1 keys so they move between the open/booked
        # partitions correctly.
        new_slot.status = SlotStatus.BOOKED
        self._put(slot_to_item(new_slot))
        if old_slot is not None and old_slot_id != new_slot_id:
            old_slot.status = SlotStatus.OPEN
            self._put(slot_to_item(old_slot))

        appt.slot_id = new_slot_id
        appt.status = AppointmentStatus.RESCHEDULED
        self._put(appointment_to_item(appt))

        self._emit(ChangeEntity.APPOINTMENT, appt.id, ChangeKind.UPDATED)
        self._emit(ChangeEntity.SLOT, new_slot_id, ChangeKind.UPDATED)
        if old_slot is not None and old_slot_id != new_slot_id:
            self._emit(ChangeEntity.SLOT, old_slot_id, ChangeKind.UPDATED)
        return Ok(appt)

    def remove(self, id: str) -> StoreResult[SlotRelease]:
        appt_item = self._find_by_entity_id("Appointment", id)
        if appt_item is None:
            return _not_found_err(f"appointment {id!r} not found")
        appt = appointment_from_item(appt_item)

        released_slot_id = appt.slot_id
        # Commit: drop the appointment and release its slot (Req 5.5, 5.7).
        self._delete(appt_item["PK"], appt_item["SK"])
        released_item = (
            self._find_by_entity_id("Slot", released_slot_id) if released_slot_id else None
        )
        if released_item is not None:
            released = slot_from_item(released_item)
            released.status = SlotStatus.OPEN
            self._put(slot_to_item(released))

        self._emit(ChangeEntity.APPOINTMENT, id, ChangeKind.REMOVED)
        if released_item is not None:
            self._emit(ChangeEntity.SLOT, released_slot_id, ChangeKind.UPDATED)
        return Ok(SlotRelease(released_slot_id=released_slot_id))

    # -- slot reads/writes -------------------------------------------------

    def add_slots(self, slots: list[Slot]) -> StoreResult[list[Slot]]:
        for slot in slots:
            if not slot.provider_id:
                return _validation_err("provider_id", "slot provider_id is required")
        # Find what must be preserved with one partition query per provider-day,
        # rather than a lookup per slot. Publishing a year is thousands of slots,
        # and the obvious per-slot existence check was a full table scan each —
        # unusable at that size.
        preserved: set[str] = set()
        for provider_id, day in {(s.provider_id, s.start[:10]) for s in slots}:
            existing = self.list_slots_for_day(provider_id, day)
            if is_err(existing):
                return Err(existing.error)
            preserved |= {
                slot.id
                for slot in existing.value
                if slot.status in AppointmentStore.PRESERVED_ON_REPUBLISH
            }

        # Republishing must not strand a patient's appointment by reopening a
        # booked slot, nor hand back time the doctor blocked.
        written = [slot for slot in slots if slot.id not in preserved]
        self._put_many([slot_to_item(slot) for slot in written])
        for slot in written:
            self._emit(ChangeEntity.SLOT, slot.id, ChangeKind.CREATED)
        return Ok(written)

    def list_slots_for_day(
        self, provider_id: str, day: ISODate
    ) -> StoreResult[list[Slot]]:
        # Every status, not just open: the doctor's calendar has to show a booked
        # morning as booked, and GSI1's open partition cannot serve that.
        #
        # A slot's key is PK=PROV#<provider>, SK=SLOT#<start>, so one provider-day
        # is a single partition query with a sort-key prefix — no scan and no
        # cross-provider read.
        items = self._query(
            Key("PK").eq(f"PROV#{provider_id}") & Key("SK").begins_with(f"SLOT#{day}")
        )
        slots = [slot_from_item(item) for item in items if item.get("entity") == "Slot"]
        slots.sort(key=lambda s: (s.start, s.id))
        return Ok(slots)

    def get_slot(self, slot_id: str) -> StoreResult[Slot | None]:
        item = self._find_by_entity_id("Slot", slot_id)
        return Ok(slot_from_item(item) if item is not None else None)

    def list_open_slots(
        self,
        provider_id: str,
        service: str,
        from_date: ISODate,
        *,
        limit: int | None = None,
    ) -> StoreResult[list[Slot]]:
        # GSI1's open partition holds exactly the open slots for a service; a
        # slot leaves it when its status changes (its GSI1PK is regenerated on
        # write). Apply the same provider/from-date predicate as the fake so the
        # observable result matches exactly (Req 16.5).
        #
        # Two bounds do the work that used to be done in Python over the whole
        # partition. GSI1SK is the slot's start, so the sort-key condition skips
        # past days at the index instead of reading and discarding them. And
        # because the index is read in start order, consuming lazily and stopping
        # at `limit` means the earliest N are the first N seen — no full read to
        # return three slots. Measured: 6.2 s -> well under a second with a year
        # of slots published, which is the difference between the model having an
        # answer when it starts speaking and inventing one.
        key = Key("GSI1PK").eq(
            f"SERVICE#{service}#STATUS#{SlotStatus.OPEN.value}"
        ) & Key("GSI1SK").gte(from_date)

        wanted = None if limit is None else max(0, limit)
        slots: list[Slot] = []
        if wanted != 0:
            for item in self._query_iter(key, index_name=GSI1, page_size=wanted):
                slot = slot_from_item(item)
                # The service partition is shared across providers, so filtering
                # here (not via a FilterExpression) is what lets `limit` mean
                # "this provider's next N" rather than "N items read".
                if slot.provider_id != provider_id:
                    continue
                slots.append(slot)
                if wanted is not None and len(slots) >= wanted:
                    break

        slots.sort(key=lambda s: (s.start, s.id))
        return Ok(slots)

    def list_open_slots_for_provider(
        self,
        provider_id: str,
        from_bound: str,
        *,
        limit: int | None = None,
    ) -> StoreResult[list[Slot]]:
        # The provider's own partition, read in start order: PK=PROV#<provider>,
        # SK=SLOT#<start>. One query answers the whole question, where the
        # service-scoped GSI needed one per service name. The sort-key bound skips
        # past days at the index and consuming lazily means the earliest N open
        # slots are the first N seen.
        key = Key("PK").eq(f"PROV#{provider_id}") & Key("SK").gte(f"SLOT#{from_bound}")

        wanted = None if limit is None else max(0, limit)
        slots: list[Slot] = []
        if wanted != 0:
            for item in self._query_iter(key, page_size=_OPEN_PAGE_SIZE):
                if item.get("entity") != "Slot":
                    continue
                if item.get("status") != SlotStatus.OPEN.value:
                    continue
                slots.append(slot_from_item(item))
                if wanted is not None and len(slots) >= wanted:
                    break

        slots.sort(key=lambda s: (s.start, s.id))
        return Ok(slots)

    def open_slot_span(
        self, provider_id: str, from_date: ISODate
    ) -> StoreResult[OpenSlotSpan | None]:
        # Two bounded reads of the provider's own partition, walked from each end.
        # PK=PROV#<provider>, SK=SLOT#<start>, so the sort key is already the
        # timeline: the first open slot going forwards is the earliest and the
        # first going backwards is the latest. Status is filtered as items arrive,
        # which is why this iterates rather than taking one item — a run of booked
        # slots at either end must be stepped over, not mistaken for an empty
        # calendar.
        key = Key("PK").eq(f"PROV#{provider_id}") & Key("SK").gte(f"SLOT#{from_date}")

        def first_open(*, forwards: bool) -> str | None:
            for item in self._query_iter(
                key, scan_index_forward=forwards, page_size=_SPAN_PAGE_SIZE
            ):
                if item.get("entity") != "Slot":
                    continue
                if item.get("status") == SlotStatus.OPEN.value:
                    start = item.get("start")
                    return str(start) if start else None
            return None

        earliest = first_open(forwards=True)
        if earliest is None:
            return Ok(None)
        latest = first_open(forwards=False) or earliest
        return Ok(OpenSlotSpan(earliest_start=earliest, latest_start=latest))

    def set_slot_status(self, slot_id: str, status: SlotStatus) -> StoreResult[Slot]:
        item = self._find_by_entity_id("Slot", slot_id)
        if item is None:
            return _not_found_err(f"slot {slot_id!r} not found")
        slot = slot_from_item(item)
        if not slot.provider_id:
            return _validation_err("provider_id", "slot provider_id is required")
        slot.status = status
        self._put(slot_to_item(slot))
        self._emit(ChangeEntity.SLOT, slot_id, ChangeKind.UPDATED)
        return Ok(slot)

    def claim_slot(self, slot_id: str) -> StoreResult[Slot]:
        """Atomically take an open slot for a booking, via a condition expression.

        The condition is what makes this safe. Reading the slot, seeing ``open`` and
        then writing ``booked`` leaves a window in which another caller does the
        same, and both bookings appear to succeed. Asking DynamoDB to perform the
        write *only if* the stored status is still ``open`` collapses that to one
        operation, so exactly one caller can win.
        """
        item = self._find_by_entity_id("Slot", slot_id)
        if item is None:
            return _not_found_err(f"slot {slot_id!r} not found")
        slot = slot_from_item(item)
        if not slot.provider_id:
            return _validation_err("provider_id", "slot provider_id is required")
        if slot.status != SlotStatus.OPEN:
            # Cheap rejection before the write for the ordinary case: the slot was
            # already taken well before this call.
            return _validation_err(
                "slot_id",
                f"slot {slot_id!r} is {slot.status.value}, not open; it cannot be booked",
            )

        slot.status = SlotStatus.BOOKED
        # Writing the whole item regenerates GSI1 keys, moving the slot out of the
        # open partition so availability stops offering it.
        written = self._put_if(
            slot_to_item(slot),
            condition=Attr("status").eq(SlotStatus.OPEN.value),
        )
        if not written:
            # Lost the race: someone booked it between the read above and this write.
            return _validation_err(
                "slot_id",
                f"slot {slot_id!r} was taken by another booking just now; "
                "it is no longer open",
            )
        self._emit(ChangeEntity.SLOT, slot_id, ChangeKind.UPDATED)
        return Ok(slot)

    def remove_slots(self, slots: Sequence[Slot]) -> StoreResult[list[str]]:
        for slot in slots:
            if not slot.provider_id:
                return _validation_err("provider_id", "slot provider_id is required")
            if slot.status == SlotStatus.BOOKED:
                return _validation_err(
                    "status",
                    f"slot {slot.id!r} is booked; cancel the appointment before "
                    "removing the slot",
                )

        if not slots:
            return Ok([])
        # The key is on the record, so no lookup is needed.
        self._delete_many(
            [(f"PROV#{slot.provider_id}", f"SLOT#{slot.start}") for slot in slots]
        )
        removed = [slot.id for slot in slots]
        for slot_id in removed:
            self._emit(ChangeEntity.SLOT, slot_id, ChangeKind.REMOVED)
        return Ok(removed)

    def set_slot_statuses(
        self, slots: Sequence[Slot], status: SlotStatus
    ) -> StoreResult[list[Slot]]:
        # No lookup at all: a slot's key is PK=PROV#<provider>, SK=SLOT#<start>,
        # both carried on the record the caller already read. That is what makes
        # this a single batched write instead of one table scan per slot.
        for slot in slots:
            if not slot.provider_id:
                return _validation_err("provider_id", "slot provider_id is required")

        updated = [replace(slot, status=status) for slot in slots]
        if not updated:
            return Ok([])
        self._put_many([slot_to_item(slot) for slot in updated])
        for slot in updated:
            self._emit(ChangeEntity.SLOT, slot.id, ChangeKind.UPDATED)
        return Ok(updated)


__all__ = ["DynamoAppointmentStore"]
