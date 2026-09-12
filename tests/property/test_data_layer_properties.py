"""Data_Layer correctness properties (tasks 3.4–3.9).

Six Hypothesis property tests, each a single property run ≥ 100 iterations
against the in-memory fake stores (fast, deterministic — task notes):

- Property 24: Provider-id association and enforcement (Req 16.3, 16.7)
- Property 25: Empty initialization (Req 16.4)
- Property 26: Write atomicity across all interfaces (Req 1.6, 2.8, 3.8, 4.9,
  5.8, 7.4, 8.5, 9.9, 11.3, 13.8, 16.6) — uses the fault-injection wrapper
- Property 7: Every appointment references an existing patient (Req 3.5)
- Property 10: Waitlist ordering is stable and ascending by time added (Req 7.3)
- Property 19: Open Decisions feed ordering — newest-first (Req 14.1)
"""

from __future__ import annotations

import uuid

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from clinic_front_desk.data_layer.faults import fail_on, wrap
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
    Slot,
    SlotStatus,
    StoreErrorKind,
    WaitlistEntry,
    is_err,
    is_ok,
)
from clinic_front_desk.tools.appointments import book_appointment, cancel

pytestmark = pytest.mark.property

# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

_IDENT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    min_size=1,
    max_size=10,
)
_SERVICES = st.sampled_from(["ent", "audiology", "hearing_test"])
_SLOT_TYPES = st.sampled_from(["morning", "afternoon", "any"])
# A small set of timestamps so equal values (ties) are generated frequently.
_TIMESTAMPS = st.sampled_from(
    [
        "2025-06-01T00:00:00Z",
        "2025-06-02T00:00:00Z",
        "2025-06-03T00:00:00Z",
        "2025-06-04T00:00:00Z",
    ]
)
_DECISION_STATUS = st.sampled_from(list(DecisionStatus))


# ---------------------------------------------------------------------------
# Property 24: Provider-id association and enforcement (Req 16.3, 16.7)
# ---------------------------------------------------------------------------
# Feature: clinic-front-desk-agent, Property 24: a schedule-owning write is
# rejected with a missing-provider failure iff it lacks a provider identifier;
# every successfully stored such record carries exactly one provider identifier.


@settings(max_examples=200)
@given(
    has_provider=st.booleans(),
    provider_id=_IDENT,
    record_id=_IDENT,
    service=_SERVICES,
    entity=st.sampled_from(["appointment", "slot", "config"]),
)
def test_property_24_provider_id_enforcement(
    has_provider: bool,
    provider_id: str,
    record_id: str,
    service: str,
    entity: str,
) -> None:
    pid = provider_id if has_provider else ""

    if entity == "appointment":
        store = MemoryAppointmentStore()
        appt = Appointment(
            id=record_id,
            provider_id="placeholder",
            patient_id="p1",
            service=service,
            slot_id="s1",
            date="2025-06-01",
            time="09:00",
        )
        appt.provider_id = pid  # bypass the dataclass guard to exercise the store
        result = store.create(appt)
        if has_provider:
            assert is_ok(result)
            assert result.value.provider_id == pid and pid != ""
            stored = store.get(record_id).unwrap()
            assert stored is not None and stored.provider_id == pid
        else:
            assert is_err(result)
            assert result.error.kind is StoreErrorKind.VALIDATION
            assert result.error.field == "provider_id"
            # Non-destructive: nothing persisted on rejection.
            assert store.get(record_id).unwrap() is None

    elif entity == "slot":
        store = MemoryAppointmentStore()
        slot = Slot(
            id=record_id,
            provider_id="placeholder",
            service=service,
            start="2025-06-01T09:00:00Z",
            end="2025-06-01T09:30:00Z",
            status=SlotStatus.OPEN,
        )
        slot.provider_id = pid  # bypass the dataclass guard
        store.seed_slot(slot)
        result = store.set_slot_status(record_id, SlotStatus.BOOKED)
        if has_provider:
            assert is_ok(result)
            assert result.value.provider_id == pid and pid != ""
        else:
            assert is_err(result)
            assert result.error.kind is StoreErrorKind.VALIDATION
            assert result.error.field == "provider_id"
            # Status unchanged by the rejected write.
            assert store.get_slot(record_id).unwrap().status is SlotStatus.OPEN

    else:  # config — the provider roster is the schedule-owning record
        store = MemoryClinicKnowledgeBaseStore()
        kb = ClinicKnowledgeBase(
            location="123 Main St",
            providers=[Provider(id=pid, name="Dr Who", specialty="ENT")],
        )
        result = store.save(kb)
        if has_provider:
            assert is_ok(result)
            assert all(p.id != "" for p in result.value.providers)
        else:
            assert is_err(result)
            assert result.error.kind is StoreErrorKind.VALIDATION
            assert result.error.field == "provider_id"
            assert store.get().unwrap() is None


# ---------------------------------------------------------------------------
# Property 25: Empty initialization (Req 16.4)
# ---------------------------------------------------------------------------
# Feature: clinic-front-desk-agent, Property 25: a freshly initialized
# Data_Layer returns empty reads for every store until records are written,
# after which reads return exactly the written records.


@settings(max_examples=100)
@given(
    probe_id=_IDENT,
    name=_IDENT,
    phone=_IDENT,
    service=_SERVICES,
    slot_type=_SLOT_TYPES,
    added_at=_TIMESTAMPS,
    generated_at=_TIMESTAMPS,
)
def test_property_25_empty_initialization(
    probe_id: str,
    name: str,
    phone: str,
    service: str,
    slot_type: str,
    added_at: str,
    generated_at: str,
) -> None:
    patients = MemoryPatientStore()
    appointments = MemoryAppointmentStore()
    waitlist = MemoryWaitlistStore()
    decisions = MemoryDecisionStore()
    clinic = MemoryClinicKnowledgeBaseStore()
    sessions = MemoryCallSessionStore()
    escalations = MemoryEscalationStore()

    # Every read is empty before any write (Req 16.4).
    assert patients.get(probe_id).unwrap() is None
    assert patients.find_by_name_and_phone(name, phone).unwrap() == []
    assert appointments.get(probe_id).unwrap() is None
    assert appointments.get_slot(probe_id).unwrap() is None
    assert appointments.list_by_patient(probe_id).unwrap() == []
    assert appointments.list_open_slots(probe_id, service, "2025-01-01").unwrap() == []
    assert waitlist.find_active(probe_id, service, slot_type).unwrap() is None
    assert waitlist.list_by_service_ordered(service).unwrap() == []
    assert decisions.list_open().unwrap() == []
    assert decisions.find_open_by_finding_key(probe_id).unwrap() is None
    assert clinic.get().unwrap() is None
    assert sessions.list_recent(10).unwrap() == []
    assert escalations.list_recent(10).unwrap() == []

    # After writing, reads return exactly the written records.
    patient = Patient(id=probe_id, name=name, callback_phone=phone)
    assert is_ok(patients.create(patient))
    assert patients.get(probe_id).unwrap() == patient
    assert patients.find_by_name_and_phone(name, phone).unwrap() == [patient]

    entry = WaitlistEntry(
        id=probe_id,
        patient_id=probe_id,
        service=service,
        preferred_slot_type=slot_type,
        added_at=added_at,
        seq=0,
    )
    added = waitlist.add(entry).unwrap()
    assert waitlist.list_by_service_ordered(service).unwrap() == [added]

    decision = Decision(
        id=probe_id,
        kind=DecisionKind.GAP_FILL,
        finding_key=f"k-{probe_id}",
        summary="s",
        recommended_action="a",
        supporting_record_count=6,
        status=DecisionStatus.OPEN,
        generated_at=generated_at,
    )
    assert is_ok(decisions.create(decision))
    assert decisions.list_open().unwrap() == [decision]


# ---------------------------------------------------------------------------
# Property 26: Write atomicity across all interfaces (Req 16.6, ...)
# ---------------------------------------------------------------------------
# Feature: clinic-front-desk-agent, Property 26: a write that fails to persist
# returns a failure result and leaves all previously stored records unchanged,
# uniformly across every store write (booking, reschedule, cancel, waitlist add,
# decision persistence, config save, and the remaining store mutations).

_ATOMICITY_OPS = st.sampled_from(
    [
        "appointment_create",
        "appointment_move",
        "appointment_remove",
        "patient_create",
        "waitlist_add",
        "waitlist_remove",
        "decision_create",
        "decision_set_status",
        "config_save",
        "call_session_create",
        "call_session_finalize",
        "escalation_create",
    ]
)


@settings(max_examples=200)
@given(op=_ATOMICITY_OPS, key=_IDENT)
def test_property_26_write_atomicity(op: str, key: str) -> None:
    if op == "appointment_create":
        store = MemoryAppointmentStore()
        existing = Appointment(
            id="existing",
            provider_id="prov1",
            patient_id="p0",
            service="ent",
            slot_id="s0",
            date="2025-06-01",
            time="08:00",
        )
        store.create(existing)
        before = store.get("existing").unwrap()
        faulty = wrap(store, fail_on("create"))
        new = Appointment(
            id=f"a-{key}",
            provider_id="prov1",
            patient_id="p1",
            service="ent",
            slot_id="s1",
            date="2025-06-01",
            time="09:00",
        )
        result = faulty.create(new)
        assert is_err(result)
        assert store.get(f"a-{key}").unwrap() is None
        assert store.get("existing").unwrap() == before

    elif op == "appointment_move":
        store = MemoryAppointmentStore()
        store.seed_slots(
            [
                Slot(id="s1", provider_id="prov1", service="ent",
                     start="2025-06-01T09:00:00Z", end="2025-06-01T09:30:00Z",
                     status=SlotStatus.BOOKED),
                Slot(id="s2", provider_id="prov1", service="ent",
                     start="2025-06-02T09:00:00Z", end="2025-06-02T09:30:00Z"),
            ]
        )
        store.create(Appointment(id="a1", provider_id="prov1", patient_id="p1",
                                 service="ent", slot_id="s1", date="2025-06-01", time="09:00"))
        faulty = wrap(store, fail_on("move"))
        result = faulty.move("a1", "s2")
        assert is_err(result)
        assert store.get("a1").unwrap().slot_id == "s1"
        assert store.get_slot("s1").unwrap().status is SlotStatus.BOOKED
        assert store.get_slot("s2").unwrap().status is SlotStatus.OPEN

    elif op == "appointment_remove":
        store = MemoryAppointmentStore()
        store.seed_slot(Slot(id="s1", provider_id="prov1", service="ent",
                             start="2025-06-01T09:00:00Z", end="2025-06-01T09:30:00Z",
                             status=SlotStatus.BOOKED))
        store.create(Appointment(id="a1", provider_id="prov1", patient_id="p1",
                                 service="ent", slot_id="s1", date="2025-06-01", time="09:00"))
        before = store.get("a1").unwrap()
        faulty = wrap(store, fail_on("remove"))
        result = faulty.remove("a1")
        assert is_err(result)
        assert store.get("a1").unwrap() == before
        assert store.get_slot("s1").unwrap().status is SlotStatus.BOOKED

    elif op == "patient_create":
        store = MemoryPatientStore()
        store.create(Patient(id="p0", name="Ann", callback_phone="555-0"))
        before = store.get("p0").unwrap()
        faulty = wrap(store, fail_on("create"))
        result = faulty.create(Patient(id=f"p-{key}", name="Bea", callback_phone="555-1"))
        assert is_err(result)
        assert store.get(f"p-{key}").unwrap() is None
        assert store.get("p0").unwrap() == before

    elif op == "waitlist_add":
        store = MemoryWaitlistStore()
        store.add(WaitlistEntry(id="w0", patient_id="p0", service="ent",
                                preferred_slot_type="any", added_at="2025-06-01T00:00:00Z", seq=0))
        before = store.list_by_service_ordered("ent").unwrap()
        faulty = wrap(store, fail_on("add"))
        result = faulty.add(WaitlistEntry(id=f"w-{key}", patient_id="p1", service="ent",
                                          preferred_slot_type="any",
                                          added_at="2025-06-02T00:00:00Z", seq=0))
        assert is_err(result)
        assert store.list_by_service_ordered("ent").unwrap() == before

    elif op == "waitlist_remove":
        store = MemoryWaitlistStore()
        store.add(WaitlistEntry(id="w0", patient_id="p0", service="ent",
                                preferred_slot_type="any", added_at="2025-06-01T00:00:00Z", seq=0))
        before = store.list_by_service_ordered("ent").unwrap()
        faulty = wrap(store, fail_on("remove"))
        result = faulty.remove("w0")
        assert is_err(result)
        assert store.list_by_service_ordered("ent").unwrap() == before

    elif op == "decision_create":
        store = MemoryDecisionStore()
        store.create(Decision(id="d0", kind=DecisionKind.GAP_FILL, finding_key="k0",
                              summary="s", recommended_action="a", supporting_record_count=6,
                              generated_at="2025-06-01T00:00:00Z"))
        before = store.list_open().unwrap()
        faulty = wrap(store, fail_on("create"))
        result = faulty.create(Decision(id=f"d-{key}", kind=DecisionKind.GAP_FILL,
                                        finding_key=f"k-{key}", summary="s", recommended_action="a",
                                        supporting_record_count=6, generated_at="2025-06-02T00:00:00Z"))
        assert is_err(result)
        assert store.list_open().unwrap() == before

    elif op == "decision_set_status":
        store = MemoryDecisionStore()
        store.create(Decision(id="d0", kind=DecisionKind.GAP_FILL, finding_key="k0",
                              summary="s", recommended_action="a", supporting_record_count=6,
                              generated_at="2025-06-01T00:00:00Z"))
        before = store.list_open().unwrap()
        faulty = wrap(store, fail_on("set_status"))
        result = faulty.set_status("d0", DecisionStatus.APPROVED, "2025-06-05T00:00:00Z")
        assert is_err(result)
        assert store.list_open().unwrap() == before  # still open, unchanged

    elif op == "config_save":
        store = MemoryClinicKnowledgeBaseStore()
        initial = ClinicKnowledgeBase(location="Main St",
                                      providers=[Provider(id="prov1", name="Dr", specialty="ENT")],
                                      configured=True)
        store.save(initial)
        before = store.get().unwrap()
        faulty = wrap(store, fail_on("save"))
        result = faulty.save(ClinicKnowledgeBase(location="Other St",
                             providers=[Provider(id="prov2", name="Dr2", specialty="Audio")]))
        assert is_err(result)
        assert store.get().unwrap() == before  # prior config fully retained

    elif op == "call_session_create":
        store = MemoryCallSessionStore()
        store.create(CallSession(id="c0", started_at="2025-06-01T09:00:00Z"))
        before = store.list_recent(10).unwrap()
        faulty = wrap(store, fail_on("create"))
        result = faulty.create(CallSession(id=f"c-{key}", started_at="2025-06-01T10:00:00Z"))
        assert is_err(result)
        assert store.list_recent(10).unwrap() == before

    elif op == "call_session_finalize":
        store = MemoryCallSessionStore()
        store.create(CallSession(id="c0", started_at="2025-06-01T09:00:00Z"))
        before = store.list_recent(10).unwrap()
        faulty = wrap(store, fail_on("finalize"))
        result = faulty.finalize("c0", CallOutcome.BOOKED, PatientRef(name="Ann"))
        assert is_err(result)
        assert store.list_recent(10).unwrap() == before  # outcome still None

    else:  # escalation_create
        store = MemoryEscalationStore()
        store.create(Escalation(id="e0", reason=EscalationReason.CLINICAL_CONTENT,
                                call_session_id="c0", context="ctx",
                                created_at="2025-06-01T09:00:00Z"))
        before = store.list_recent(10).unwrap()
        faulty = wrap(store, fail_on("create"))
        result = faulty.create(Escalation(id=f"e-{key}", reason=EscalationReason.PATIENT_REQUEST,
                                          call_session_id="c1", context="ctx",
                                          created_at="2025-06-01T10:00:00Z"))
        assert is_err(result)
        assert store.list_recent(10).unwrap() == before


# ---------------------------------------------------------------------------
# Property 7: Every appointment references an existing patient (Req 3.5)
# ---------------------------------------------------------------------------
# Feature: clinic-front-desk-agent, Property 7: for any sequence of booking and
# cancellation operations, every stored appointment's patient_id resolves to an
# existing patient record.

_PROVIDER = "prov1"
_BOOK_SERVICE = "ent"


@settings(max_examples=100)
@given(
    callers=st.lists(st.tuples(_IDENT, _IDENT), min_size=1, max_size=6),
    ops=st.lists(
        st.tuples(
            st.sampled_from(["book", "cancel"]),
            st.integers(min_value=0, max_value=5),
        ),
        min_size=1,
        max_size=15,
    ),
)
def test_property_7_appointment_references_existing_patient(
    callers: list[tuple[str, str]],
    ops: list[tuple[str, int]],
) -> None:
    appt_store = MemoryAppointmentStore()
    patient_store = MemoryPatientStore()

    # Seed a pool of bookable slots.
    slots = [
        Slot(id=f"slot-{i}", provider_id=_PROVIDER, service=_BOOK_SERVICE,
             start=f"2025-06-{i + 1:02d}T09:00:00Z", end=f"2025-06-{i + 1:02d}T09:30:00Z")
        for i in range(8)
    ]
    appt_store.seed_slots(slots)

    appt_ids: list[str] = []

    def assert_invariant() -> None:
        for aid in appt_ids:
            appt = appt_store.get(aid).unwrap()
            if appt is None:
                continue  # cancelled — no longer stored
            resolved = patient_store.get(appt.patient_id).unwrap()
            assert resolved is not None, f"appointment {aid} references missing patient"

    for action, idx in ops:
        if action == "book":
            name, phone = callers[idx % len(callers)]
            # Lookup-or-create the patient (mirrors the booking flow, Req 3.4/3.5).
            found = patient_store.find_by_name_and_phone(name, phone).unwrap()
            if found:
                patient_id = found[0].id
            else:
                patient_id = str(uuid.uuid4())
                patient_store.create(
                    Patient(id=patient_id, name=name, callback_phone=phone)
                )
            open_slots = appt_store.list_open_slots(_PROVIDER, _BOOK_SERVICE, "2025-01-01").unwrap()
            if not open_slots:
                continue
            result = book_appointment(
                appt_store,
                provider_id=_PROVIDER,
                patient_id=patient_id,
                slot_id=open_slots[0].id,
                service=_BOOK_SERVICE,
            )
            if is_ok(result):
                appt_ids.append(result.value.appointment.id)
        else:  # cancel
            if appt_ids:
                cancel(appt_store, appointment_id=appt_ids[idx % len(appt_ids)])
        assert_invariant()

    assert_invariant()


# ---------------------------------------------------------------------------
# Property 10: Waitlist ordering is stable and ascending by time added (Req 7.3)
# ---------------------------------------------------------------------------
# Feature: clinic-front-desk-agent, Property 10: list_by_service_ordered returns
# entries ascending by added_at, breaking ties by insertion order via the
# monotonic seq the store assigns.


@settings(max_examples=100)
@given(
    entries=st.lists(
        st.tuples(_IDENT, _TIMESTAMPS),
        min_size=1,
        max_size=12,
        unique_by=lambda t: t[0],  # unique entry ids
    )
)
def test_property_10_waitlist_ordering(entries: list[tuple[str, str]]) -> None:
    store = MemoryWaitlistStore()
    service = "ent"

    insertion_order: list[str] = []
    for entry_id, added_at in entries:
        store.add(
            WaitlistEntry(
                id=entry_id,
                patient_id=f"pat-{entry_id}",
                service=service,
                preferred_slot_type="any",
                added_at=added_at,
                seq=999,  # caller suggestion is ignored; store assigns its own
            )
        )
        insertion_order.append(entry_id)

    ordered = store.list_by_service_ordered(service).unwrap()
    added_at_by_id = {eid: ts for eid, ts in entries}

    # Expected: stable sort of the insertion order by ascending added_at. Python's
    # sort is stable, so equal added_at entries keep insertion order — exactly the
    # (added_at, seq) tiebreak the store guarantees.
    expected_ids = sorted(insertion_order, key=lambda eid: added_at_by_id[eid])
    assert [e.id for e in ordered] == expected_ids

    # added_at is non-decreasing, and seq strictly increases with insertion order.
    assert all(
        ordered[i].added_at <= ordered[i + 1].added_at for i in range(len(ordered) - 1)
    )
    seqs_in_insertion_order = [
        next(e.seq for e in ordered if e.id == eid) for eid in insertion_order
    ]
    assert seqs_in_insertion_order == sorted(seqs_in_insertion_order)
    assert len(set(seqs_in_insertion_order)) == len(seqs_in_insertion_order)


# ---------------------------------------------------------------------------
# Property 19: Open Decisions feed ordering (Req 14.1)
# ---------------------------------------------------------------------------
# Feature: clinic-front-desk-agent, Property 19: list_open returns exactly the
# open decisions, ordered most-recently-generated first.


@settings(max_examples=100)
@given(
    decisions=st.lists(
        st.tuples(_IDENT, _TIMESTAMPS, _DECISION_STATUS),
        min_size=1,
        max_size=12,
        unique_by=lambda t: t[0],  # unique decision ids
    )
)
def test_property_19_open_decisions_newest_first(
    decisions: list[tuple[str, str, DecisionStatus]],
) -> None:
    store = MemoryDecisionStore()

    insertion_index: dict[str, int] = {}
    for i, (dec_id, generated_at, status) in enumerate(decisions):
        store.create(
            Decision(
                id=dec_id,
                kind=DecisionKind.GAP_FILL,
                finding_key=f"k-{dec_id}",
                summary="s",
                recommended_action="a",
                supporting_record_count=6,
                status=status,
                generated_at=generated_at,
            )
        )
        insertion_index[dec_id] = i

    listed = store.list_open().unwrap()

    # Exactly the open decisions are listed.
    expected_open = {dec_id for dec_id, _, status in decisions if status is DecisionStatus.OPEN}
    assert {d.id for d in listed} == expected_open

    # Newest-first: descending generated_at, ties broken by descending insertion.
    generated_by_id = {dec_id: gen for dec_id, gen, _ in decisions}
    expected_order = sorted(
        expected_open,
        key=lambda dec_id: (generated_by_id[dec_id], insertion_index[dec_id]),
        reverse=True,
    )
    assert [d.id for d in listed] == expected_order
    # generated_at is non-increasing down the feed.
    assert all(
        listed[i].generated_at >= listed[i + 1].generated_at for i in range(len(listed) - 1)
    )
