"""Unit tests for the waitlist Strands tools (task 6.10).

Covers ``add_to_waitlist`` and ``fill_gap_from_waitlist``:

- add: success round-trip + confirmation fields (Req 7.1, 7.2), active-duplicate
  suppression (Req 7.5), and store-failure with no partial entry (Req 7.4).
- fill: earliest-selection + booking + entry removal (Req 8.2, 8.3, 8.4),
  no-match no-action (Req 8.6), and compensated failure paths that leave the
  slot open and the entry unchanged (Req 8.5).

These are focused example/edge tests; the exhaustive property tests live in
tasks 6.11 and 10.6.
"""

from __future__ import annotations

from clinic_front_desk.data_layer.faults import fail_on, wrap
from clinic_front_desk.data_layer.memory import (
    MemoryAppointmentStore,
    MemoryWaitlistStore,
)
from clinic_front_desk.models import (
    Duplicate,
    NotFound,
    Slot,
    SlotStatus,
    StoreFailure,
    WaitlistEntry,
    is_err,
    is_ok,
)
from clinic_front_desk.tools.waitlist import (
    GapFillResult,
    add_to_waitlist,
    fill_gap_from_waitlist,
)


def _seed_entry(
    store: MemoryWaitlistStore,
    *,
    entry_id: str,
    patient_id: str,
    service: str,
    slot_type: str,
    added_at: str,
) -> WaitlistEntry:
    """Directly add an active waitlist entry to the store for setup."""
    result = store.add(
        WaitlistEntry(
            id=entry_id,
            patient_id=patient_id,
            service=service,
            preferred_slot_type=slot_type,
            added_at=added_at,
            seq=0,
            active=True,
        )
    )
    assert is_ok(result)
    return result.value


def _open_slot(
    store: MemoryAppointmentStore,
    *,
    slot_id: str,
    provider_id: str,
    service: str,
    start: str,
) -> Slot:
    slot = Slot(
        id=slot_id,
        provider_id=provider_id,
        service=service,
        start=start,
        end=start,
        status=SlotStatus.OPEN,
    )
    store.seed_slot(slot)
    return slot


# -- add_to_waitlist -------------------------------------------------------


def test_add_to_waitlist_records_entry_with_confirmation_fields() -> None:
    """Req 7.1, 7.2: a successful add persists the entry and returns the
    requested service and preferred slot type for confirmation."""
    store = MemoryWaitlistStore()

    result = add_to_waitlist(
        store,
        patient_id="p1",
        service="hearing_test",
        preferred_slot_type="morning",
        entry_id="w1",
        added_at="2025-06-01T09:00:00Z",
    )

    assert is_ok(result)
    entry = result.value
    assert entry.id == "w1"
    assert entry.patient_id == "p1"
    assert entry.service == "hearing_test"  # confirmation field (Req 7.2)
    assert entry.preferred_slot_type == "morning"  # confirmation field (Req 7.2)
    assert entry.active is True
    # Persisted and retrievable through the store.
    listed = store.list_by_service_ordered("hearing_test").unwrap()
    assert [e.id for e in listed] == ["w1"]


def test_add_to_waitlist_generates_id_and_timestamp_when_absent() -> None:
    store = MemoryWaitlistStore()

    result = add_to_waitlist(
        store,
        patient_id="p1",
        service="ent",
        preferred_slot_type="any",
        clock=lambda: "2025-06-02T10:00:00Z",
        id_gen=lambda: "generated-id",
    )

    assert is_ok(result)
    assert result.value.id == "generated-id"
    assert result.value.added_at == "2025-06-02T10:00:00Z"


def test_add_to_waitlist_suppresses_active_duplicate() -> None:
    """Req 7.5: a second add for the same patient/service/slot type is declined
    with a Duplicate error and creates no second entry."""
    store = MemoryWaitlistStore()
    _seed_entry(
        store,
        entry_id="w1",
        patient_id="p1",
        service="ent",
        slot_type="morning",
        added_at="2025-06-01T09:00:00Z",
    )

    result = add_to_waitlist(
        store,
        patient_id="p1",
        service="ent",
        preferred_slot_type="morning",
        entry_id="w2",
    )

    assert is_err(result)
    assert isinstance(result.error, Duplicate)
    assert result.error.entry_id == "w1"
    # No duplicate created: still exactly one entry.
    listed = store.list_by_service_ordered("ent").unwrap()
    assert [e.id for e in listed] == ["w1"]


def test_add_to_waitlist_different_slot_type_is_not_a_duplicate() -> None:
    """A different preferred slot type is a distinct placement, not a duplicate."""
    store = MemoryWaitlistStore()
    _seed_entry(
        store,
        entry_id="w1",
        patient_id="p1",
        service="ent",
        slot_type="morning",
        added_at="2025-06-01T09:00:00Z",
    )

    result = add_to_waitlist(
        store,
        patient_id="p1",
        service="ent",
        preferred_slot_type="afternoon",
        entry_id="w2",
    )

    assert is_ok(result)
    listed = store.list_by_service_ordered("ent").unwrap()
    assert {e.id for e in listed} == {"w1", "w2"}


def test_add_to_waitlist_store_failure_leaves_no_partial_entry() -> None:
    """Req 7.4: when the add write fails, no partial entry is retained."""
    base = MemoryWaitlistStore()
    store = wrap(base, fail_on("add"))

    result = add_to_waitlist(
        store,
        patient_id="p1",
        service="ent",
        preferred_slot_type="morning",
        entry_id="w1",
    )

    assert is_err(result)
    assert isinstance(result.error, StoreFailure)
    # Nothing persisted.
    assert base.list_by_service_ordered("ent").unwrap() == []


def test_add_to_waitlist_lookup_failure_is_store_failure() -> None:
    base = MemoryWaitlistStore()
    store = wrap(base, fail_on("find_active"))

    result = add_to_waitlist(
        store,
        patient_id="p1",
        service="ent",
        preferred_slot_type="morning",
    )

    assert is_err(result)
    assert isinstance(result.error, StoreFailure)


# -- fill_gap_from_waitlist ------------------------------------------------


def test_fill_gap_selects_earliest_books_and_removes_entry() -> None:
    """Req 8.2, 8.3, 8.4: the earliest matching patient is booked into the slot
    and their waitlist entry is removed."""
    wl = MemoryWaitlistStore()
    appts = MemoryAppointmentStore()
    _open_slot(appts, slot_id="s1", provider_id="prov1", service="ent", start="2025-06-10T14:30:00Z")
    # Two matching entries; w_early was added first, so it must be selected.
    _seed_entry(wl, entry_id="w_early", patient_id="p_early", service="ent",
                slot_type="any", added_at="2025-06-01T09:00:00Z")
    _seed_entry(wl, entry_id="w_late", patient_id="p_late", service="ent",
                slot_type="any", added_at="2025-06-02T09:00:00Z")

    result = fill_gap_from_waitlist(
        wl, appts, slot_id="s1", appointment_id="a1", clock=lambda: "2025-06-05T00:00:00Z"
    )

    assert is_ok(result)
    filled: GapFillResult = result.value
    # Earliest selected (Req 8.2).
    assert filled.removed_waitlist_entry_id == "w_early"
    # Booked into the slot with the slot's provider/service/date/time (Req 8.3).
    appt = filled.appointment
    assert appt.patient_id == "p_early"
    assert appt.provider_id == "prov1"
    assert appt.service == "ent"
    assert appt.slot_id == "s1"
    assert appt.date == "2025-06-10"
    assert appt.time == "14:30"
    assert appts.get("a1").unwrap() is not None
    # Slot now booked.
    assert appts.get_slot("s1").unwrap().status == SlotStatus.BOOKED
    # Entry removed (Req 8.4); the later entry remains.
    remaining = wl.list_by_service_ordered("ent").unwrap()
    assert [e.id for e in remaining] == ["w_late"]


def test_fill_gap_no_matching_patient_takes_no_action() -> None:
    """Req 8.6: with no waitlisted patient for the slot's service, the slot is
    left open and no fill occurs."""
    wl = MemoryWaitlistStore()
    appts = MemoryAppointmentStore()
    _open_slot(appts, slot_id="s1", provider_id="prov1", service="ent", start="2025-06-10T14:30:00Z")
    # A waitlist entry for a different service must not match.
    _seed_entry(wl, entry_id="w1", patient_id="p1", service="allergy",
                slot_type="any", added_at="2025-06-01T09:00:00Z")

    result = fill_gap_from_waitlist(wl, appts, slot_id="s1")

    assert is_err(result)
    assert isinstance(result.error, NotFound)
    # Slot left open, entry untouched.
    assert appts.get_slot("s1").unwrap().status == SlotStatus.OPEN
    assert [e.id for e in wl.list_by_service_ordered("allergy").unwrap()] == ["w1"]


def test_fill_gap_missing_slot_is_not_found() -> None:
    wl = MemoryWaitlistStore()
    appts = MemoryAppointmentStore()

    result = fill_gap_from_waitlist(wl, appts, slot_id="ghost")

    assert is_err(result)
    assert isinstance(result.error, NotFound)


def test_fill_gap_booking_failure_leaves_slot_and_entry_unchanged() -> None:
    """Req 8.5: if booking fails, the slot stays open and the entry is retained."""
    wl = MemoryWaitlistStore()
    appts_base = MemoryAppointmentStore()
    _open_slot(appts_base, slot_id="s1", provider_id="prov1", service="ent",
               start="2025-06-10T14:30:00Z")
    _seed_entry(wl, entry_id="w1", patient_id="p1", service="ent",
                slot_type="any", added_at="2025-06-01T09:00:00Z")
    appts = wrap(appts_base, fail_on("create"))

    result = fill_gap_from_waitlist(wl, appts, slot_id="s1", appointment_id="a1")

    assert is_err(result)
    assert isinstance(result.error, StoreFailure)
    # Slot still open, no appointment, entry retained.
    assert appts_base.get_slot("s1").unwrap().status == SlotStatus.OPEN
    assert appts_base.get("a1").unwrap() is None
    assert [e.id for e in wl.list_by_service_ordered("ent").unwrap()] == ["w1"]


def test_fill_gap_slot_claim_failure_is_compensated() -> None:
    """Req 8.5: a failed slot claim leaves the slot open and the entry unchanged.

    The gap-fill claims the slot conditionally *before* writing the appointment, so
    ``claim_slot`` is the write that can fail here. It matters most on this path: the
    doctor approves a Decision detected minutes earlier, by which time a caller may
    already have taken the slot.
    """
    wl = MemoryWaitlistStore()
    appts_base = MemoryAppointmentStore()
    _open_slot(appts_base, slot_id="s1", provider_id="prov1", service="ent",
               start="2025-06-10T14:30:00Z")
    _seed_entry(wl, entry_id="w1", patient_id="p1", service="ent",
                slot_type="any", added_at="2025-06-01T09:00:00Z")
    appts = wrap(appts_base, fail_on("claim_slot"))

    result = fill_gap_from_waitlist(wl, appts, slot_id="s1", appointment_id="a1")

    assert is_err(result)
    assert isinstance(result.error, StoreFailure)
    # Compensated: appointment rolled back, slot open, entry retained.
    assert appts_base.get("a1").unwrap() is None
    assert appts_base.get_slot("s1").unwrap().status == SlotStatus.OPEN
    assert [e.id for e in wl.list_by_service_ordered("ent").unwrap()] == ["w1"]


def test_fill_gap_entry_removal_failure_is_compensated() -> None:
    """Req 8.5: if removing the waitlist entry fails, the appointment is rolled
    back so the slot is left open and the entry unchanged."""
    wl_base = MemoryWaitlistStore()
    appts = MemoryAppointmentStore()
    _open_slot(appts, slot_id="s1", provider_id="prov1", service="ent",
               start="2025-06-10T14:30:00Z")
    _seed_entry(wl_base, entry_id="w1", patient_id="p1", service="ent",
                slot_type="any", added_at="2025-06-01T09:00:00Z")
    wl = wrap(wl_base, fail_on("remove"))

    result = fill_gap_from_waitlist(wl, appts, slot_id="s1", appointment_id="a1")

    assert is_err(result)
    assert isinstance(result.error, StoreFailure)
    # Compensated: appointment rolled back, slot returned to open.
    assert appts.get("a1").unwrap() is None
    assert appts.get_slot("s1").unwrap().status == SlotStatus.OPEN
    # Entry retained (removal failed).
    assert [e.id for e in wl_base.list_by_service_ordered("ent").unwrap()] == ["w1"]
