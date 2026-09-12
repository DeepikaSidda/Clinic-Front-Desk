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


def test_book_slot_status_failure_rolls_back_appointment() -> None:
    store = _store_with_slots()
    guarded = wrap(store, fail_on("set_slot_status"))
    result = book_appointment(
        guarded, provider_id="prov1", patient_id="p1", slot_id="s1", service="ent",
        appointment_id="a1",
    )
    assert is_err(result)
    assert result.error.kind == "store_failure"
    # Compensating rollback removed the appointment; slot never booked (Req 2.8).
    assert store.get("a1").unwrap() is None
    assert store.get_slot("s1").unwrap().status == SlotStatus.OPEN


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
