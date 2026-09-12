"""Single-write persistence success through each interface (task 4.3, Req 16.2).

For every one of the seven Data_Layer store interfaces, a single write returns a
success ``Result`` and the written record is subsequently readable. Runs against
the in-memory fakes, which conform to the same interface contract as the
DynamoDB implementations (storage-swap equivalence is Property 27 / task 4.2).
"""

from __future__ import annotations

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
    Escalation,
    EscalationReason,
    Patient,
    Provider,
    Slot,
    SlotStatus,
    WaitlistEntry,
    is_ok,
)


def test_appointment_store_single_write_persists() -> None:
    store = MemoryAppointmentStore()
    appt = Appointment(id="a1", provider_id="prov1", patient_id="p1", service="ent",
                       slot_id="s1", date="2025-06-01", time="09:00")
    result = store.create(appt)
    assert is_ok(result)
    assert result.value.id == "a1"
    assert store.get("a1").unwrap() == appt


def test_appointment_store_single_slot_status_write_persists() -> None:
    store = MemoryAppointmentStore()
    store.seed_slot(Slot(id="s1", provider_id="prov1", service="ent",
                         start="2025-06-01T09:00:00Z", end="2025-06-01T09:30:00Z"))
    result = store.set_slot_status("s1", SlotStatus.BOOKED)
    assert is_ok(result)
    assert store.get_slot("s1").unwrap().status is SlotStatus.BOOKED


def test_patient_store_single_write_persists() -> None:
    store = MemoryPatientStore()
    patient = Patient(id="p1", name="Alice", callback_phone="555-1")
    result = store.create(patient)
    assert is_ok(result)
    assert store.get("p1").unwrap() == patient


def test_waitlist_store_single_write_persists() -> None:
    store = MemoryWaitlistStore()
    entry = WaitlistEntry(id="w1", patient_id="p1", service="ent",
                          preferred_slot_type="morning", added_at="2025-06-01T00:00:00Z", seq=0)
    result = store.add(entry)
    assert is_ok(result)
    assert store.list_by_service_ordered("ent").unwrap() == [result.value]


def test_decision_store_single_write_persists() -> None:
    store = MemoryDecisionStore()
    decision = Decision(id="d1", kind=DecisionKind.GAP_FILL, finding_key="k1",
                        summary="s", recommended_action="a", supporting_record_count=6,
                        generated_at="2025-06-01T00:00:00Z")
    result = store.create(decision)
    assert is_ok(result)
    assert store.list_open().unwrap() == [decision]


def test_clinic_knowledge_base_store_single_write_persists() -> None:
    store = MemoryClinicKnowledgeBaseStore()
    kb = ClinicKnowledgeBase(location="123 Main St",
                             providers=[Provider(id="prov1", name="Dr Who", specialty="ENT")],
                             configured=True)
    result = store.save(kb)
    assert is_ok(result)
    assert store.get().unwrap() == kb


def test_call_session_store_single_write_persists() -> None:
    from clinic_front_desk.models import CallSession

    store = MemoryCallSessionStore()
    session = CallSession(id="c1", started_at="2025-06-01T09:00:00Z")
    result = store.create(session)
    assert is_ok(result)
    assert store.list_recent(10).unwrap() == [session]


def test_escalation_store_single_write_persists() -> None:
    store = MemoryEscalationStore()
    esc = Escalation(id="e1", reason=EscalationReason.CLINICAL_CONTENT,
                     call_session_id="c1", context="ctx", created_at="2025-06-01T09:00:00Z")
    result = store.create(esc)
    assert is_ok(result)
    assert store.list_recent(10).unwrap() == [esc]
