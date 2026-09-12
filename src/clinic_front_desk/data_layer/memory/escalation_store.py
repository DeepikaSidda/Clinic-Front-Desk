"""In-memory :class:`EscalationStore` fake (task 3.2, Req 9, 15, 16).

:meth:`list_recent` returns escalations most-recent-first (Req 15.2, 9.6); a
store-managed insertion counter breaks ties on equal ``created_at``.
"""

from __future__ import annotations

from clinic_front_desk.data_layer.events import ChangeEmitter, ChangeEntity, ChangeKind
from clinic_front_desk.data_layer.interfaces import EscalationStore, NewEscalation
from clinic_front_desk.models import Escalation, Ok, StoreResult

from ._support import MemoryStoreBase

_STORE = "MemoryEscalationStore"


class MemoryEscalationStore(EscalationStore, MemoryStoreBase):
    """A dict-backed :class:`EscalationStore` honouring the full store contract."""

    def __init__(self, emitter: ChangeEmitter | None = None) -> None:
        MemoryStoreBase.__init__(self, emitter)
        self._escalations: dict[str, Escalation] = {}
        self._insertion: dict[str, int] = {}
        self._next_insertion: int = 0

    def create(self, e: NewEscalation) -> StoreResult[Escalation]:
        self._escalations[e.id] = self._copy(e)
        self._insertion[e.id] = self._next_insertion
        self._next_insertion += 1
        self._emit(ChangeEntity.ESCALATION, e.id, ChangeKind.CREATED)
        return Ok(self._copy(self._escalations[e.id]))

    def list_recent(self, limit: int) -> StoreResult[list[Escalation]]:
        ordered = sorted(
            self._escalations.values(),
            key=lambda e: (e.created_at, self._insertion[e.id]),
            reverse=True,
        )
        capped = ordered[:limit] if limit >= 0 else ordered
        return Ok([self._copy(e) for e in capped])


__all__ = ["MemoryEscalationStore"]
