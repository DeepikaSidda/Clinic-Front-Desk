"""Focused sanity checks for the in-memory fake stores (task 3.2).

These are lightweight examples that confirm the store contract holds; the
exhaustive property tests live in tasks 3.4–3.10.
"""

from __future__ import annotations

from clinic_front_desk.data_layer.events import ChangeEntity, ChangeEvent, ChangeKind
from clinic_front_desk.data_layer.memory import (
    MemoryAppointmentStore,
    MemoryCallSessionStore,
    MemoryClinicKnowledgeBaseStore,
    MemoryDecisionStore,
    MemoryEscalationStore,
    MemoryPatientStore,
    MemoryWaitlistStore,
)
from clinic_front_desk.models import (
    Appointment,
    ClinicKnowledgeBase,
    Decision,
    DecisionKind,
    DecisionStatus,
    Patient,
    Provider,
    Slot,
    SlotStatus,
    WaitlistEntry,
    is_err,
    is_ok,
)


class RecordingEmitter:
    """A ``ChangeEmitter`` that records every event for assertions."""

    def __init__(self) -> None:
        self.events: list[ChangeEvent] = []

    def emit(self, event: ChangeEvent) -> None:
        self.events.append(event)


# -- empty initialization (Req 16.4) ---------------------------------------


def test_stores_start_empty() -> None:
    assert is_ok(MemoryPatientStore().get("nope"))
    assert MemoryPatientStore().get("nope").unwrap() is None
    assert MemoryAppointmentStore().list_by_patient("p1").unwrap() == []
    assert MemoryWaitlistStore().list_by_service_ordered("ent").unwrap() == []
    assert MemoryDecisionStore().list_open().unwrap() == []
    assert MemoryClinicKnowledgeBaseStore().get().unwrap() is None
    assert MemoryCallSessionStore().list_recent(10).unwrap() == []
    assert MemoryEscalationStore().list_recent(10).unwrap() == []


# -- provider-id enforcement (Req 16.3, 16.7) ------------------------------


def test_appointment_create_rejects_blank_provider_id() -> None:
    store = MemoryAppointmentStore()
    # Build a valid appointment, then blank its provider_id post-construction to
    # simulate an input that bypassed dataclass validation.
    appt = Appointment(
        id="a1", provider_id="prov1", patient_id="p1", service="ent",
        slot_id="s1", date="2025-06-01", time="09:00",
    )
    object.__setattr__(appt, "provider_id", "")
    result = store.create(appt)
    assert is_err(result)
    assert result.error.field == "provider_id"
    # Non-destructive: nothing was stored.
    assert store.get("a1").unwrap() is None


def test_config_save_rejects_provider_without_id() -> None:
    store = MemoryClinicKnowledgeBaseStore()
    kb = ClinicKnowledgeBase(
        location="Main St",
        providers=[Provider(id="", name="Dr", specialty="ENT")],
    )
    result = store.save(kb)
    assert is_err(result)
    # Prior (empty) state retained.
    assert store.get().unwrap() is None


# -- atomicity / non-destruction (Req 16.6) --------------------------------


def test_failed_move_leaves_state_unchanged() -> None:
    store = MemoryAppointmentStore()
    store.seed_slot(Slot(id="s1", provider_id="prov1", service="ent",
                         start="2025-06-01T09:00:00Z", end="2025-06-01T09:30:00Z",
                         status=SlotStatus.BOOKED))
    appt = store.create(Appointment(
        id="a1", provider_id="prov1", patient_id="p1", service="ent",
        slot_id="s1", date="2025-06-01", time="09:00",
    )).unwrap()
    # Move to a non-existent slot -> failure, appointment stays on original slot.
    result = store.move("a1", "does-not-exist")
    assert is_err(result)
    assert store.get("a1").unwrap().slot_id == "s1"
    assert store.get_slot("s1").unwrap().status == SlotStatus.BOOKED


def test_booking_slot_lifecycle_round_trip() -> None:
    store = MemoryAppointmentStore()
    store.seed_slots([
        Slot(id="s1", provider_id="prov1", service="ent",
             start="2025-06-01T09:00:00Z", end="2025-06-01T09:30:00Z"),
        Slot(id="s2", provider_id="prov1", service="ent",
             start="2025-06-02T09:00:00Z", end="2025-06-02T09:30:00Z"),
    ])
    store.create(Appointment(
        id="a1", provider_id="prov1", patient_id="p1", service="ent",
        slot_id="s1", date="2025-06-01", time="09:00",
    ))
    store.set_slot_status("s1", SlotStatus.BOOKED)
    # Reschedule to s2: s2 booked, s1 released.
    store.move("a1", "s2")
    assert store.get_slot("s1").unwrap().status == SlotStatus.OPEN
    assert store.get_slot("s2").unwrap().status == SlotStatus.BOOKED
    assert store.get("a1").unwrap().slot_id == "s2"
    # Cancel: appointment gone, slot released.
    released = store.remove("a1").unwrap()
    assert released.released_slot_id == "s2"
    assert store.get("a1").unwrap() is None
    assert store.get_slot("s2").unwrap().status == SlotStatus.OPEN


def test_returned_entities_are_copies() -> None:
    store = MemoryPatientStore()
    created = store.create(Patient(id="p1", name="A", callback_phone="123")).unwrap()
    created.name = "MUTATED"
    assert store.get("p1").unwrap().name == "A"


# -- waitlist ordering (Req 7.3) -------------------------------------------


def test_waitlist_orders_by_added_at_then_seq() -> None:
    store = MemoryWaitlistStore()
    # Same added_at for b and c; insertion order must be preserved via seq.
    store.add(WaitlistEntry(id="a", patient_id="pa", service="ent",
                            preferred_slot_type="any", added_at="2025-06-02T00:00:00Z", seq=99))
    store.add(WaitlistEntry(id="b", patient_id="pb", service="ent",
                            preferred_slot_type="any", added_at="2025-06-01T00:00:00Z", seq=99))
    store.add(WaitlistEntry(id="c", patient_id="pc", service="ent",
                            preferred_slot_type="any", added_at="2025-06-01T00:00:00Z", seq=99))
    ordered = [e.id for e in store.list_by_service_ordered("ent").unwrap()]
    assert ordered == ["b", "c", "a"]


def test_waitlist_find_active_and_remove() -> None:
    store = MemoryWaitlistStore()
    store.add(WaitlistEntry(id="w1", patient_id="p1", service="ent",
                            preferred_slot_type="morning", added_at="2025-06-01T00:00:00Z", seq=0))
    assert store.find_active("p1", "ent", "morning").unwrap() is not None
    assert store.find_active("p1", "ent", "evening").unwrap() is None
    assert is_ok(store.remove("w1"))
    assert store.list_by_service_ordered("ent").unwrap() == []
    assert is_err(store.remove("w1"))


# -- open decisions newest-first (Req 14.1) --------------------------------


def _decision(id: str, generated_at: str, status: DecisionStatus = DecisionStatus.OPEN) -> Decision:
    return Decision(
        id=id, kind=DecisionKind.GAP_FILL, finding_key=f"k-{id}",
        summary="s", recommended_action="a", generated_at=generated_at, status=status,
    )


def test_open_decisions_newest_first_and_dedupe() -> None:
    store = MemoryDecisionStore()
    store.create(_decision("d1", "2025-06-01T00:00:00Z"))
    store.create(_decision("d2", "2025-06-03T00:00:00Z"))
    store.create(_decision("d3", "2025-06-02T00:00:00Z"))
    assert [d.id for d in store.list_open().unwrap()] == ["d2", "d3", "d1"]
    # Resolved decisions drop out of the open feed.
    store.set_status("d2", DecisionStatus.APPROVED, "2025-06-04T00:00:00Z")
    assert [d.id for d in store.list_open().unwrap()] == ["d3", "d1"]
    assert store.find_open_by_finding_key("k-d3").unwrap() is not None
    assert store.find_open_by_finding_key("k-d2").unwrap() is None


# -- change emission on success only (Req 16.6) ----------------------------


def test_emits_only_on_successful_write() -> None:
    emitter = RecordingEmitter()
    store = MemoryPatientStore(emitter)
    store.create(Patient(id="p1", name="A", callback_phone="123"))
    assert emitter.events == [
        ChangeEvent(entity=ChangeEntity.PATIENT, id="p1", kind=ChangeKind.CREATED)
    ]


def test_no_emit_on_failed_write() -> None:
    emitter = RecordingEmitter()
    store = MemoryAppointmentStore(emitter)
    appt = Appointment(
        id="a1", provider_id="prov1", patient_id="p1", service="ent",
        slot_id="s1", date="2025-06-01", time="09:00",
    )
    object.__setattr__(appt, "provider_id", "")
    store.create(appt)
    assert emitter.events == []
