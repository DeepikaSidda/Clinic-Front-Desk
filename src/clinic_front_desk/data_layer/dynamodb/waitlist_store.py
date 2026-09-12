"""DynamoDB :class:`WaitlistStore` (task 4.1, Req 7, 8, 16).

Single-table layout: ``PK=WAITLIST#<service>``, ``SK=<addedAt>#<seq>``.

Ordering (Req 7.3): entries for a service are returned ascending by ``added_at``
with the monotonic ``seq`` breaking ties. The store assigns its own
strictly-increasing ``seq`` on every :meth:`add` (starting at 0), exactly like
the in-memory fake, so insertion order is authoritative regardless of what the
caller supplied. Sorting is done in Python by ``(added_at, seq)`` numerically so
multi-digit ``seq`` values order correctly (a lexicographic sort of the SK would
not).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from clinic_front_desk.data_layer.events import ChangeEmitter, ChangeEntity, ChangeKind
from clinic_front_desk.data_layer.interfaces import NewWaitlistEntry, WaitlistStore
from clinic_front_desk.models import (
    Err,
    Ok,
    StoreError,
    StoreErrorKind,
    StoreResult,
    WaitlistEntry,
    waitlist_entry_from_item,
    waitlist_entry_to_item,
)

from ._support import Attr, DynamoStoreBase, Key

_STORE = "DynamoWaitlistStore"


class DynamoWaitlistStore(WaitlistStore, DynamoStoreBase):
    """Single-table :class:`WaitlistStore` honouring the full store contract."""

    def __init__(self, table: Any, emitter: ChangeEmitter | None = None) -> None:
        DynamoStoreBase.__init__(self, table, emitter)
        self._next_seq: int = 0

    def add(self, e: NewWaitlistEntry) -> StoreResult[WaitlistEntry]:
        # Assign a store-managed monotonic seq so equal-``added_at`` entries keep
        # insertion order (Req 7.3), overriding any caller-supplied suggestion.
        seq = self._next_seq
        self._next_seq += 1
        stored = replace(e, seq=seq)
        item = waitlist_entry_to_item(stored)
        self._put(item)
        self._emit(ChangeEntity.WAITLIST_ENTRY, stored.id, ChangeKind.CREATED)
        return Ok(waitlist_entry_from_item(item))

    def find_active(
        self, patient_id: str, service: str, slot_type: str
    ) -> StoreResult[WaitlistEntry | None]:
        items = self._query(
            Key("PK").eq(f"WAITLIST#{service}"),
            filter_expression=(
                Attr("active").eq(True)
                & Attr("patient_id").eq(patient_id)
                & Attr("preferred_slot_type").eq(slot_type)
            ),
        )
        entries = [waitlist_entry_from_item(i) for i in items]
        entries.sort(key=lambda entry: (entry.added_at, entry.seq))
        if not entries:
            return Ok(None)
        return Ok(entries[0])

    def list_by_service_ordered(self, service: str) -> StoreResult[list[WaitlistEntry]]:
        items = self._query(
            Key("PK").eq(f"WAITLIST#{service}"),
            filter_expression=Attr("active").eq(True),
        )
        entries = [waitlist_entry_from_item(i) for i in items]
        entries.sort(key=lambda entry: (entry.added_at, entry.seq))
        return Ok(entries)

    def remove(self, id: str) -> StoreResult[None]:
        item = self._find_by_entity_id("WaitlistEntry", id)
        if item is None:
            return Err(
                StoreError(
                    kind=StoreErrorKind.NOT_FOUND,
                    detail=f"waitlist entry {id!r} not found",
                    store=_STORE,
                )
            )
        self._delete(item["PK"], item["SK"])
        self._emit(ChangeEntity.WAITLIST_ENTRY, id, ChangeKind.REMOVED)
        return Ok(None)


__all__ = ["DynamoWaitlistStore"]
