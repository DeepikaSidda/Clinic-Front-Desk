"""DynamoDB :class:`EscalationStore` (task 4.1, Req 9, 15, 16).

Single-table layout: ``PK=ESCALATION``, ``SK=<createdAt>#<id>``. All escalations
share one partition, so :meth:`list_recent` queries that partition and returns
escalations most-recent-first (descending ``created_at``, then descending ``id``
as a stable tiebreak), capped at ``limit`` (Req 15.2, 9.6).
"""

from __future__ import annotations

from typing import Any

from clinic_front_desk.data_layer.events import ChangeEmitter, ChangeEntity, ChangeKind
from clinic_front_desk.data_layer.interfaces import EscalationStore, NewEscalation
from clinic_front_desk.models import (
    ESCALATION_PK,
    Escalation,
    Ok,
    StoreResult,
    escalation_from_item,
    escalation_to_item,
)

from ._support import DynamoStoreBase, Key

_STORE = "DynamoEscalationStore"


class DynamoEscalationStore(EscalationStore, DynamoStoreBase):
    """Single-table :class:`EscalationStore` honouring the full store contract."""

    def __init__(self, table: Any, emitter: ChangeEmitter | None = None) -> None:
        DynamoStoreBase.__init__(self, table, emitter)

    def create(self, e: NewEscalation) -> StoreResult[Escalation]:
        item = escalation_to_item(e)
        self._put(item)
        self._emit(ChangeEntity.ESCALATION, e.id, ChangeKind.CREATED)
        return Ok(escalation_from_item(item))

    def list_recent(self, limit: int) -> StoreResult[list[Escalation]]:
        items = self._query(
            Key("PK").eq(ESCALATION_PK),
            scan_index_forward=False,
        )
        escalations = [escalation_from_item(i) for i in items]
        escalations.sort(key=lambda e: (e.created_at, e.id), reverse=True)
        capped = escalations[:limit] if limit >= 0 else escalations
        return Ok(capped)


__all__ = ["DynamoEscalationStore"]
