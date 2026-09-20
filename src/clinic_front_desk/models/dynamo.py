"""DynamoDB single-table (de)serialization helpers (design "DynamoDB Table Design").

The single-table layout is an implementation detail hidden behind the
Data_Layer interfaces (Req 16.5); these helpers are the one place that knows the
``PK``/``SK`` + GSI key construction. Each entity has a ``*_to_item`` /
``*_from_item`` pair that round-trips exactly.

Design decisions:

- Items carry the composite key attributes (``PK``/``SK``), any GSI key
  attributes from the design's table, an ``entity`` discriminator, and the
  entity's own fields stored under their snake_case names. Reconstruction reads
  the field attributes (never re-parses the key), so round-tripping is exact.
- Values are plain JSON-compatible Python types (``str``/``int``/``float``/
  ``bool``/``None``/``list``/``dict``). Conversion of numbers to
  ``decimal.Decimal`` for the boto3 resource API happens at the DynamoDB client
  boundary (task 4.1), not here — keeping this layer storage-representation
  agnostic and trivially testable.
"""

from __future__ import annotations

from typing import Any

from .matching import patient_lookup_key
from .entities import (
    Appointment,
    CallSession,
    ClinicKnowledgeBase,
    DayHours,
    Decision,
    Escalation,
    Patient,
    PatientRef,
    Provider,
    ScheduleRule,
    ServiceConfig,
    Slot,
    SymptomRoute,
    WaitlistEntry,
)
from .enums import (
    AppointmentStatus,
    CallOutcome,
    DecisionKind,
    DecisionStatus,
    EscalationReason,
    SlotStatus,
)

Item = dict[str, Any]

# Partition/sort key literals used by the single-table layout.
CLINIC_PK = "CLINIC#config"
CONFIG_SK = "CONFIG"
CALLSESSION_PK = "CALLSESSION"
ESCALATION_PK = "ESCALATION"


# ---------------------------------------------------------------------------
# Nested-structure helpers
# ---------------------------------------------------------------------------


def _schedule_rule_to_dict(rule: ScheduleRule) -> Item:
    return {"day_of_week": rule.day_of_week, "start": rule.start, "end": rule.end}


def _schedule_rule_from_dict(d: Item) -> ScheduleRule:
    return ScheduleRule(day_of_week=int(d["day_of_week"]), start=d["start"], end=d["end"])


def _provider_to_dict(p: Provider) -> Item:
    return {
        "id": p.id,
        "name": p.name,
        "specialty": p.specialty,
        "schedule": [_schedule_rule_to_dict(r) for r in p.schedule],
    }


def _provider_from_dict(d: Item) -> Provider:
    return Provider(
        id=d["id"],
        name=d["name"],
        specialty=d["specialty"],
        schedule=[_schedule_rule_from_dict(r) for r in d.get("schedule", [])],
    )


def _service_to_dict(s: ServiceConfig) -> Item:
    return {"name": s.name, "prep_instructions": s.prep_instructions, "price": s.price}


def _service_from_dict(d: Item) -> ServiceConfig:
    return ServiceConfig(
        name=d["name"],
        prep_instructions=d.get("prep_instructions"),
        price=d.get("price"),
    )


def _hours_to_dict(hours: dict[int, DayHours | None]) -> Item:
    # DynamoDB map keys are strings.
    out: Item = {}
    for day, dh in hours.items():
        out[str(day)] = None if dh is None else {"open": dh.open, "close": dh.close}
    return out


def _hours_from_dict(d: Item) -> dict[int, DayHours | None]:
    out: dict[int, DayHours | None] = {}
    for day, dh in d.items():
        out[int(day)] = None if dh is None else DayHours(open=dh["open"], close=dh["close"])
    return out


def _patient_ref_to_dict(ref: PatientRef | None) -> Item | None:
    if ref is None:
        return None
    return {
        "patient_id": ref.patient_id,
        "name": ref.name,
        "callback_phone": ref.callback_phone,
    }


def _patient_ref_from_dict(d: Item | None) -> PatientRef | None:
    if d is None:
        return None
    return PatientRef(
        patient_id=d.get("patient_id"),
        name=d.get("name"),
        callback_phone=d.get("callback_phone"),
    )


# ---------------------------------------------------------------------------
# ClinicKnowledgeBase  (PK=CLINIC#config, SK=CONFIG)
# ---------------------------------------------------------------------------


def _symptom_route_to_dict(route: SymptomRoute) -> Item:
    return {
        "phrases": list(route.phrases),
        "service": route.service,
        "advice": route.advice,
        "urgent": route.urgent,
        "urgent_instruction": route.urgent_instruction,
    }


def _symptom_route_from_dict(d: Item) -> SymptomRoute:
    return SymptomRoute(
        phrases=list(d.get("phrases", [])),
        service=d.get("service", ""),
        advice=d.get("advice", ""),
        urgent=bool(d.get("urgent", False)),
        urgent_instruction=d.get("urgent_instruction", ""),
    )


def clinic_kb_to_item(kb: ClinicKnowledgeBase) -> Item:
    return {
        "PK": CLINIC_PK,
        "SK": CONFIG_SK,
        "entity": "ClinicKnowledgeBase",
        "location": kb.location,
        "hours": _hours_to_dict(kb.hours),
        "services": [_service_to_dict(s) for s in kb.services],
        "accepted_insurance": list(kb.accepted_insurance),
        "providers": [_provider_to_dict(p) for p in kb.providers],
        "symptom_routes": [_symptom_route_to_dict(r) for r in kb.symptom_routes],
        "configured": kb.configured,
        "updated_at": kb.updated_at,
    }


def clinic_kb_from_item(item: Item) -> ClinicKnowledgeBase:
    return ClinicKnowledgeBase(
        location=item["location"],
        hours=_hours_from_dict(item.get("hours", {})),
        services=[_service_from_dict(s) for s in item.get("services", [])],
        accepted_insurance=list(item.get("accepted_insurance", [])),
        providers=[_provider_from_dict(p) for p in item.get("providers", [])],
        # Defaulted, so a config written before routing existed loads cleanly and
        # simply routes nothing — which is the pre-feature behaviour.
        symptom_routes=[
            _symptom_route_from_dict(r) for r in item.get("symptom_routes", [])
        ],
        configured=bool(item.get("configured", False)),
        updated_at=item.get("updated_at", ""),
    )


# ---------------------------------------------------------------------------
# Provider  (PK=CLINIC#config, SK=PROVIDER#<id>)
# ---------------------------------------------------------------------------


def provider_to_item(p: Provider) -> Item:
    return {
        "PK": CLINIC_PK,
        "SK": f"PROVIDER#{p.id}",
        "entity": "Provider",
        **_provider_to_dict(p),
    }


def provider_from_item(item: Item) -> Provider:
    return _provider_from_dict(item)


# ---------------------------------------------------------------------------
# Slot  (PK=PROV#<providerId>, SK=SLOT#<start>, GSI1 open-slot lookup)
# ---------------------------------------------------------------------------


def slot_to_item(s: Slot) -> Item:
    return {
        "PK": f"PROV#{s.provider_id}",
        "SK": f"SLOT#{s.start}",
        "GSI1PK": f"SERVICE#{s.service}#STATUS#{SlotStatus(s.status).value}",
        "GSI1SK": s.start,
        "entity": "Slot",
        "id": s.id,
        "provider_id": s.provider_id,
        "service": s.service,
        "start": s.start,
        "end": s.end,
        "status": SlotStatus(s.status).value,
    }


def slot_from_item(item: Item) -> Slot:
    return Slot(
        id=item["id"],
        provider_id=item["provider_id"],
        service=item["service"],
        start=item["start"],
        end=item["end"],
        status=SlotStatus(item["status"]),
    )


# ---------------------------------------------------------------------------
# Appointment  (PK=PROV#<providerId>, SK=APPT#<date>#<time>#<id>, GSI2 by patient)
# ---------------------------------------------------------------------------


def appointment_to_item(a: Appointment) -> Item:
    return {
        "PK": f"PROV#{a.provider_id}",
        "SK": f"APPT#{a.date}#{a.time}#{a.id}",
        "GSI2PK": f"PATIENT#{a.patient_id}",
        "GSI2SK": f"APPT#{a.created_at}",
        "entity": "Appointment",
        "id": a.id,
        "provider_id": a.provider_id,
        "patient_id": a.patient_id,
        "service": a.service,
        "slot_id": a.slot_id,
        "date": a.date,
        "time": a.time,
        "status": AppointmentStatus(a.status).value,
        "created_at": a.created_at,
        "updated_at": a.updated_at,
    }


def appointment_from_item(item: Item) -> Appointment:
    return Appointment(
        id=item["id"],
        provider_id=item["provider_id"],
        patient_id=item["patient_id"],
        service=item["service"],
        slot_id=item["slot_id"],
        date=item["date"],
        time=item["time"],
        status=AppointmentStatus(item["status"]),
        created_at=item.get("created_at", ""),
        updated_at=item.get("updated_at", ""),
    )


# ---------------------------------------------------------------------------
# Patient  (PK=PATIENT#<id>, SK=PROFILE, GSI3 name+phone lookup)
# ---------------------------------------------------------------------------


def patient_to_item(p: Patient) -> Item:
    return {
        "PK": f"PATIENT#{p.id}",
        "SK": "PROFILE",
        # Normalised, because the caller's name arrives from speech-to-text in
        # whatever case it feels like. Keyed on the raw string, a patient whose
        # record reads "Lakshmi Prasad" could not be found when she said her own
        # name. The display name below is untouched.
        "GSI3PK": f"NAMEPHONE#{patient_lookup_key(p.name, p.callback_phone)}",
        "entity": "Patient",
        "id": p.id,
        "name": p.name,
        "callback_phone": p.callback_phone,
        "extra_identifiers": p.extra_identifiers,
        "created_at": p.created_at,
        **({"code": p.code} if p.code else {}),
        # Optional intake details. Written only when present so a record for a
        # caller who declined them carries no empty health attributes at all.
        **({"age": p.age} if p.age is not None else {}),
        **({"blood_group": p.blood_group} if p.blood_group else {}),
        **({"weight_kg": p.weight_kg} if p.weight_kg is not None else {}),
        **({"height_cm": p.height_cm} if p.height_cm is not None else {}),
    }


def patient_from_item(item: Item) -> Patient:
    age = item.get("age")
    weight = item.get("weight_kg")
    height = item.get("height_cm")
    return Patient(
        id=item["id"],
        name=item["name"],
        callback_phone=item["callback_phone"],
        extra_identifiers=item.get("extra_identifiers"),
        created_at=item.get("created_at", ""),
        code=item.get("code", ""),
        # from_dynamo has already turned Decimal into int/float; coerce anyway so a
        # record written by an older or hand-edited item still loads.
        age=int(age) if age is not None else None,
        blood_group=item.get("blood_group"),
        weight_kg=float(weight) if weight is not None else None,
        height_cm=float(height) if height is not None else None,
    )


# ---------------------------------------------------------------------------
# WaitlistEntry  (PK=WAITLIST#<service>, SK=<addedAt>#<seq>)
# ---------------------------------------------------------------------------


def waitlist_entry_to_item(e: WaitlistEntry) -> Item:
    return {
        "PK": f"WAITLIST#{e.service}",
        "SK": f"{e.added_at}#{e.seq}",
        "entity": "WaitlistEntry",
        "id": e.id,
        "patient_id": e.patient_id,
        "service": e.service,
        "preferred_slot_type": e.preferred_slot_type,
        "added_at": e.added_at,
        "seq": e.seq,
        "active": e.active,
    }


def waitlist_entry_from_item(item: Item) -> WaitlistEntry:
    return WaitlistEntry(
        id=item["id"],
        patient_id=item["patient_id"],
        service=item["service"],
        preferred_slot_type=item["preferred_slot_type"],
        added_at=item["added_at"],
        seq=int(item["seq"]),
        active=bool(item.get("active", True)),
    )


# ---------------------------------------------------------------------------
# Decision  (PK=DECISION#<status>, SK=<generatedAt>#<id>, GSI4 findingKey dedupe)
# ---------------------------------------------------------------------------


def decision_to_item(d: Decision) -> Item:
    return {
        "PK": f"DECISION#{DecisionStatus(d.status).value}",
        "SK": f"{d.generated_at}#{d.id}",
        "GSI4PK": f"FINDINGKEY#{d.finding_key}",
        "entity": "Decision",
        "id": d.id,
        "kind": DecisionKind(d.kind).value,
        "finding_key": d.finding_key,
        "summary": d.summary,
        "recommended_action": d.recommended_action,
        "action_payload": dict(d.action_payload),
        "supporting_record_count": d.supporting_record_count,
        "status": DecisionStatus(d.status).value,
        "generated_at": d.generated_at,
        "resolved_at": d.resolved_at,
    }


def decision_from_item(item: Item) -> Decision:
    return Decision(
        id=item["id"],
        kind=DecisionKind(item["kind"]),
        finding_key=item["finding_key"],
        summary=item["summary"],
        recommended_action=item["recommended_action"],
        action_payload=dict(item.get("action_payload", {})),
        supporting_record_count=int(item.get("supporting_record_count", 0)),
        status=DecisionStatus(item["status"]),
        generated_at=item.get("generated_at", ""),
        resolved_at=item.get("resolved_at"),
    )


# ---------------------------------------------------------------------------
# CallSession  (PK=CALLSESSION, SK=<startedAt>#<id>)
# ---------------------------------------------------------------------------


def call_session_to_item(s: CallSession) -> Item:
    return {
        "PK": CALLSESSION_PK,
        "SK": f"{s.started_at}#{s.id}",
        "entity": "CallSession",
        "id": s.id,
        "started_at": s.started_at,
        "ended_at": s.ended_at,
        "outcome": None if s.outcome is None else CallOutcome(s.outcome).value,
        "patient_ref": _patient_ref_to_dict(s.patient_ref),
        "transcript": s.transcript,
        # Pointer to the audio in object storage; the audio itself would blow past
        # DynamoDB's 400 KB item limit (see the CallRecordingStore interface).
        "recording_uri": s.recording_uri,
    }


def call_session_from_item(item: Item) -> CallSession:
    outcome = item.get("outcome")
    return CallSession(
        id=item["id"],
        started_at=item["started_at"],
        ended_at=item.get("ended_at"),
        outcome=None if outcome is None else CallOutcome(outcome),
        patient_ref=_patient_ref_from_dict(item.get("patient_ref")),
        transcript=item.get("transcript"),
        recording_uri=item.get("recording_uri"),
    )


# ---------------------------------------------------------------------------
# Escalation  (PK=ESCALATION, SK=<createdAt>#<id>)
# ---------------------------------------------------------------------------


def escalation_to_item(e: Escalation) -> Item:
    return {
        "PK": ESCALATION_PK,
        "SK": f"{e.created_at}#{e.id}",
        "entity": "Escalation",
        "id": e.id,
        "reason": EscalationReason(e.reason).value,
        "call_session_id": e.call_session_id,
        "context": e.context,
        "created_at": e.created_at,
        "patient_ref": _patient_ref_to_dict(e.patient_ref),
    }


def escalation_from_item(item: Item) -> Escalation:
    return Escalation(
        id=item["id"],
        reason=EscalationReason(item["reason"]),
        call_session_id=item["call_session_id"],
        context=item["context"],
        created_at=item.get("created_at", ""),
        patient_ref=_patient_ref_from_dict(item.get("patient_ref")),
    )


__all__ = [
    "Item",
    "CLINIC_PK",
    "CONFIG_SK",
    "CALLSESSION_PK",
    "ESCALATION_PK",
    "clinic_kb_to_item",
    "clinic_kb_from_item",
    "provider_to_item",
    "provider_from_item",
    "slot_to_item",
    "slot_from_item",
    "appointment_to_item",
    "appointment_from_item",
    "patient_to_item",
    "patient_from_item",
    "waitlist_entry_to_item",
    "waitlist_entry_from_item",
    "decision_to_item",
    "decision_from_item",
    "call_session_to_item",
    "call_session_from_item",
    "escalation_to_item",
    "escalation_from_item",
]
