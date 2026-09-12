"""DynamoDB single-table store implementations (task 4.1).

Concrete implementations of the seven Data_Layer store interfaces over the
``PK``/``SK`` + GSI1-GSI4 single-table layout from the design's "DynamoDB Table
Design". They return success ``Result``s on write, emit ``ChangeEvent``s on
success only, enforce provider-id presence on schedule-owning writes, and save
the clinic config atomically (single item, no partial update). The single-table
layout is hidden behind the interfaces (Req 16.5), so swapping storage swaps
only the implementation.

The numeric Decimal boundary the boto3 resource API requires is handled inside
:mod:`._support` (``to_dynamo`` / ``from_dynamo``): the
:mod:`clinic_front_desk.models.dynamo` helpers stay storage-representation
agnostic (plain types), and numbers are converted to ``Decimal`` for DynamoDB
and back on read.

Use :func:`create_table` to bootstrap the table (PK/SK + GSI1-GSI4) against
DynamoDB-local or moto, and :func:`create_stores` to build all seven stores over
one table sharing a single change emitter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from clinic_front_desk.data_layer.events import ChangeEmitter

from ._support import create_table, from_dynamo, table_exists, to_dynamo
from .appointment_store import DynamoAppointmentStore
from .call_session_store import DynamoCallSessionStore
from .clinic_knowledge_base_store import DynamoClinicKnowledgeBaseStore
from .decision_store import DynamoDecisionStore
from .escalation_store import DynamoEscalationStore
from .patient_store import DynamoPatientStore
from .waitlist_store import DynamoWaitlistStore


@dataclass(frozen=True)
class DynamoStores:
    """A bundle of all seven DynamoDB stores sharing one table and emitter."""

    appointments: DynamoAppointmentStore
    patients: DynamoPatientStore
    waitlist: DynamoWaitlistStore
    decisions: DynamoDecisionStore
    clinic_knowledge_base: DynamoClinicKnowledgeBaseStore
    call_sessions: DynamoCallSessionStore
    escalations: DynamoEscalationStore


def create_stores(table: Any, emitter: ChangeEmitter | None = None) -> DynamoStores:
    """Build all seven DynamoDB stores over ``table`` sharing ``emitter``.

    Args:
        table: A boto3 DynamoDB ``Table`` resource (e.g. from :func:`create_table`).
        emitter: The change emitter every store publishes successful mutations
            to; defaults to a no-op emitter inside each store.

    Returns:
        A :class:`DynamoStores` bundle.
    """
    return DynamoStores(
        appointments=DynamoAppointmentStore(table, emitter),
        patients=DynamoPatientStore(table, emitter),
        waitlist=DynamoWaitlistStore(table, emitter),
        decisions=DynamoDecisionStore(table, emitter),
        clinic_knowledge_base=DynamoClinicKnowledgeBaseStore(table, emitter),
        call_sessions=DynamoCallSessionStore(table, emitter),
        escalations=DynamoEscalationStore(table, emitter),
    )


__all__ = [
    # stores
    "DynamoAppointmentStore",
    "DynamoPatientStore",
    "DynamoWaitlistStore",
    "DynamoDecisionStore",
    "DynamoClinicKnowledgeBaseStore",
    "DynamoCallSessionStore",
    "DynamoEscalationStore",
    # bootstrap + factory
    "create_table",
    "table_exists",
    "create_stores",
    "DynamoStores",
    # decimal boundary (exposed for tests)
    "to_dynamo",
    "from_dynamo",
]
