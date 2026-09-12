"""Store-level sanity checks for the DynamoDB single-table stores (task 4.1).

These run against a moto-mocked DynamoDB (in-process, fast, deterministic) and
confirm the DynamoDB implementations honour the same observable contract as the
in-memory fakes: empty-init reads, round-trip writes, slot lifecycle, waitlist
ordering, open-decision ordering, the Decimal boundary, provider-id enforcement,
atomic config save, and change emission on success only. The exhaustive
storage-swap equivalence (Property 27) lives in task 4.2.
"""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws

from clinic_front_desk.data_layer.dynamodb import (
    DynamoStores,
    create_stores,
    create_table,
    from_dynamo,
    to_dynamo,
)
from clinic_front_desk.data_layer.events import ChangeEntity, ChangeEvent, ChangeKind
from clinic_front_desk.models import (
    Appointment,
    CallOutcome,
    CallSession,
    ClinicKnowledgeBase,
    Decision,
    DecisionKind,
    DecisionStatus,
    Escalation,
    EscalationReason,
    Patient,
    PatientRef,
    Provider,
    ServiceConfig,
    Slot,
    SlotStatus,
    WaitlistEntry,
    is_err,
    is_ok,
)

_TABLE_NAME = "clinic-front-desk-test"
_REGION = "us-east-1"


class RecordingEmitter:
    """A ``ChangeEmitter`` that records every event for assertions."""

    def __init__(self) -> None:
        self.events: list[ChangeEvent] = []

    def emit(self, event: ChangeEvent) -> None:
        self.events.append(event)


@pytest.fixture()
def emitter() -> RecordingEmitter:
    return RecordingEmitter()


@pytest.fixture()
def stores(emitter: RecordingEmitter) -> Iterator[DynamoStores]:
    """A fresh moto-backed table with all seven stores wired to one emitter."""
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        table = create_table(dynamodb, _TABLE_NAME)
        yield create_stores(table, emitter)


# -- Decimal boundary -------------------------------------------------------


def test_decimal_boundary_round_trips_numbers() -> None:
    plain = {"i": 5, "f": 150.75, "b": True, "n": None, "nested": [1, 2.5, {"x": 9.0}]}
    restored = from_dynamo(to_dynamo(plain))
    assert restored == {"i": 5, "f": 150.75, "b": True, "n": None, "nested": [1, 2.5, {"x": 9}]}
    # Booleans must survive as bool (not coerced through the int/Decimal path).
    assert restored["b"] is True


# -- empty initialization (Req 16.4) ---------------------------------------


def test_stores_start_empty(stores: DynamoStores) -> None:
    assert stores.patients.get("nope").unwrap() is None
    assert stores.appointments.get("nope").unwrap() is None
    assert stores.appointments.list_by_patient("p1").unwrap() == []
    assert stores.appointments.list_open_slots("prov1", "ent", "2025-01-01").unwrap() == []
    assert stores.waitlist.list_by_service_ordered("ent").unwrap() == []
    assert stores.decisions.list_open().unwrap() == []
    assert stores.clinic_knowledge_base.get().unwrap() is None
    assert stores.call_sessions.list_recent(10).unwrap() == []
    assert stores.escalations.list_recent(10).unwrap() == []


# -- patient round-trip + lookup -------------------------------------------


def test_patient_create_get_and_lookup(stores: DynamoStores, emitter: RecordingEmitter) -> None:
    stores.patients.create(
        Patient(id="p1", name="Alice", callback_phone="555-1", created_at="2025-01-01T00:00:00Z")
    )
    got = stores.patients.get("p1").unwrap()
    assert got is not None and got.name == "Alice"
    found = stores.patients.find_by_name_and_phone("Alice", "555-1").unwrap()
    assert [p.id for p in found] == ["p1"]
    assert stores.patients.find_by_name_and_phone("Alice", "wrong").unwrap() == []
    assert ChangeEvent(ChangeEntity.PATIENT, "p1", ChangeKind.CREATED) in emitter.events


# -- provider-id enforcement (Req 16.3, 16.7) ------------------------------


def test_appointment_create_rejects_blank_provider_id(stores: DynamoStores) -> None:
    appt = Appointment(
        id="a1", provider_id="prov1", patient_id="p1", service="ent",
        slot_id="s1", date="2025-06-01", time="09:00",
    )
    appt.provider_id = ""  # simulate an input that bypassed dataclass validation
    result = stores.appointments.create(appt)
    assert is_err(result)
    assert result.error.field == "provider_id"
    assert stores.appointments.get("a1").unwrap() is None


def test_config_save_rejects_provider_without_id(stores: DynamoStores) -> None:
    kb = ClinicKnowledgeBase(
        location="Main St", providers=[Provider(id="", name="Dr", specialty="ENT")]
    )
    assert is_err(stores.clinic_knowledge_base.save(kb))
    assert stores.clinic_knowledge_base.get().unwrap() is None


# -- slot lifecycle round-trip (Req 2.5, 4.7, 4.8, 5.5, 5.7) ---------------


def test_booking_slot_lifecycle_round_trip(stores: DynamoStores) -> None:
    ap = stores.appointments
    ap.seed_slots([
        Slot(id="s1", provider_id="prov1", service="ent",
             start="2025-06-01T09:00:00Z", end="2025-06-01T09:30:00Z"),
        Slot(id="s2", provider_id="prov1", service="ent",
             start="2025-06-02T09:00:00Z", end="2025-06-02T09:30:00Z"),
    ])
    # Both slots open initially.
    assert {s.id for s in ap.list_open_slots("prov1", "ent", "2025-06-01").unwrap()} == {"s1", "s2"}
    ap.create(Appointment(
        id="a1", provider_id="prov1", patient_id="p1", service="ent",
        slot_id="s1", date="2025-06-01", time="09:00",
    ))
    ap.set_slot_status("s1", SlotStatus.BOOKED)
    # s1 now booked -> leaves the open-slot lookup.
    assert {s.id for s in ap.list_open_slots("prov1", "ent", "2025-06-01").unwrap()} == {"s2"}
    # Reschedule to s2: s2 booked, s1 released.
    ap.move("a1", "s2")
    assert ap.get_slot("s1").unwrap().status == SlotStatus.OPEN
    assert ap.get_slot("s2").unwrap().status == SlotStatus.BOOKED
    assert ap.get("a1").unwrap().slot_id == "s2"
    # Cancel: appointment gone, slot released.
    released = ap.remove("a1").unwrap()
    assert released.released_slot_id == "s2"
    assert ap.get("a1").unwrap() is None
    assert ap.get_slot("s2").unwrap().status == SlotStatus.OPEN


def test_failed_move_leaves_state_unchanged(stores: DynamoStores) -> None:
    ap = stores.appointments
    ap.seed_slot(Slot(id="s1", provider_id="prov1", service="ent",
                      start="2025-06-01T09:00:00Z", end="2025-06-01T09:30:00Z",
                      status=SlotStatus.BOOKED))
    ap.create(Appointment(
        id="a1", provider_id="prov1", patient_id="p1", service="ent",
        slot_id="s1", date="2025-06-01", time="09:00",
    ))
    assert is_err(ap.move("a1", "does-not-exist"))
    assert ap.get("a1").unwrap().slot_id == "s1"
    assert ap.get_slot("s1").unwrap().status == SlotStatus.BOOKED


def test_list_by_provider_and_day(stores: DynamoStores) -> None:
    ap = stores.appointments
    ap.create(Appointment(id="a2", provider_id="prov1", patient_id="p1", service="ent",
                          slot_id="s2", date="2025-06-01", time="11:00"))
    ap.create(Appointment(id="a1", provider_id="prov1", patient_id="p2", service="ent",
                          slot_id="s1", date="2025-06-01", time="09:00"))
    ap.create(Appointment(id="a3", provider_id="prov1", patient_id="p3", service="ent",
                          slot_id="s3", date="2025-06-02", time="09:00"))
    same_day = ap.list_by_provider_and_day("prov1", "2025-06-01").unwrap()
    assert [a.id for a in same_day] == ["a1", "a2"]  # sorted by time


# -- waitlist ordering (Req 7.3) -------------------------------------------


def test_waitlist_orders_by_added_at_then_seq(stores: DynamoStores) -> None:
    wl = stores.waitlist
    wl.add(WaitlistEntry(id="a", patient_id="pa", service="ent",
                         preferred_slot_type="any", added_at="2025-06-02T00:00:00Z", seq=99))
    wl.add(WaitlistEntry(id="b", patient_id="pb", service="ent",
                         preferred_slot_type="any", added_at="2025-06-01T00:00:00Z", seq=99))
    wl.add(WaitlistEntry(id="c", patient_id="pc", service="ent",
                         preferred_slot_type="any", added_at="2025-06-01T00:00:00Z", seq=99))
    assert [e.id for e in wl.list_by_service_ordered("ent").unwrap()] == ["b", "c", "a"]


def test_waitlist_find_active_and_remove(stores: DynamoStores) -> None:
    wl = stores.waitlist
    wl.add(WaitlistEntry(id="w1", patient_id="p1", service="ent",
                         preferred_slot_type="morning", added_at="2025-06-01T00:00:00Z", seq=0))
    assert wl.find_active("p1", "ent", "morning").unwrap() is not None
    assert wl.find_active("p1", "ent", "evening").unwrap() is None
    assert is_ok(wl.remove("w1"))
    assert wl.list_by_service_ordered("ent").unwrap() == []
    assert is_err(wl.remove("w1"))


# -- open decisions newest-first + status transition (Req 14.1) ------------


def _decision(id: str, generated_at: str) -> Decision:
    return Decision(id=id, kind=DecisionKind.GAP_FILL, finding_key=f"k-{id}",
                    summary="s", recommended_action="a", supporting_record_count=6,
                    generated_at=generated_at)


def test_open_decisions_newest_first_and_status_transition(stores: DynamoStores) -> None:
    ds = stores.decisions
    ds.create(_decision("d1", "2025-06-01T00:00:00Z"))
    ds.create(_decision("d2", "2025-06-03T00:00:00Z"))
    ds.create(_decision("d3", "2025-06-02T00:00:00Z"))
    assert [d.id for d in ds.list_open().unwrap()] == ["d2", "d3", "d1"]
    # Resolving moves the item out of the open partition entirely.
    ds.set_status("d2", DecisionStatus.APPROVED, "2025-06-04T00:00:00Z")
    assert [d.id for d in ds.list_open().unwrap()] == ["d3", "d1"]
    assert ds.find_open_by_finding_key("k-d3").unwrap() is not None
    assert ds.find_open_by_finding_key("k-d2").unwrap() is None


# -- atomic config save + Decimal price round-trip (Req 1.6, 16.2) ---------


def test_config_save_and_get_round_trip(stores: DynamoStores, emitter: RecordingEmitter) -> None:
    kb = ClinicKnowledgeBase(
        location="123 Main St",
        services=[ServiceConfig(name="ent", prep_instructions="fast 8h", price=150.75)],
        accepted_insurance=["Aetna"],
        providers=[Provider(id="prov1", name="Dr Who", specialty="ENT")],
        configured=True,
        updated_at="2025-06-01T00:00:00Z",
    )
    stores.clinic_knowledge_base.save(kb)
    got = stores.clinic_knowledge_base.get().unwrap()
    assert got is not None
    assert got.location == "123 Main St"
    assert got.services[0].price == 150.75  # float survived the Decimal boundary
    assert got.providers[0].id == "prov1"
    assert ChangeEvent(ChangeEntity.CLINIC_KNOWLEDGE_BASE, "CONFIG", ChangeKind.UPDATED) in emitter.events


# -- call sessions + escalations recent-first (Req 15.2) -------------------


def test_call_session_finalize_and_recent(stores: DynamoStores) -> None:
    cs = stores.call_sessions
    cs.create(CallSession(id="c1", started_at="2025-06-01T09:00:00Z"))
    cs.create(CallSession(id="c2", started_at="2025-06-01T10:00:00Z"))
    finalized = cs.finalize("c1", CallOutcome.BOOKED, PatientRef(name="Alice")).unwrap()
    assert finalized.outcome == CallOutcome.BOOKED
    assert finalized.patient_ref is not None and finalized.patient_ref.name == "Alice"
    assert [s.id for s in cs.list_recent(10).unwrap()] == ["c2", "c1"]
    assert [s.id for s in cs.list_recent(1).unwrap()] == ["c2"]


def test_escalations_recent_first(stores: DynamoStores) -> None:
    es = stores.escalations
    es.create(Escalation(id="e1", reason=EscalationReason.CLINICAL_CONTENT,
                         call_session_id="c1", context="ctx", created_at="2025-06-01T09:00:00Z"))
    es.create(Escalation(id="e2", reason=EscalationReason.PATIENT_REQUEST,
                         call_session_id="c2", context="ctx", created_at="2025-06-01T10:00:00Z"))
    assert [e.id for e in es.list_recent(10).unwrap()] == ["e2", "e1"]


def test_call_session_create_replaces_by_id(stores: DynamoStores) -> None:
    """Two creates for one id must leave one record, as the in-memory store does.

    Regression found against the real table: the CallSession SK embeds
    ``started_at``, so a reconnect reusing the same runtime session id wrote a
    *second* item. That left a phantom call in the activity log with no outcome,
    and ``finalize`` then updated the older of the two — so the live call's outcome
    landed on the wrong record.
    """
    stores.call_sessions.create(
        CallSession(id="call-dup", started_at="2026-03-04T09:00:00Z")
    )
    stores.call_sessions.create(
        CallSession(id="call-dup", started_at="2026-03-04T09:30:00Z")
    )

    listed = stores.call_sessions.list_recent(20)
    assert is_ok(listed)
    matching = [s for s in listed.value if s.id == "call-dup"]
    assert len(matching) == 1
    assert matching[0].started_at == "2026-03-04T09:30:00Z"


def test_finalize_after_a_recreate_updates_the_surviving_record(
    stores: DynamoStores,
) -> None:
    stores.call_sessions.create(
        CallSession(id="call-dup", started_at="2026-03-04T09:00:00Z")
    )
    stores.call_sessions.create(
        CallSession(id="call-dup", started_at="2026-03-04T09:30:00Z")
    )

    finalized = stores.call_sessions.finalize(
        "call-dup",
        CallOutcome.BOOKED,
        PatientRef(name="Dana Ellis"),
        ended_at="2026-03-04T09:35:00Z",
        transcript="[00:00] patient: hello",
        recording_uri="s3://bucket/call-recordings/2026/03/04/call-dup.wav",
    )

    assert is_ok(finalized)
    listed = stores.call_sessions.list_recent(20)
    assert is_ok(listed)
    matching = [s for s in listed.value if s.id == "call-dup"]
    assert len(matching) == 1
    session = matching[0]
    assert session.started_at == "2026-03-04T09:30:00Z"
    assert session.outcome == CallOutcome.BOOKED
    assert session.ended_at == "2026-03-04T09:35:00Z"
    assert session.transcript == "[00:00] patient: hello"
    assert session.recording_uri is not None
