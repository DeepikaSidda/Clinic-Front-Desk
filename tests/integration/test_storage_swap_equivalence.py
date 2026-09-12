"""Property 27: Storage-swap observable equivalence (task 4.2, Req 16.5).

The same sequence of Data_Layer operations is run against two conforming store
implementations — the DynamoDB single-table stores (moto-backed) and the
in-memory fakes — and the observable results are asserted equivalent. If they
match for arbitrary operation sequences, agent code needs no change when storage
is swapped.

This is a single Hypothesis property (≥ 100 iterations). Because tie-break order
for equal sort keys is an unspecified implementation detail (the two backends
break ties differently), the generators use distinct sort keys where ordering is
observable (decision ``generated_at``, session ``started_at``, escalation
``created_at``) so the compared ordering reflects the contract, not a backend
quirk.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from moto import mock_aws

from clinic_front_desk.data_layer.dynamodb import create_stores, create_table
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
    ServiceConfig,
    Slot,
    SlotStatus,
    WaitlistEntry,
    is_ok,
)

pytestmark = [pytest.mark.property, pytest.mark.integration]

_REGION = "us-east-1"
_TABLE = "clinic-swap-equivalence"
_PROVIDER = "prov1"
_SERVICES = ["ent", "audio"]

# Fixed id pools so operations that target missing records fail identically on
# both backends (both must return the same not-found outcome).
_PATIENTS = ["p1", "p2", "p3"]
_APPTS = ["a1", "a2", "a3"]
_SLOTS = ["s1", "s2", "s3", "s4"]
_WAITLIST = ["w1", "w2", "w3"]
_DECISIONS = ["d1", "d2", "d3", "d4"]
_SESSIONS = ["c1", "c2", "c3"]
_ESCALATIONS = ["e1", "e2", "e3"]


def _seed_slots() -> list[Slot]:
    return [
        Slot(
            id=f"s{i}",
            provider_id=_PROVIDER,
            service=_SERVICES[i % 2],
            start=f"2025-07-{i:02d}T09:00:00Z",
            end=f"2025-07-{i:02d}T09:30:00Z",
        )
        for i in range(1, 5)
    ]


# ---------------------------------------------------------------------------
# Command generators — each yields a (kind, params) tuple over the fixed pools.
# ---------------------------------------------------------------------------

_command = st.one_of(
    st.fixed_dictionaries(
        {"kind": st.just("patient_create"), "id": st.sampled_from(_PATIENTS),
         "name": st.sampled_from(["Ann", "Bea", "Cy"]),
         "phone": st.sampled_from(["555-1", "555-2"])}
    ),
    st.fixed_dictionaries(
        {"kind": st.just("appointment_create"), "id": st.sampled_from(_APPTS),
         "patient": st.sampled_from(_PATIENTS), "slot": st.sampled_from(_SLOTS),
         "service": st.sampled_from(_SERVICES)}
    ),
    st.fixed_dictionaries(
        {"kind": st.just("appointment_move"), "id": st.sampled_from(_APPTS),
         "slot": st.sampled_from(_SLOTS)}
    ),
    st.fixed_dictionaries(
        {"kind": st.just("appointment_remove"), "id": st.sampled_from(_APPTS)}
    ),
    st.fixed_dictionaries(
        {"kind": st.just("slot_status"), "slot": st.sampled_from(_SLOTS),
         "status": st.sampled_from(list(SlotStatus))}
    ),
    st.fixed_dictionaries(
        {"kind": st.just("waitlist_add"), "id": st.sampled_from(_WAITLIST),
         "patient": st.sampled_from(_PATIENTS), "service": st.sampled_from(_SERVICES),
         "slot_type": st.sampled_from(["morning", "afternoon", "any"]),
         "added_at": st.sampled_from(["2025-06-01T00:00:00Z", "2025-06-02T00:00:00Z"])}
    ),
    st.fixed_dictionaries(
        {"kind": st.just("waitlist_remove"), "id": st.sampled_from(_WAITLIST)}
    ),
    st.fixed_dictionaries(
        {"kind": st.just("decision_create"), "id": st.sampled_from(_DECISIONS)}
    ),
    st.fixed_dictionaries(
        {"kind": st.just("decision_set_status"), "id": st.sampled_from(_DECISIONS),
         "status": st.sampled_from([DecisionStatus.APPROVED, DecisionStatus.DISMISSED,
                                    DecisionStatus.ACTION_FAILED])}
    ),
    st.fixed_dictionaries(
        {"kind": st.just("config_save"), "location": st.sampled_from(["Main St", "Oak Ave"]),
         "price": st.sampled_from([120.5, 150.75, 99.0])}
    ),
    st.fixed_dictionaries(
        {"kind": st.just("call_session_create"), "id": st.sampled_from(_SESSIONS)}
    ),
    st.fixed_dictionaries(
        {"kind": st.just("call_session_finalize"), "id": st.sampled_from(_SESSIONS),
         "outcome": st.sampled_from(list(CallOutcome))}
    ),
    st.fixed_dictionaries(
        {"kind": st.just("escalation_create"), "id": st.sampled_from(_ESCALATIONS),
         "reason": st.sampled_from(list(EscalationReason))}
    ),
)


def _decision_ts(dec_id: str) -> str:
    return f"2025-08-{int(dec_id[1:]):02d}T00:00:00Z"


def _session_ts(sid: str) -> str:
    return f"2025-09-{int(sid[1:]):02d}T00:00:00Z"


def _escalation_ts(eid: str) -> str:
    return f"2025-10-{int(eid[1:]):02d}T00:00:00Z"


def _apply(bundle: Any, cmd: dict[str, Any]) -> tuple[str, Any]:
    """Apply one command and return a comparable normalized result."""
    kind = cmd["kind"]
    if kind == "patient_create":
        r = bundle.patients.create(Patient(id=cmd["id"], name=cmd["name"],
                                           callback_phone=cmd["phone"], created_at="2025-01-01T00:00:00Z"))
    elif kind == "appointment_create":
        idx = int(cmd["id"][1:])
        r = bundle.appointments.create(Appointment(
            id=cmd["id"], provider_id=_PROVIDER, patient_id=cmd["patient"],
            service=cmd["service"], slot_id=cmd["slot"],
            date="2025-06-01", time=f"{8 + idx:02d}:00",
            created_at="2025-01-01T00:00:00Z", updated_at="2025-01-01T00:00:00Z"))
    elif kind == "appointment_move":
        r = bundle.appointments.move(cmd["id"], cmd["slot"])
    elif kind == "appointment_remove":
        r = bundle.appointments.remove(cmd["id"])
    elif kind == "slot_status":
        r = bundle.appointments.set_slot_status(cmd["slot"], cmd["status"])
    elif kind == "waitlist_add":
        r = bundle.waitlist.add(WaitlistEntry(
            id=cmd["id"], patient_id=cmd["patient"], service=cmd["service"],
            preferred_slot_type=cmd["slot_type"], added_at=cmd["added_at"], seq=0))
    elif kind == "waitlist_remove":
        r = bundle.waitlist.remove(cmd["id"])
    elif kind == "decision_create":
        # A unique finding_key per decision honours the synthesizer's dedup
        # contract (at most one open decision per finding_key); with duplicate
        # keys, find_open_by_finding_key's pick is under-specified.
        r = bundle.decisions.create(Decision(
            id=cmd["id"], kind=DecisionKind.GAP_FILL, finding_key=f"k-{cmd['id']}",
            summary="s", recommended_action="a", supporting_record_count=6,
            status=DecisionStatus.OPEN, generated_at=_decision_ts(cmd["id"])))
    elif kind == "decision_set_status":
        r = bundle.decisions.set_status(cmd["id"], cmd["status"], "2025-12-01T00:00:00Z")
    elif kind == "config_save":
        r = bundle.clinic_knowledge_base.save(ClinicKnowledgeBase(
            location=cmd["location"],
            services=[ServiceConfig(name="ent", prep_instructions="fast", price=cmd["price"])],
            accepted_insurance=["Aetna"],
            providers=[Provider(id=_PROVIDER, name="Dr Who", specialty="ENT")],
            configured=True, updated_at="2025-06-01T00:00:00Z"))
    elif kind == "call_session_create":
        r = bundle.call_sessions.create(CallSession(id=cmd["id"], started_at=_session_ts(cmd["id"])))
    elif kind == "call_session_finalize":
        r = bundle.call_sessions.finalize(cmd["id"], cmd["outcome"], PatientRef(name="Ann"))
    elif kind == "escalation_create":
        r = bundle.escalations.create(Escalation(
            id=cmd["id"], reason=cmd["reason"], call_session_id="c1", context="ctx",
            created_at=_escalation_ts(cmd["id"])))
    else:  # pragma: no cover - guard
        raise AssertionError(f"unknown command {kind}")

    if is_ok(r):
        return ("ok", r.value)
    return ("err", r.error.kind)


def _read_battery(bundle: Any) -> list[Any]:
    """A fixed suite of reads whose results must match across backends."""
    out: list[Any] = []
    for pid in _PATIENTS:
        out.append(bundle.patients.get(pid).unwrap())
    # Candidate-list order for disambiguation is not part of the contract, so
    # compare it as a sorted list.
    out.append(sorted(bundle.patients.find_by_name_and_phone("Ann", "555-1").unwrap(),
                      key=lambda p: p.id))
    for aid in _APPTS:
        out.append(bundle.appointments.get(aid).unwrap())
    for sid in _SLOTS:
        out.append(bundle.appointments.get_slot(sid).unwrap())
    for pid in _PATIENTS:
        out.append(bundle.appointments.list_by_patient(pid).unwrap())
    for svc in _SERVICES:
        out.append(bundle.appointments.list_open_slots(_PROVIDER, svc, "2025-01-01").unwrap())
        # The bounded reads availability actually uses: a limit, and a bound with
        # minute precision rather than a bare date. Both backends must agree, or
        # the agent offers different times against DynamoDB than in tests.
        out.append(
            bundle.appointments.list_open_slots(_PROVIDER, svc, "2025-01-01", limit=1).unwrap()
        )
        out.append(
            bundle.appointments.list_open_slots(_PROVIDER, svc, "2025-01-01", limit=0).unwrap()
        )
        out.append(
            bundle.appointments.list_open_slots(_PROVIDER, svc, "2025-06-01T10:00").unwrap()
        )
        out.append(
            bundle.appointments.list_open_slots(
                _PROVIDER, svc, "2025-06-01T10:00", limit=2
            ).unwrap()
        )
        out.append(bundle.waitlist.list_by_service_ordered(svc).unwrap())
    out.append(bundle.appointments.list_by_provider_and_day(_PROVIDER, "2025-06-01").unwrap())
    out.append(bundle.decisions.list_open().unwrap())
    for dec_id in _DECISIONS:
        out.append(bundle.decisions.find_open_by_finding_key(f"k-{dec_id}").unwrap())
    out.append(bundle.decisions.find_open_by_finding_key("k-missing").unwrap())
    out.append(bundle.clinic_knowledge_base.get().unwrap())
    out.append(bundle.call_sessions.list_recent(50).unwrap())
    out.append(bundle.escalations.list_recent(50).unwrap())
    return out


def _memory_bundle() -> Any:
    bundle = SimpleNamespace(
        appointments=MemoryAppointmentStore(),
        patients=MemoryPatientStore(),
        waitlist=MemoryWaitlistStore(),
        decisions=MemoryDecisionStore(),
        clinic_knowledge_base=MemoryClinicKnowledgeBaseStore(),
        call_sessions=MemoryCallSessionStore(),
        escalations=MemoryEscalationStore(),
    )
    bundle.appointments.seed_slots(_seed_slots())
    return bundle


def _normalize_ops(ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give every waitlist add a unique id and point removes at a real one.

    Waitlist ids are freshly minted per add in the real booking flow, never
    re-used. Re-adding the *same* id is therefore out of contract, and the two
    backends legitimately differ on it (the fake overwrites by id; the
    single-table store keys on ``addedAt#seq`` so a re-add is a second row). We
    keep the operation sequence realistic by assigning each add a unique id and
    aiming each remove at the most recently added entry.
    """
    normalized: list[dict[str, Any]] = []
    last_waitlist_id: str | None = None
    for i, cmd in enumerate(ops):
        cmd = dict(cmd)
        if cmd["kind"] == "waitlist_add":
            cmd["id"] = f"w{i}"
            last_waitlist_id = cmd["id"]
        elif cmd["kind"] == "waitlist_remove":
            cmd["id"] = last_waitlist_id or "w-missing"
        normalized.append(cmd)
    return normalized


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(raw_ops=st.lists(_command, min_size=1, max_size=12))
def test_property_27_storage_swap_equivalence(raw_ops: list[dict[str, Any]]) -> None:
    # Feature: clinic-front-desk-agent, Property 27: two conforming store
    # implementations produce equivalent observable results for the same
    # operation sequence, so storage can be swapped without agent changes.
    ops = _normalize_ops(raw_ops)
    memory = _memory_bundle()
    memory_results = [_apply(memory, cmd) for cmd in ops]
    memory_reads = _read_battery(memory)

    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        table = create_table(dynamodb, _TABLE)
        dynamo = create_stores(table)
        dynamo.appointments.seed_slots(_seed_slots())

        dynamo_results = [_apply(dynamo, cmd) for cmd in ops]
        dynamo_reads = _read_battery(dynamo)

    assert dynamo_results == memory_results
    assert dynamo_reads == memory_reads
