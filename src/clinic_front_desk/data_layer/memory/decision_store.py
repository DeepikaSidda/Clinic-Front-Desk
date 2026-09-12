"""In-memory :class:`DecisionStore` fake (task 3.2, Req 13, 14, 16).

Enforces the open-feed ordering invariant (Req 14.1): :meth:`list_open` returns
open decisions newest-first (most recently generated first). A store-managed
monotonic insertion counter breaks ties on equal ``generated_at`` so ordering is
stable and deterministic.
"""

from __future__ import annotations

from clinic_front_desk.data_layer.events import ChangeEmitter, ChangeEntity, ChangeKind
from clinic_front_desk.data_layer.interfaces import DecisionStore, NewDecision
from clinic_front_desk.models import (
    Decision,
    DecisionStatus,
    ISODateTime,
    Ok,
    StoreResult,
)

from ._support import MemoryStoreBase, not_found_err

_STORE = "MemoryDecisionStore"


class MemoryDecisionStore(DecisionStore, MemoryStoreBase):
    """A dict-backed :class:`DecisionStore` honouring the full store contract."""

    def __init__(self, emitter: ChangeEmitter | None = None) -> None:
        MemoryStoreBase.__init__(self, emitter)
        self._decisions: dict[str, Decision] = {}
        self._insertion: dict[str, int] = {}
        self._next_insertion: int = 0

    def create(self, d: NewDecision) -> StoreResult[Decision]:
        self._decisions[d.id] = self._copy(d)
        self._insertion[d.id] = self._next_insertion
        self._next_insertion += 1
        self._emit(ChangeEntity.DECISION, d.id, ChangeKind.CREATED)
        return Ok(self._copy(self._decisions[d.id]))

    def list_open(self) -> StoreResult[list[Decision]]:
        return self.list_by_status(DecisionStatus.OPEN)

    def list_by_status(self, status: DecisionStatus) -> StoreResult[list[Decision]]:
        matching = [d for d in self._decisions.values() if d.status == status]
        # Newest-first: descending generated_at, then descending insertion order
        # so equal timestamps still surface the most recently added first.
        matching.sort(
            key=lambda d: (d.generated_at, self._insertion[d.id]), reverse=True
        )
        return Ok([self._copy(d) for d in matching])

    def find_open_by_finding_key(self, key: str) -> StoreResult[Decision | None]:
        for d in self._decisions.values():
            if d.status == DecisionStatus.OPEN and d.finding_key == key:
                return Ok(self._copy(d))
        return Ok(None)

    def set_status(
        self, id: str, status: DecisionStatus, resolved_at: ISODateTime
    ) -> StoreResult[Decision]:
        decision = self._decisions.get(id)
        if decision is None:
            return not_found_err(_STORE, f"decision {id!r} not found")
        decision.status = status
        decision.resolved_at = resolved_at
        self._emit(ChangeEntity.DECISION, id, ChangeKind.UPDATED)
        return Ok(self._copy(decision))


__all__ = ["MemoryDecisionStore"]
