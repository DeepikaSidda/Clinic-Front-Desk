"""The abstract Data_Layer store interfaces and the ``Result`` contract.

Seven interfaces cover the DynamoDB single-table record types; an eighth,
:class:`~clinic_front_desk.data_layer.interfaces.CallRecordingStore`, covers call
audio, which is far too large for a DynamoDB item and so lives in object storage
with a ``recording_uri`` pointer on the ``CallSession``.

One narrow interface per persisted record type (Req 16.1); agents and tools
depend only on these abstractions, never on storage directly. Every write
returns a :data:`~clinic_front_desk.models.StoreResult` and leaves prior records
unchanged on failure (Req 16.6); any Appointment/Slot/schedule write without a
``provider_id`` is rejected (Req 16.7). Concrete implementations emit a
:class:`~clinic_front_desk.data_layer.events.ChangeEvent` on every successful
mutation.

The shared ``Result`` contract (``Ok``/``Err``/``StoreError``) is re-exported
here for convenience so callers can import the interfaces and the result types
from one place.
"""

from __future__ import annotations

from clinic_front_desk.models import (
    Err,
    Ok,
    Result,
    StoreError,
    StoreErrorKind,
    StoreResult,
    is_err,
    is_ok,
)

from .appointment_store import AppointmentStore
from .call_recording_store import (
    DEFAULT_PLAYBACK_URL_TTL_SECONDS,
    CallRecordingStore,
)
from .call_session_store import CallSessionStore
from .clinic_document_store import ClinicDocumentStore
from .clinic_knowledge_base_store import ClinicKnowledgeBaseStore
from .decision_store import DecisionStore
from .escalation_store import EscalationStore
from .inputs import (
    OpenSlotSpan,
    NewAppointment,
    NewCallSession,
    NewDecision,
    NewEscalation,
    NewPatient,
    NewWaitlistEntry,
    SlotRelease,
)
from .patient_store import PatientStore
from .waitlist_store import WaitlistStore

__all__ = [
    # store interfaces
    "AppointmentStore",
    "PatientStore",
    "WaitlistStore",
    "DecisionStore",
    "ClinicKnowledgeBaseStore",
    "CallSessionStore",
    "EscalationStore",
    "CallRecordingStore",
    "ClinicDocumentStore",
    "DEFAULT_PLAYBACK_URL_TTL_SECONDS",
    # create-input aliases and result shapes
    "NewAppointment",
    "NewPatient",
    "NewWaitlistEntry",
    "NewDecision",
    "NewCallSession",
    "NewEscalation",
    "OpenSlotSpan",
    "SlotRelease",
    # result contract (re-exported from models)
    "Ok",
    "Err",
    "Result",
    "StoreResult",
    "StoreError",
    "StoreErrorKind",
    "is_ok",
    "is_err",
]
