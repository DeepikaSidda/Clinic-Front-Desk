"""Domain data models and shared ``Result``/error types.

This package defines the vocabulary the rest of the system is built on:

- :mod:`~clinic_front_desk.models.entities` — the persisted domain models
  (``Provider``, ``Slot``, ``Patient``, ``Appointment``, ``WaitlistEntry``,
  ``Decision``, ``CallSession``, ``Escalation``, ``ClinicKnowledgeBase``, …) and
  the analysis-only ``Finding``.
- :mod:`~clinic_front_desk.models.enums` — the status/kind enumerations.
- :mod:`~clinic_front_desk.models.result` — the shared ``Result[T]`` success/
  failure union plus ``StoreError`` and the ``ToolError`` discriminated types
  used across the store and tool boundaries.
- :mod:`~clinic_front_desk.models.dynamo` — DynamoDB single-table
  (de)serialization helpers.

All names are re-exported here so callers can ``from clinic_front_desk.models
import Appointment, Ok, Err, ToolError`` without knowing the submodule layout.
"""

from __future__ import annotations

from .dynamo import (
    CALLSESSION_PK,
    CLINIC_PK,
    CONFIG_SK,
    ESCALATION_PK,
    Item,
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
    patient_from_item,
    patient_to_item,
    provider_from_item,
    provider_to_item,
    slot_from_item,
    slot_to_item,
    waitlist_entry_from_item,
    waitlist_entry_to_item,
)
from .matching import (
    CODE_PHONE_DIGITS,
    NATIONAL_NUMBER_DIGITS,
    normalize_patient_code,
    normalize_person_name,
    normalize_phone,
    patient_code,
    patient_lookup_key,
)
from .entities import (
    MONEY_MAX,
    MONEY_MIN,
    Appointment,
    CallSession,
    ClinicKnowledgeBase,
    DayHours,
    Decision,
    Escalation,
    Finding,
    ISODate,
    ISODateTime,
    ClinicDocument,
    DocumentChunk,
    Money,
    Patient,
    PatientRef,
    Provider,
    RecordingRef,
    RetrievedChunk,
    ScheduleRule,
    ServiceConfig,
    Slot,
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
from .result import (
    Ambiguous,
    Duplicate,
    Err,
    NotFound,
    NotOffered,
    Ok,
    Result,
    StoreError,
    StoreErrorKind,
    StoreFailure,
    StoreResult,
    ToolError,
    ToolResult,
    Validation,
    is_err,
    is_ok,
)

__all__ = [
    # record matching
    "CODE_PHONE_DIGITS",
    "NATIONAL_NUMBER_DIGITS",
    "normalize_patient_code",
    "normalize_person_name",
    "normalize_phone",
    "patient_code",
    "patient_lookup_key",
    # result / errors
    "Ok",
    "Err",
    "Result",
    "StoreResult",
    "ToolResult",
    "is_ok",
    "is_err",
    "StoreError",
    "StoreErrorKind",
    "ToolError",
    "StoreFailure",
    "NotFound",
    "Ambiguous",
    "Validation",
    "NotOffered",
    "Duplicate",
    # enums
    "SlotStatus",
    "AppointmentStatus",
    "DecisionKind",
    "DecisionStatus",
    "CallOutcome",
    "EscalationReason",
    # entities
    "ISODate",
    "ISODateTime",
    "Money",
    "MONEY_MIN",
    "MONEY_MAX",
    "ScheduleRule",
    "Provider",
    "ServiceConfig",
    "DayHours",
    "ClinicKnowledgeBase",
    "Slot",
    "Patient",
    "Appointment",
    "WaitlistEntry",
    "Decision",
    "PatientRef",
    "RecordingRef",
    "ClinicDocument",
    "DocumentChunk",
    "RetrievedChunk",
    "CallSession",
    "Escalation",
    "Finding",
    # dynamo (de)serialization
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
