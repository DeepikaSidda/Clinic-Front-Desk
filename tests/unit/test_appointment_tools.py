"""Unit tests for the appointment lifecycle tools (task 6.4).

Covers ``book_appointment``, ``reschedule``, and ``cancel`` over the in-memory
:class:`MemoryAppointmentStore`, including the slot-lifecycle transitions and
the failure-path guarantees (no partial appointment / unchanged state) using the
fault-injection wrapper. The exhaustive round-trip property test lives in
task 6.5.
"""

from __future__ import annotations

from clinic_front_desk.data_layer.faults import fail_on, wrap
from clinic_front_desk.data_layer.memory import MemoryAppointmentStore
from clinic_front_desk.models import (
    Appointment,
    Slot,
    SlotStatus,
    is_err,
    is_ok,
)
from clinic_front_desk.tools.appointments import (
    BookingResult,
    CancelResult,
    RescheduleResult,
    book_appointment,
    cancel,
    reschedule,
)


def _store_with_slots() -> MemoryAppointmentStore:
    store = MemoryAppointmentStore()
    store.seed_slots(
        [
            Slot(
                id="s1", provider_id="prov1", service="ent",
                start="2025-06-01T09:00:00Z", end="2025-06-01T09:30:00Z",
            ),
            Slot(
                id="s2", provider_id="prov1", service="ent",
                start="2025-06-02T10:00:00Z", end="2025-06-02T10:30:00Z",
            ),
        ]
    )
    return store


def _book(store: MemoryAppointmentStore, slot_id: str = "s1", appt_id: str = "a1") -> BookingResult:
    result = book_appointment(
        store,
        provider_id="prov1",
        patient_id="p1",
        slot_id=slot_id,
        service="ent",
        appointment_id=appt_id,
        now="2025-05-01T00:00:00Z",
    )
    assert is_ok(result)
    return result.value


# -- book_appointment -------------------------------------------------------


def test_book_creates_appointment_and_books_slot() -> None:
    store = _store_with_slots()
    booking = _book(store)

    assert isinstance(booking, BookingResult)
    appt = booking.appointment
    assert appt.id == "a1"
    assert appt.provider_id == "prov1"
    assert appt.patient_id == "p1"
    assert appt.service == "ent"
    assert appt.slot_id == "s1"
    # Date/time derived from the slot start.
    assert appt.date == "2025-06-01"
    assert appt.time == "09:00"
    # Persisted and slot flipped to booked.
    assert store.get("a1").unwrap() is not None
    assert store.get_slot("s1").unwrap().status == SlotStatus.BOOKED


def test_book_rejects_missing_provider_id() -> None:
    store = _store_with_slots()
    result = book_appointment(
        store, provider_id="", patient_id="p1", slot_id="s1", service="ent"
    )
    assert is_err(result)
    assert result.error.kind == "validation"
    assert result.error.field == "provider_id"


def test_book_rejects_missing_patient_and_service() -> None:
    store = _store_with_slots()
    assert is_err(
        book_appointment(store, provider_id="prov1", patient_id="", slot_id="s1", service="ent")
    )
    assert is_err(
        book_appointment(store, provider_id="prov1", patient_id="p1", slot_id="s1", service="")
    )


def test_book_unknown_slot_is_not_found() -> None:
    store = _store_with_slots()
    result = book_appointment(
        store, provider_id="prov1", patient_id="p1", slot_id="nope", service="ent"
    )
    assert is_err(result)
    assert result.error.kind == "not_found"


def test_book_create_failure_leaves_no_partial_appointment() -> None:
    store = _store_with_slots()
    guarded = wrap(store, fail_on("create"))
    result = book_appointment(
        guarded, provider_id="prov1", patient_id="p1", slot_id="s1", service="ent",
        appointment_id="a1",
    )
    assert is_err(result)
    assert result.error.kind == "store_failure"
    # No appointment persisted and the slot is still open (Req 2.8).
    assert store.get("a1").unwrap() is None
    assert store.get_slot("s1").unwrap().status == SlotStatus.OPEN


def test_book_slot_claim_failure_writes_no_appointment() -> None:
    """Req 2.8, with the claim moved ahead of the appointment write.

    Booking now claims the slot first, so a failed claim means there was never an
    appointment to roll back. Faults on ``set_slot_status`` no longer exercise this
    path — the booking does not call it.
    """
    store = _store_with_slots()
    guarded = wrap(store, fail_on("claim_slot"))
    result = book_appointment(
        guarded, provider_id="prov1", patient_id="p1", slot_id="s1", service="ent",
        appointment_id="a1",
    )
    assert is_err(result)
    assert result.error.kind == "store_failure"
    assert store.get("a1").unwrap() is None
    assert store.get_slot("s1").unwrap().status == SlotStatus.OPEN


def test_booking_a_slot_twice_is_refused() -> None:
    """The bug: the same half hour could be sold to two patients.

    ``book_appointment`` read the slot, wrote the appointment, then stamped the slot
    booked without ever checking it was open. The second booking succeeded, and both
    patients held an appointment on the same time with nothing in the data saying so.
    """
    store = _store_with_slots()
    first = book_appointment(
        store, provider_id="prov1", patient_id="p1", slot_id="s1", service="ent",
        appointment_id="a1",
    )
    assert not is_err(first)

    second = book_appointment(
        store, provider_id="prov1", patient_id="p2", slot_id="s1", service="ent",
        appointment_id="a2",
    )

    assert is_err(second), "the slot was already taken"
    assert "not open" in second.error.detail.lower()
    # And the second patient got no appointment at all, rather than a phantom one.
    assert store.get("a2").unwrap() is None
    # The first patient still holds the slot.
    assert store.get("a1").unwrap().patient_id == "p1"
    assert store.get_slot("s1").unwrap().status == SlotStatus.BOOKED


def test_a_blocked_slot_cannot_be_booked() -> None:
    """The doctor's lunch hour is not available because she blocked it."""
    store = _store_with_slots()
    store.set_slot_status("s1", SlotStatus.BLOCKED)

    result = book_appointment(
        store, provider_id="prov1", patient_id="p1", slot_id="s1", service="ent",
        appointment_id="a1",
    )

    assert is_err(result)
    assert store.get("a1").unwrap() is None
    assert store.get_slot("s1").unwrap().status == SlotStatus.BLOCKED


def test_rescheduling_onto_a_booked_slot_is_refused() -> None:
    """The same gap existed on the move path, so it is held down too."""
    store = _store_with_slots()
    _book(store, slot_id="s1", appt_id="a1")
    _book(store, slot_id="s2", appt_id="a2")

    moved = reschedule(store, appointment_id="a1", new_slot_id="s2")

    assert is_err(moved), "s2 is held by a2"
    # a2 keeps its slot and a1 keeps its own.
    assert store.get("a2").unwrap().slot_id == "s2"
    assert store.get("a1").unwrap().slot_id == "s1"
    assert store.get_slot("s1").unwrap().status == SlotStatus.BOOKED


# -- reschedule -------------------------------------------------------------


def test_reschedule_moves_appointment_and_releases_old_slot() -> None:
    store = _store_with_slots()
    _book(store, slot_id="s1", appt_id="a1")

    result = reschedule(store, appointment_id="a1", new_slot_id="s2")
    assert is_ok(result)
    payload = result.value
    assert isinstance(payload, RescheduleResult)
    assert payload.released_slot_id == "s1"
    assert payload.appointment.slot_id == "s2"
    # New slot booked, previous slot released (Req 4.7, 4.8).
    assert store.get_slot("s2").unwrap().status == SlotStatus.BOOKED
    assert store.get_slot("s1").unwrap().status == SlotStatus.OPEN


def test_reschedule_unknown_appointment_is_not_found() -> None:
    store = _store_with_slots()
    result = reschedule(store, appointment_id="ghost", new_slot_id="s2")
    assert is_err(result)
    assert result.error.kind == "not_found"


def test_reschedule_failure_leaves_both_slots_and_appointment_unchanged() -> None:
    store = _store_with_slots()
    _book(store, slot_id="s1", appt_id="a1")
    guarded = wrap(store, fail_on("move"))

    result = reschedule(guarded, appointment_id="a1", new_slot_id="s2")
    assert is_err(result)
    assert result.error.kind == "store_failure"
    # Nothing moved (Req 4.9).
    assert store.get("a1").unwrap().slot_id == "s1"
    assert store.get_slot("s1").unwrap().status == SlotStatus.BOOKED
    assert store.get_slot("s2").unwrap().status == SlotStatus.OPEN


# -- cancel -----------------------------------------------------------------


def test_cancel_removes_appointment_and_releases_slot() -> None:
    store = _store_with_slots()
    _book(store, slot_id="s1", appt_id="a1")

    result = cancel(store, appointment_id="a1")
    assert is_ok(result)
    assert isinstance(result.value, CancelResult)
    assert result.value.released_slot_id == "s1"
    # Appointment gone, slot released (Req 5.5, 5.7).
    assert store.get("a1").unwrap() is None
    assert store.get_slot("s1").unwrap().status == SlotStatus.OPEN


def test_cancel_unknown_appointment_is_not_found() -> None:
    store = _store_with_slots()
    result = cancel(store, appointment_id="ghost")
    assert is_err(result)
    assert result.error.kind == "not_found"


def test_cancel_failure_retains_appointment_unchanged() -> None:
    store = _store_with_slots()
    _book(store, slot_id="s1", appt_id="a1")
    guarded = wrap(store, fail_on("remove"))

    result = cancel(guarded, appointment_id="a1")
    assert is_err(result)
    assert result.error.kind == "store_failure"
    # Appointment retained, slot still booked (Req 5.8).
    assert store.get("a1").unwrap() is not None
    assert store.get_slot("s1").unwrap().status == SlotStatus.BOOKED
