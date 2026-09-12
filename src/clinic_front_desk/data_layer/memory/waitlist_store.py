"""In-memory :class:`WaitlistStore` fake (task 3.2, Req 7, 8, 16).

Enforces the waitlist ordering invariant (Req 7.3): entries for a service are
returned ascending by ``added_at`` with the monotonic ``seq`` breaking ties, so
entries recorded at the same instant keep their original insertion order. The
store assigns its own strictly-increasing ``seq`` on every :meth:`add`, making
insertion order authoritative regardless of what the caller supplied.
"""

from __future__ import annotations

from dataclasses import replace

from clinic_front_desk.data_layer.events import ChangeEmitter, ChangeEntity, ChangeKind
from clinic_front_desk.data_layer.interfaces import NewWaitlistEntry, WaitlistStore
from clinic_front_desk.models import Ok, StoreResult, WaitlistEntry

from ._support import MemoryStoreBase, not_found_err

_STORE = "MemoryWaitlistStore"


class MemoryWaitlistStore(WaitlistStore, MemoryStoreBase):
    """A dict-backed :class:`WaitlistStore` honouring the full store contract."""

    def __init__(self, emitter: ChangeEmitter | None = None) -> None:
        MemoryStoreBase.__init__(self, emitter)
        self._entries: dict[str, WaitlistEntry] = {}
        self._next_seq: int = 0

    def add(self, e: NewWaitlistEntry) -> StoreResult[WaitlistEntry]:
        # Assign a store-managed monotonic seq so equal-``added_at`` entries keep
        # insertion order (Req 7.3), overriding any caller-supplied suggestion.
        seq = self._next_seq
        self._next_seq += 1
        stored = replace(self._copy(e), seq=seq)
        self._entries[stored.id] = stored
        self._emit(ChangeEntity.WAITLIST_ENTRY, stored.id, ChangeKind.CREATED)
        return Ok(self._copy(stored))

    def find_active(
        self, patient_id: str, service: str, slot_type: str
    ) -> StoreResult[WaitlistEntry | None]:
        for entry in self._entries.values():
            if (
                entry.active
                and entry.patient_id == patient_id
                and entry.service == service
                and entry.preferred_slot_type == slot_type
            ):
                return Ok(self._copy(entry))
        return Ok(None)

    def list_by_service_ordered(self, service: str) -> StoreResult[list[WaitlistEntry]]:
        matches = [
            self._copy(entry)
            for entry in self._entries.values()
            if entry.active and entry.service == service
        ]
        matches.sort(key=lambda entry: (entry.added_at, entry.seq))
        return Ok(matches)

    def remove(self, id: str) -> StoreResult[None]:
        if id not in self._entries:
            return not_found_err(_STORE, f"waitlist entry {id!r} not found")
        del self._entries[id]
        self._emit(ChangeEntity.WAITLIST_ENTRY, id, ChangeKind.REMOVED)
        return Ok(None)


__all__ = ["MemoryWaitlistStore"]
