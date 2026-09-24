"""Domain data models (design "Data Models").

Dataclasses for every persisted entity plus the analysis-only :class:`Finding`.
Conventions from the design:

- All timestamps are ISO-8601 UTC strings (``ISODateTime``); calendar dates are
  ISO date strings (``ISODate``).
- ``Money`` is a bounded float (0.01 – 999,999.99, Req 1.2).
- Every schedule-owning entity (:class:`Slot`, :class:`Appointment`) carries a
  required, non-empty ``provider_id`` (Req 16.3, 16.7); construction rejects a
  blank one so the invariant holds before a value ever reaches a store.

Field names use Python's snake_case (e.g. ``provider_id``) rather than the
design's TypeScript camelCase (``providerId``); the mapping is intentional and
consistent, and the DynamoDB (de)serialization helpers in
:mod:`clinic_front_desk.models.dynamo` bridge to the wire shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .enums import (
    AppointmentStatus,
    CallOutcome,
    DecisionKind,
    DecisionStatus,
    EscalationReason,
    SlotStatus,
)

# Semantic aliases for readability; these are plain strings on the wire.
ISODate = str
ISODateTime = str
Money = float

# Money bounds (Req 1.2), exported for validators/tests.
MONEY_MIN: Money = 0.01
MONEY_MAX: Money = 999_999.99


@dataclass
class ScheduleRule:
    """A provider's bookable window on one weekday (design ``ScheduleRule``)."""

    day_of_week: int  # 0 (Sunday) .. 6 (Saturday)
    start: str  # "09:00"
    end: str  # "17:00"


@dataclass
class Provider:
    """A clinician who owns a schedule (design ``Provider``, Req 1.3)."""

    id: str
    name: str  # 1–100 chars (Req 1.3)
    specialty: str
    schedule: list[ScheduleRule] = field(default_factory=list)


@dataclass
class ServiceConfig:
    """A single offered service and its optional prep/pricing (design ``ServiceConfig``)."""

    name: str
    prep_instructions: str | None = None  # up to 2000 chars (Req 1.2)
    price: Money | None = None  # 0.01–999,999.99 (Req 1.2)


@dataclass
class DayHours:
    """Opening and closing time for a single weekday."""

    open: str  # "09:00"
    close: str  # "17:00"


@dataclass
class SymptomRoute:
    """The doctor's own rule for what a described problem should be booked as.

    This is the mechanism that lets a caller say "there's an itch inside my nose"
    and be booked correctly, **without the model deciding what the itch means**.

    The distinction is the whole design. A model inferring a service from a symptom
    is making a clinical judgement, unsupervised, on a recorded line, to someone who
    will act on it. A doctor writing "itching, sneezing, blocked nose -> ENT
    Consultation" is making that same judgement once, deliberately, in a place she
    can review and correct. The caller's experience is identical; only the author
    changes. Anything the doctor has not written a rule for still escalates to a
    human rather than being guessed at.

    Attributes:
        phrases: Words a caller might use, in their own language. Matched as whole
            words against what the caller said, so "ear" does not fire on "hearing".
        service: The offered service to book. Must be one of the configured
            services, or the route is ignored — a rule pointing at a service the
            clinic does not offer would strand the caller.
        advice: The doctor's own wording, spoken to the caller as-is. Optional; the
            agent says nothing extra when it is empty.
        urgent: When true the agent must NOT offer a routine slot. Some things need
            to be seen today, and quietly booking next Tuesday for sudden hearing
            loss is the most damaging thing this system could do.
        urgent_instruction: What to tell the caller instead, in the doctor's words.
    """

    phrases: list[str] = field(default_factory=list)
    service: str = ""
    advice: str = ""
    urgent: bool = False
    urgent_instruction: str = ""


@dataclass
class ClinicKnowledgeBase:
    """Onboarded clinic configuration (design ``ClinicKnowledgeBase``, Req 1)."""

    location: str
    # Weekday index (0–6) -> hours for that day, or None when closed.
    hours: dict[int, DayHours | None] = field(default_factory=dict)
    services: list[ServiceConfig] = field(default_factory=list)  # 1–100 (Req 1.2)
    accepted_insurance: list[str] = field(default_factory=list)
    #: The clinic's own number, for callers the agent cannot finish helping.
    #:
    #: "Ring the clinic during opening hours" is not an instruction anyone can follow
    #: without it. Empty by default, and every place that speaks it degrades to the
    #: wording used before this existed rather than reading out a blank.
    contact_phone: str = ""
    providers: list[Provider] = field(default_factory=list)  # 1–50 (Req 1.3)
    #: The doctor's symptom-to-service routing. Empty by default, which restores the
    #: original behaviour exactly: a described symptom matches nothing and escalates.
    symptom_routes: list[SymptomRoute] = field(default_factory=list)
    configured: bool = False  # False until required fields present (Req 1.7)
    updated_at: ISODateTime = ""


@dataclass
class Slot:
    """A discrete bookable interval on a provider's calendar (design ``Slot``)."""

    id: str
    provider_id: str  # required (Req 16.3, 16.7)
    service: str
    start: ISODateTime
    end: ISODateTime
    status: SlotStatus = SlotStatus.OPEN

    def __post_init__(self) -> None:
        if not self.provider_id:
            raise ValueError("Slot.provider_id is required (Req 16.7)")


@dataclass
class Patient:
    """A caller record (design ``Patient``).

    Intake fields
    -------------
    ``name`` and ``callback_phone`` identify the record and are required: without
    them the clinic cannot tell two patients apart or call anyone back.

    Everything below them is **optional, and deliberately so**. ``age``,
    ``blood_group``, ``weight_kg`` and ``height_cm`` are health information about an
    identifiable person, not administrative detail, and a caller is entitled to
    decline any of them. Requiring one would mean refusing an appointment to
    someone who would not state their blood group over the phone, which trades a
    real booking for a form field. They are recorded when offered and left unset
    when not.
    """

    id: str
    name: str
    callback_phone: str
    extra_identifiers: dict[str, str] | None = None  # disambiguation (Req 3.6)
    created_at: ISODateTime = ""
    #: A short code the patient can say back — see
    #: :func:`~clinic_front_desk.models.matching.patient_code`. Stored rather than
    #: derived on read, so correcting a misheard name does not silently hand the
    #: patient a different code from the one they were given on the call.
    code: str = ""
    #: Optional intake details. Health information — see the class docstring.
    age: int | None = None
    blood_group: str | None = None
    weight_kg: float | None = None
    height_cm: float | None = None


@dataclass
class Appointment:
    """A scheduled visit (design ``Appointment``)."""

    id: str
    provider_id: str  # required (Req 16.3, 16.7)
    patient_id: str
    service: str
    slot_id: str
    date: ISODate
    time: str
    status: AppointmentStatus = AppointmentStatus.BOOKED
    created_at: ISODateTime = ""
    updated_at: ISODateTime = ""

    def __post_init__(self) -> None:
        if not self.provider_id:
            raise ValueError("Appointment.provider_id is required (Req 16.7)")


@dataclass
class WaitlistEntry:
    """An ordered waitlist placement (design ``WaitlistEntry``, Req 7.3)."""

    id: str
    patient_id: str
    service: str
    preferred_slot_type: str
    added_at: ISODateTime
    seq: int  # monotonic tiebreaker for equal added_at (Req 7.3)
    active: bool = True


@dataclass
class Decision:
    """A Practice_Intelligence recommendation (design ``Decision``, Req 13/14)."""

    id: str
    kind: DecisionKind
    finding_key: str  # stable dedupe key (Req 13.3)
    summary: str
    recommended_action: str
    action_payload: dict[str, Any] = field(default_factory=dict)
    supporting_record_count: int = 0  # ≥ 5 to exist (Req 13.6)
    status: DecisionStatus = DecisionStatus.OPEN
    generated_at: ISODateTime = ""
    resolved_at: ISODateTime | None = None


@dataclass
class PatientRef:
    """A partial reference to a patient captured during a call (design ``PatientRef``)."""

    patient_id: str | None = None
    name: str | None = None
    callback_phone: str | None = None


@dataclass(frozen=True)
class DocumentChunk:
    """One retrievable passage of an uploaded clinic document.

    Chunks are the unit of retrieval: a caller's question is matched against these
    rather than against whole documents, so an answer can cite the passage it came
    from. ``page`` is retained where the source format has pages, because "it says
    so on page 2 of the practice information sheet" is what makes an answer
    auditable by the doctor.
    """

    document_id: str
    index: int
    text: str
    page: int | None = None
    #: Embedding vector. Empty until embedded, which is a separate step because it
    #: costs a Bedrock call per chunk.
    embedding: tuple[float, ...] = ()


@dataclass(frozen=True)
class ClinicDocument:
    """An uploaded clinic information document (design "Clinic_Knowledge_Base").

    The *descriptive* half of clinic knowledge — detailed directions, parking,
    holiday closures, accessibility, policies — which does not fit the structured
    :class:`ClinicKnowledgeBase` fields and which the six fixed FAQ topics cannot
    answer. Stored as the original file plus its retrievable chunks.
    """

    id: str
    filename: str
    content_type: str
    uploaded_at: ISODateTime
    byte_size: int
    chunk_count: int = 0
    page_count: int | None = None
    #: Where the original upload is stored (``s3://…`` or ``memory://…``).
    uri: str | None = None
    #: Set when text could not be extracted, so the doctor is told the upload is
    #: unusable instead of it silently contributing nothing.
    extraction_error: str | None = None


@dataclass(frozen=True)
class RetrievedChunk:
    """A chunk matched to a question, with its similarity score."""

    chunk: DocumentChunk
    score: float


@dataclass(frozen=True)
class RecordingRef:
    """Where a Call_Session's audio recording was stored.

    Returned by
    :meth:`~clinic_front_desk.data_layer.interfaces.CallRecordingStore.put`. The
    :attr:`uri` is what gets written onto the ``CallSession``; the rest is
    metadata useful for display and for verifying an upload without fetching the
    bytes back.
    """

    call_session_id: str
    uri: str
    byte_size: int
    content_type: str = "audio/wav"
    duration_seconds: float | None = None


@dataclass
class CallSession:
    """A single continuous voice interaction (design ``CallSession``)."""

    id: str
    started_at: ISODateTime
    ended_at: ISODateTime | None = None
    outcome: CallOutcome | None = None  # Req 11.5, 12.7
    patient_ref: PatientRef | None = None
    #: Turn-by-turn transcript of the call, rendered on end.
    transcript: str | None = None
    #: Where the call's audio recording is stored (an ``s3://bucket/key`` URI, or
    #: ``memory://`` for the in-memory fake). ``None`` when the call was not
    #: recorded — recording is off unless a bucket is configured.
    recording_uri: str | None = None


@dataclass
class Escalation:
    """A recorded routing of a request to a human (design ``Escalation``, Req 9.4)."""

    id: str
    reason: EscalationReason
    call_session_id: str
    context: str
    created_at: ISODateTime = ""
    patient_ref: PatientRef | None = None


@dataclass
class Finding:
    """An analysis observation, pre-Decision (design ``Finding``, Req 13)."""

    key: str  # stable dedupe key
    kind: DecisionKind
    summary: str
    recommended_action: str
    action_payload: dict[str, Any] = field(default_factory=dict)
    supporting_record_count: int = 0
    actionable: bool = False  # Req 13.4


__all__ = [
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
    "CallSession",
    "Escalation",
    "Finding",
]
