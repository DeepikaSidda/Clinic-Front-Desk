"""``AppointmentStore`` — appointment and slot-lifecycle data access (Req 2, 4, 5, 16).

This interface owns both appointments and the slot state that appointments
move through (open → booked → open). It is the primary schedule-owning store,
so it is the strictest enforcer of the provider-id rule (Req 16.7).

Contract (applies to every method):
    - **Atomicity / non-destruction (Req 16.6).** A write that fails returns an
      :class:`~clinic_front_desk.models.Err` carrying a
      :class:`~clinic_front_desk.models.StoreError` and leaves *every* prior
      record unchanged — no partial appointment, no half-moved slot. Callers
      that see an ``Err`` can assume the store is exactly as it was before the
      call.
    - **Provider-id enforcement (Req 16.7).** Any write that creates or moves an
      appointment or mutates a slot must carry a non-empty ``provider_id`` on the
      affected record. A missing/blank provider id is rejected with a
      ``StoreError`` of kind ``validation`` (``field="provider_id"``) and no
      write occurs. (The entity dataclasses reject a blank ``provider_id`` at
      construction, so this guards inputs that bypass construction, e.g. a
      ``move`` targeting a slot whose provider cannot be resolved.)
    - **Change emission (Req 16.6, 15.4).** On success — and only on success —
      the store emits a :class:`~clinic_front_desk.data_layer.events.ChangeEvent`
      via its configured
      :class:`~clinic_front_desk.data_layer.events.ChangeEmitter`: ``create`` →
      ``CREATED`` appointment; ``move``/``remove``/``set_slot_status`` →
      ``UPDATED`` appointment and/or ``UPDATED`` slot; ``remove`` also emits a
      ``REMOVED`` appointment.
    - **Empty initialization (Req 16.4).** Before any write, ``get`` returns
      ``Ok(None)`` and the ``list_*`` reads return ``Ok([])``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from clinic_front_desk.models import (
    Appointment,
    ISODate,
    Slot,
    SlotStatus,
    StoreResult,
)

from .inputs import NewAppointment, OpenSlotSpan, SlotRelease


class AppointmentStore(ABC):
    """Read/write interface for :class:`~clinic_front_desk.models.Appointment`
    records and their :class:`~clinic_front_desk.models.Slot` state."""

    @abstractmethod
    def create(self, a: NewAppointment) -> StoreResult[Appointment]:
        """Write a new appointment to the provider's calendar (Req 2.5).

        Rejects a missing ``provider_id`` (Req 16.7). On failure, no partial
        appointment is retained (Req 2.8). On success, emits a ``CREATED``
        appointment change event and returns the persisted appointment.
        """
        raise NotImplementedError

    @abstractmethod
    def get(self, id: str) -> StoreResult[Appointment | None]:
        """Return the appointment with ``id``, or ``Ok(None)`` if none exists."""
        raise NotImplementedError

    @abstractmethod
    def list_by_provider_and_day(
        self, provider_id: str, day: ISODate
    ) -> StoreResult[list[Appointment]]:
        """Return the provider's appointments for ``day`` (Req 15.1)."""
        raise NotImplementedError

    @abstractmethod
    def list_by_patient(self, patient_id: str) -> StoreResult[list[Appointment]]:
        """Return every appointment for a patient (Req 4.1, 5.1)."""
        raise NotImplementedError

    @abstractmethod
    def move(self, id: str, new_slot_id: str) -> StoreResult[Appointment]:
        """Reschedule: move the appointment to ``new_slot_id`` (Req 4.7).

        On success the new slot becomes ``booked`` and the previously held slot
        becomes ``open`` (Req 4.8). On failure both slots and the appointment
        are left unchanged (Req 4.9).
        """
        raise NotImplementedError

    @abstractmethod
    def remove(self, id: str) -> StoreResult[SlotRelease]:
        """Cancel: remove the appointment and release its slot (Req 5.5, 5.7).

        Returns the released slot id. On failure the appointment is retained
        unchanged (Req 5.8).
        """
        raise NotImplementedError

    #: Slot statuses that republishing a day must never overwrite.
    #:
    #: Defined on the interface rather than in each store so both backends preserve
    #: exactly the same slots and cannot drift (Req 16.5).
    PRESERVED_ON_REPUBLISH: frozenset[SlotStatus] = frozenset(
        {SlotStatus.BOOKED, SlotStatus.BLOCKED}
    )

    @abstractmethod
    def add_slots(self, slots: list[Slot]) -> StoreResult[list[Slot]]:
        """Publish bookable slots on a provider's calendar.

        The doctor-only write that creates availability. It is on this interface
        rather than left to a seeding helper because publishing a day is a real
        operation the portal performs, and it needs the same typed, Result-returning
        contract as every other write.

        No Strands tool calls this, and none should: the patient-facing path may
        book and release slots but must never be able to *create* one, or a caller
        pressing for an earlier time could eventually be offered availability the
        doctor never opened.

        Writing an id that already exists **replaces** that slot, so republishing a
        day is idempotent rather than accumulating duplicates — except that a slot
        which is already ``booked`` or ``blocked`` is left untouched. Resetting a
        booked slot to open would strand a patient's appointment on a slot claiming
        to be free, and resetting a blocked one would quietly hand back time the
        doctor deliberately took off the calendar.

        Returns:
            ``Ok`` with the slots actually written (excluding any skipped because
            they were booked or blocked), or ``Err(StoreError)`` having written
            nothing.
        """
        raise NotImplementedError

    @abstractmethod
    def list_slots_for_day(
        self, provider_id: str, day: ISODate
    ) -> StoreResult[list[Slot]]:
        """Return every slot for a provider on ``day``, whatever its status.

        Distinct from :meth:`list_open_slots`, which exists to answer "what can I
        offer this caller" and therefore hides booked slots. The doctor's calendar
        needs the opposite: the whole day including what is already taken, or the
        portal would show a booked morning as an empty one.
        """
        raise NotImplementedError

    @abstractmethod
    def get_slot(self, slot_id: str) -> StoreResult[Slot | None]:
        """Return the slot with ``slot_id``, or ``Ok(None)`` if none exists."""
        raise NotImplementedError

    @abstractmethod
    def list_open_slots(
        self,
        provider_id: str,
        service: str,
        from_date: ISODate,
        *,
        limit: int | None = None,
    ) -> StoreResult[list[Slot]]:
        """Return open slots for a service from ``from_date`` onward (Req 2.2, 4.4).

        Args:
            from_date: Inclusive lower bound on a slot's ``start``, compared as
                text. An ISO date (``2026-09-10``) admits the whole day; a fuller
                ``2026-09-10T15:00`` starts partway through it. Both work because
                a slot's ``start`` begins with its date, so one comparison covers
                either form.
            limit: Return at most this many slots, earliest first. ``None`` (the
                default) returns every match.

        Why ``limit`` is on the interface rather than left to the caller: a
        clinic with a year of 30-minute slots published has thousands open, and a
        caller only ever hears the next few. Fetching them all to show three took
        a measured 6 seconds against DynamoDB, which is longer than the model
        waits before it starts speaking — so it answered "no availability" from
        its own guess. Trimming after the fact cannot fix that; the bound has to
        reach the query.

        Implementations must apply ``limit`` *after* filtering to ``provider_id``,
        so a shared service partition holding another provider's slots can never
        return an empty page while this provider has open time.
        """
        raise NotImplementedError

    @abstractmethod
    def list_open_slots_for_provider(
        self,
        provider_id: str,
        from_bound: str,
        *,
        limit: int | None = None,
    ) -> StoreResult[list[Slot]]:
        """Open slots on a provider's calendar from ``from_bound`` onward.

        Service-agnostic, because that is what a published slot actually is: the
        doctor's time. A single-doctor ENT clinic takes whichever ENT service the
        caller needs in whatever half hour is free — the service is decided at
        booking and recorded on the appointment, not fixed when the day is opened.
        The slot's own ``service`` label is only whatever the last publish of that
        day used, and cannot be otherwise: a slot's identity is provider + day +
        start with no service in it, so publishing a day under a second service
        overwrites the same slots rather than adding parallel ones.

        Prefer this over :meth:`list_open_slots` when answering "what can I offer
        this caller". The service-scoped version reads a shared GSI partition once
        per service name, which meant three round trips to answer one question and
        made a caller's request for one service report a wide-open day as fully
        booked.

        Args:
            from_bound: Inclusive lower bound on a slot's ``start``, compared as
                text. A date (``2026-09-10``) admits the whole day; a fuller
                ``2026-09-10T15:00`` starts partway through it.
            limit: Return at most this many, earliest first, applied after
                filtering so a run of booked slots cannot return an empty page
                while the provider has open time.

        Returns:
            ``Ok`` with open slots ordered by start then id.
        """
        raise NotImplementedError

    @abstractmethod
    def open_slot_span(
        self, provider_id: str, from_date: ISODate
    ) -> StoreResult[OpenSlotSpan | None]:
        """How far this provider's open calendar runs from ``from_date`` onward.

        Returns ``Ok(None)`` when the provider has no open slots at all from that
        bound — a genuinely unpublished calendar.

        Deliberately not service-scoped, and deliberately not a list. A published
        slot is the provider's time rather than a service-specific offer, and the
        caller of this method wants one cheap fact: is there bookable time around
        the date a patient just named. Implementations must answer it without
        reading every slot, because it is on the path that runs before the agent
        greets the caller.
        """
        raise NotImplementedError

    @abstractmethod
    def set_slot_status(self, slot_id: str, status: SlotStatus) -> StoreResult[Slot]:
        """Set a slot's lifecycle status, returning the updated slot.

        Rejects the change for a slot missing a ``provider_id`` (Req 16.7).
        """
        raise NotImplementedError

    @abstractmethod
    def remove_slots(self, slots: Sequence[Slot]) -> StoreResult[list[str]]:
        """Delete slots from the calendar entirely, returning the ids removed.

        For time that should not be on the calendar at all. Blocking takes a slot
        out of what the agent may offer but leaves it visible to the doctor, which
        is right for a lunch hour and wrong for hours the clinic never opens — a day
        listing 26 struck-through overnight rows is noise the doctor reads past
        every morning.

        **A booked slot is never deleted, and its presence fails the whole batch.**
        Removing it would strand a patient holding an appointment on time that no
        longer exists, with nothing left to say who they were. Freeing that time
        means cancelling their appointment first, deliberately and separately.

        Rejects the batch if any slot is missing a ``provider_id`` (Req 16.7).
        Deleting an id that is already absent is not an error, so this is safe to
        re-run. Emits one ``REMOVED`` slot event per deletion.
        """
        raise NotImplementedError

    @abstractmethod
    def set_slot_statuses(
        self, slots: Sequence[Slot], status: SlotStatus
    ) -> StoreResult[list[Slot]]:
        """Set the status of many already-read slots in one go.

        Takes whole :class:`~clinic_front_desk.models.Slot` records rather than
        ids, because the caller that needs this has just read a day's calendar and
        already holds them — and because a slot's storage key is derivable from the
        record, while an id alone is not.

        That difference is the point. :meth:`set_slot_status` resolves an id by
        scanning, which is fine for one slot and unusable in bulk: closing the
        overnight hours across a published quarter is around 2,500 slots, so 2,500
        table scans. This turns the same work into one batched write per day.

        Rejects the whole batch if any slot is missing a ``provider_id`` (Req 16.7)
        and writes nothing. Slots already at ``status`` are still written; callers
        that care about churn should filter first.

        Returns the updated slots, and emits one ``UPDATED`` slot event each.
        """
        raise NotImplementedError


__all__ = ["AppointmentStore"]
