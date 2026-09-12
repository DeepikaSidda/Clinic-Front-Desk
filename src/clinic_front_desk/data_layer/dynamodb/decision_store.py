"""DynamoDB :class:`DecisionStore` (task 4.1, Req 13, 14, 16).

Single-table layout: ``PK=DECISION#<status>``, ``SK=<generatedAt>#<id>``, with
GSI4 (``FINDINGKEY#<key>``) for dedupe. Because the status is part of the
partition key, a status transition moves the item between partitions — so
:meth:`set_status` writes the new-status item and deletes the old-status one.

Open-feed ordering (Req 14.1): :meth:`list_open` returns open decisions
newest-first (descending ``generated_at``, then descending ``id`` as a stable
tiebreak).
"""

from __future__ import annotations

from typing import Any

from clinic_front_desk.data_layer.events import ChangeEmitter, ChangeEntity, ChangeKind
from clinic_front_desk.data_layer.interfaces import DecisionStore, NewDecision
from clinic_front_desk.models import (
    Decision,
    DecisionStatus,
    Err,
    ISODateTime,
    Ok,
    StoreError,
    StoreErrorKind,
    StoreResult,
    decision_from_item,
    decision_to_item,
)

from ._support import GSI4, Attr, DynamoStoreBase, Key

_STORE = "DynamoDecisionStore"


class DynamoDecisionStore(DecisionStore, DynamoStoreBase):
    """Single-table :class:`DecisionStore` honouring the full store contract."""

    def __init__(self, table: Any, emitter: ChangeEmitter | None = None) -> None:
        DynamoStoreBase.__init__(self, table, emitter)

    def create(self, d: NewDecision) -> StoreResult[Decision]:
        item = decision_to_item(d)
        # Replace-by-id semantics (matching the in-memory store contract, Req
        # 16.5): a create for an id that already exists must not leave a stale
        # row behind. Because status is part of the partition key, a prior row
        # for this id can live in a different (resolved) partition; remove it
        # before writing the new one so the two backends stay observationally
        # equivalent.
        existing = self._find_by_entity_id("Decision", d.id)
        if existing is not None and (existing["PK"], existing["SK"]) != (
            item["PK"],
            item["SK"],
        ):
            self._delete(existing["PK"], existing["SK"])
        self._put(item)
        self._emit(ChangeEntity.DECISION, d.id, ChangeKind.CREATED)
        return Ok(decision_from_item(item))

    def list_open(self) -> StoreResult[list[Decision]]:
        return self.list_by_status(DecisionStatus.OPEN)

    def list_by_status(self, status: DecisionStatus) -> StoreResult[list[Decision]]:
        # Status is the partition key, so this is a plain Query on an existing
        # partition — no GSI and no table-schema change needed.
        items = self._query(Key("PK").eq(f"DECISION#{status.value}"))
        decisions = [decision_from_item(i) for i in items]
        # Newest-first: descending generated_at, then descending id so equal
        # timestamps surface the most recently generated first (Req 14.1).
        decisions.sort(key=lambda d: (d.generated_at, d.id), reverse=True)
        return Ok(decisions)

    def find_open_by_finding_key(self, key: str) -> StoreResult[Decision | None]:
        items = self._query(
            Key("GSI4PK").eq(f"FINDINGKEY#{key}"),
            index_name=GSI4,
            filter_expression=Attr("status").eq(DecisionStatus.OPEN.value),
        )
        if not items:
            return Ok(None)
        return Ok(decision_from_item(items[0]))

    def set_status(
        self, id: str, status: DecisionStatus, resolved_at: ISODateTime
    ) -> StoreResult[Decision]:
        item = self._find_by_entity_id("Decision", id)
        if item is None:
            return Err(
                StoreError(
                    kind=StoreErrorKind.NOT_FOUND,
                    detail=f"decision {id!r} not found",
                    store=_STORE,
                )
            )
        old_pk, old_sk = item["PK"], item["SK"]
        decision = decision_from_item(item)
        decision.status = status
        decision.resolved_at = resolved_at
        new_item = decision_to_item(decision)
        # The status change moves the item to a new partition; write the new
        # item first, then remove the stale one. The SK (generatedAt#id) is
        # unchanged, so the two never collide.
        self._put(new_item)
        if (new_item["PK"], new_item["SK"]) != (old_pk, old_sk):
            self._delete(old_pk, old_sk)
        self._emit(ChangeEntity.DECISION, id, ChangeKind.UPDATED)
        return Ok(decision)


__all__ = ["DynamoDecisionStore"]
