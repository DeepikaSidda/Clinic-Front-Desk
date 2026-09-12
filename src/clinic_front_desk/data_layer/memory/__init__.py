"""In-memory fake store implementations (task 3.2).

Dict-backed fakes satisfying the same contract as the DynamoDB implementations:

- **Provider-id enforcement (Req 16.3, 16.7)** on schedule-owning writes.
- **Atomic, all-or-nothing writes (Req 16.6)** that validate before mutating and
  leave prior records unchanged on failure.
- **Empty initialization (Req 16.4):** reads return empty until something is
  written.
- **Waitlist ordering (Req 7.3):** ascending ``added_at`` with a monotonic
  ``seq`` tiebreak.
- **Open-Decisions ordering (Req 14.1):** newest-first.
- **ChangeEvent emission on success only (Req 16.6),** through an injected
  :class:`~clinic_front_desk.data_layer.events.ChangeEmitter` that defaults to
  :class:`~clinic_front_desk.data_layer.events.NullChangeEmitter`.

These are the default backend for the property tests (tasks 3.4–3.10). The
fault-injection wrapper (task 3.3) composes over any of these fakes.
"""

from __future__ import annotations

from .appointment_store import MemoryAppointmentStore
from .call_recording_store import MemoryCallRecordingStore, memory_recording_uri
from .call_session_store import MemoryCallSessionStore
from .clinic_document_store import MemoryClinicDocumentStore, memory_document_uri
from .clinic_knowledge_base_store import MemoryClinicKnowledgeBaseStore
from .decision_store import MemoryDecisionStore
from .escalation_store import MemoryEscalationStore
from .patient_store import MemoryPatientStore
from .waitlist_store import MemoryWaitlistStore

__all__ = [
    "MemoryAppointmentStore",
    "MemoryPatientStore",
    "MemoryWaitlistStore",
    "MemoryDecisionStore",
    "MemoryClinicKnowledgeBaseStore",
    "MemoryCallSessionStore",
    "MemoryEscalationStore",
    "MemoryCallRecordingStore",
    "memory_recording_uri",
    "MemoryClinicDocumentStore",
    "memory_document_uri",
]
