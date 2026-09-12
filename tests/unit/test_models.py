"""Unit tests for the domain data models (task 2.2).

Covers three concerns from the design's Data Models section:

1. Model construction — including the required, non-empty ``provider_id``
   invariant on schedule-owning entities (Req 16.3, 16.7).
2. Enum / status values — the exact string values the wire format depends on.
3. Round-trip (de)serialization to/from the DynamoDB single-table item shape.

_Requirements: 16.3_
"""

from __future__ import annotations

import pytest

from clinic_front_desk.models import (
    Ambiguous,
    Appointment,
    AppointmentStatus,
    CallOutcome,
    CallSession,
    ClinicKnowledgeBase,
    DayHours,
    Decision,
    DecisionKind,
    DecisionStatus,
    Duplicate,
    Err,
    Escalation,
    EscalationReason,
    Finding,
    NotFound,
    NotOffered,
    Ok,
    Patient,
    PatientRef,
    Provider,
    ScheduleRule,
    ServiceConfig,
    Slot,
    SlotStatus,
    StoreError,
    StoreErrorKind,
    StoreFailure,
    Validation,
    WaitlistEntry,
    appointment_from_item,
    appointment_to_item,
    call_session_from_item,
    call_session_to_item,
    clinic_kb_from_item,
    clinic_kb_to_item,
    decision_from_item,
    decision_to_item,
    escalation_from_item,
    escalation_to_item,
    is_err,
    is_ok,
    patient_from_item,
    patient_to_item,
    provider_from_item,
    provider_to_item,
    slot_from_item,
    slot_to_item,
    waitlist_entry_from_item,
    waitlist_entry_to_item,
)

# ---------------------------------------------------------------------------
# Model construction
# ---------------------------------------------------------------------------


def test_slot_requires_provider_id() -> None:
    """A Slot with a blank provider_id is rejected at construction (Req 16.7)."""
    with pytest.raises(ValueError, match="provider_id"):
        Slot(id="s1", provider_id="", service="hearing_test", start="t", end="t2")


def test_appointment_requires_provider_id() -> None:
    """An Appointment with a blank provider_id is rejected at construction (Req 16.7)."""
    with pytest.raises(ValueError, match="provider_id"):
        Appointment(
            id="a1",
            provider_id="",
            patient_id="p1",
            service="hearing_test",
            slot_id="s1",
            date="2025-06-01",
            time="09:00",
        )


def test_schedule_owning_models_accept_provider_id() -> None:
    """With a provider_id present, schedule-owning models construct fine."""
    slot = Slot(id="s1", provider_id="prov-1", service="hearing_test", start="t", end="t2")
    appt = Appointment(
        id="a1",
        provider_id="prov-1",
        patient_id="p1",
        service="hearing_test",
        slot_id="s1",
        date="2025-06-01",
        time="09:00",
    )
    assert slot.provider_id == "prov-1"
    assert appt.provider_id == "prov-1"


def test_default_statuses_and_seq() -> None:
    """Defaults match the design: slots open, appointments booked, waitlist active."""
    slot = Slot(id="s1", provider_id="prov-1", service="svc", start="t", end="t2")
    appt = Appointment(
        id="a1",
        provider_id="prov-1",
        patient_id="p1",
        service="svc",
        slot_id="s1",
        date="2025-06-01",
        time="09:00",
    )
    wl = WaitlistEntry(
        id="w1", patient_id="p1", service="svc", preferred_slot_type="any", added_at="t", seq=0
    )
    assert slot.status is SlotStatus.OPEN
    assert appt.status is AppointmentStatus.BOOKED
    assert wl.active is True
    assert wl.seq == 0


def test_finding_defaults_not_actionable() -> None:
    """A Finding defaults to non-actionable with zero supporting records (Req 13.4/13.6)."""
    f = Finding(key="k", kind=DecisionKind.UNMET_DEMAND, summary="s", recommended_action="a")
    assert f.actionable is False
    assert f.supporting_record_count == 0


# ---------------------------------------------------------------------------
# Result / error types
# ---------------------------------------------------------------------------


def test_ok_and_err_discriminate() -> None:
    ok: Ok[int] = Ok(3)
    err: Err[StoreError] = Err(StoreError(kind=StoreErrorKind.NOT_FOUND, detail="nope"))
    assert ok.ok is True
    assert err.ok is False
    assert is_ok(ok) and not is_err(ok)
    assert is_err(err) and not is_ok(err)
    assert ok.unwrap() == 3


def test_err_unwrap_raises() -> None:
    err: Err[StoreError] = Err(StoreError(kind=StoreErrorKind.STORE_FAILURE, detail="boom"))
    with pytest.raises(ValueError):
        err.unwrap()


def test_tool_error_kinds() -> None:
    """Each ToolError variant carries its design-specified discriminator/fields."""
    assert StoreFailure(store="AppointmentStore", detail="d").kind == "store_failure"
    assert NotFound(detail="d").kind == "not_found"
    assert Ambiguous(candidates=["a", "b"]).kind == "ambiguous"
    assert Validation(field="service", detail="d").kind == "validation"
    assert NotOffered(named_service="mri").kind == "not_offered"
    assert Duplicate(entry_id="w1").kind == "duplicate"


# ---------------------------------------------------------------------------
# Enum / status values (exact wire strings)
# ---------------------------------------------------------------------------


def test_enum_string_values() -> None:
    # "blocked" was added for the doctor taking time off the calendar. It is a
    # persisted value, so it is pinned here like the others: renaming it would
    # orphan every already-stored blocked slot.
    assert [s.value for s in SlotStatus] == ["open", "held", "booked", "blocked"]
    assert [s.value for s in AppointmentStatus] == [
        "booked",
        "rescheduled",
        "cancelled",
        "completed",
        "no_show",
    ]
    assert [s.value for s in DecisionKind] == [
        "gap_fill",
        "no_show_trend",
        "schedule_gap",
        "unmet_demand",
        "unoffered_service_demand",
    ]
    assert [s.value for s in DecisionStatus] == ["open", "approved", "dismissed", "action_failed"]
    assert [s.value for s in CallOutcome] == [
        "booked",
        "rescheduled",
        "cancelled",
        "waitlisted",
        "escalated",
        "no_action",
        "interrupted",
    ]
    assert [s.value for s in EscalationReason] == [
        "clinical_content",
        "outside_admin_rules",
        "patient_distress",
        "patient_request",
    ]


def test_str_enum_compares_to_str() -> None:
    assert SlotStatus.BOOKED == "booked"
    assert AppointmentStatus.NO_SHOW == "no_show"


# ---------------------------------------------------------------------------
# DynamoDB round-trip (de)serialization
# ---------------------------------------------------------------------------


def _sample_provider() -> Provider:
    return Provider(
        id="prov-1",
        name="Dr. Ada",
        specialty="ENT",
        schedule=[ScheduleRule(day_of_week=1, start="09:00", end="17:00")],
    )


def test_provider_round_trip() -> None:
    p = _sample_provider()
    item = provider_to_item(p)
    assert item["PK"] == "CLINIC#config"
    assert item["SK"] == "PROVIDER#prov-1"
    assert provider_from_item(item) == p


def test_clinic_kb_round_trip() -> None:
    kb = ClinicKnowledgeBase(
        location="123 Main St",
        hours={0: None, 1: DayHours(open="09:00", close="17:00")},
        services=[ServiceConfig(name="hearing_test", prep_instructions="Arrive early", price=120.5)],
        accepted_insurance=["Aetna", "Cigna"],
        providers=[_sample_provider()],
        configured=True,
        updated_at="2025-06-01T00:00:00Z",
    )
    item = clinic_kb_to_item(kb)
    assert item["PK"] == "CLINIC#config"
    assert item["SK"] == "CONFIG"
    assert clinic_kb_from_item(item) == kb


def test_slot_round_trip_and_gsi() -> None:
    slot = Slot(
        id="slot-1",
        provider_id="prov-1",
        service="hearing_test",
        start="2025-06-01T09:00:00Z",
        end="2025-06-01T09:30:00Z",
        status=SlotStatus.OPEN,
    )
    item = slot_to_item(slot)
    assert item["PK"] == "PROV#prov-1"
    assert item["SK"] == "SLOT#2025-06-01T09:00:00Z"
    assert item["GSI1PK"] == "SERVICE#hearing_test#STATUS#open"
    assert slot_from_item(item) == slot


def test_appointment_round_trip_and_gsi() -> None:
    appt = Appointment(
        id="appt-1",
        provider_id="prov-1",
        patient_id="pat-1",
        service="hearing_test",
        slot_id="slot-1",
        date="2025-06-01",
        time="09:00",
        status=AppointmentStatus.BOOKED,
        created_at="2025-05-01T00:00:00Z",
        updated_at="2025-05-01T00:00:00Z",
    )
    item = appointment_to_item(appt)
    assert item["PK"] == "PROV#prov-1"
    assert item["SK"] == "APPT#2025-06-01#09:00#appt-1"
    assert item["GSI2PK"] == "PATIENT#pat-1"
    assert appointment_from_item(item) == appt


def test_patient_round_trip_and_gsi() -> None:
    patient = Patient(
        id="pat-1",
        name="Sam Doe",
        callback_phone="+15551234567",
        extra_identifiers={"dob": "1990-01-01"},
        created_at="2025-05-01T00:00:00Z",
    )
    item = patient_to_item(patient)
    assert item["PK"] == "PATIENT#pat-1"
    assert item["SK"] == "PROFILE"
    # The lookup key is normalised — case levelled, phone reduced to its national
    # digits — because a spoken name and a dictated number never arrive in the same
    # shape twice. Keyed on the raw strings, a patient could not be found when she
    # said her own name.
    assert item["GSI3PK"] == "NAMEPHONE#sam doe#5551234567"
    # The stored record itself is untouched: the doctor reads what was entered.
    assert item["name"] == "Sam Doe"
    assert item["callback_phone"] == "+15551234567"
    assert patient_from_item(item) == patient


def test_waitlist_entry_round_trip_and_order_key() -> None:
    entry = WaitlistEntry(
        id="wl-1",
        patient_id="pat-1",
        service="hearing_test",
        preferred_slot_type="morning",
        added_at="2025-05-01T00:00:00Z",
        seq=7,
        active=True,
    )
    item = waitlist_entry_to_item(entry)
    assert item["PK"] == "WAITLIST#hearing_test"
    # SK encodes ascending addedAt then the seq tiebreaker (Req 7.3).
    assert item["SK"] == "2025-05-01T00:00:00Z#7"
    assert waitlist_entry_from_item(item) == entry


def test_decision_round_trip_and_dedupe_key() -> None:
    decision = Decision(
        id="dec-1",
        kind=DecisionKind.GAP_FILL,
        finding_key="gap_fill#slot-1",
        summary="Open slot matches a waitlisted patient",
        recommended_action="Contact the earliest matching waitlisted patient",
        action_payload={"slotId": "slot-1"},
        supporting_record_count=6,
        status=DecisionStatus.OPEN,
        generated_at="2025-06-01T00:00:00Z",
        resolved_at=None,
    )
    item = decision_to_item(decision)
    assert item["PK"] == "DECISION#open"
    assert item["SK"] == "2025-06-01T00:00:00Z#dec-1"
    assert item["GSI4PK"] == "FINDINGKEY#gap_fill#slot-1"
    assert decision_from_item(item) == decision


def test_call_session_round_trip() -> None:
    session = CallSession(
        id="cs-1",
        started_at="2025-06-01T00:00:00Z",
        ended_at="2025-06-01T00:05:00Z",
        outcome=CallOutcome.BOOKED,
        patient_ref=PatientRef(patient_id="pat-1", name="Sam Doe", callback_phone="+15551234567"),
        transcript="...",
    )
    item = call_session_to_item(session)
    assert item["PK"] == "CALLSESSION"
    assert item["SK"] == "2025-06-01T00:00:00Z#cs-1"
    assert call_session_from_item(item) == session


def test_call_session_round_trip_minimal() -> None:
    """A session with no outcome/patient_ref/transcript still round-trips."""
    session = CallSession(id="cs-2", started_at="2025-06-01T00:00:00Z")
    assert call_session_from_item(call_session_to_item(session)) == session


def test_escalation_round_trip() -> None:
    esc = Escalation(
        id="esc-1",
        reason=EscalationReason.CLINICAL_CONTENT,
        call_session_id="cs-1",
        context="patient asked for a diagnosis",
        created_at="2025-06-01T00:00:00Z",
        patient_ref=PatientRef(name="Sam Doe"),
    )
    item = escalation_to_item(esc)
    assert item["PK"] == "ESCALATION"
    assert item["SK"] == "2025-06-01T00:00:00Z#esc-1"
    assert escalation_from_item(item) == esc
