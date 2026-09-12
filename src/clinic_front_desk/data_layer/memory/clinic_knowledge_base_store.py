"""In-memory :class:`ClinicKnowledgeBaseStore` fake (task 3.2, Req 1, 16).

The knowledge base is a singleton. :meth:`save` is atomic (Req 1.6): it either
persists the entire configuration or, on rejection, leaves the previously stored
configuration completely unchanged — never a partial update. Because the config
carries the provider roster and their schedules, a provider missing an ``id`` is
rejected as a schedule-owning provider-id violation (Req 16.7).
"""

from __future__ import annotations

from clinic_front_desk.data_layer.events import ChangeEmitter, ChangeEntity, ChangeKind
from clinic_front_desk.data_layer.interfaces import ClinicKnowledgeBaseStore
from clinic_front_desk.models import CONFIG_SK, ClinicKnowledgeBase, Ok, StoreResult

from ._support import MemoryStoreBase, validation_err

_STORE = "MemoryClinicKnowledgeBaseStore"


class MemoryClinicKnowledgeBaseStore(ClinicKnowledgeBaseStore, MemoryStoreBase):
    """A single-cell fake for the singleton clinic configuration."""

    def __init__(self, emitter: ChangeEmitter | None = None) -> None:
        MemoryStoreBase.__init__(self, emitter)
        self._kb: ClinicKnowledgeBase | None = None

    def get(self) -> StoreResult[ClinicKnowledgeBase | None]:
        return Ok(self._copy(self._kb) if self._kb is not None else None)

    def save(self, kb: ClinicKnowledgeBase) -> StoreResult[ClinicKnowledgeBase]:
        # Provider-id enforcement (Req 16.7): validate the whole roster before
        # touching stored state so a rejected save is fully non-destructive.
        for provider in kb.providers:
            if not provider.id:
                return validation_err(
                    _STORE, "provider_id", "every configured provider requires an id"
                )
        # Atomic replace: the entire config is swapped in one assignment (Req 1.6).
        self._kb = self._copy(kb)
        self._emit(ChangeEntity.CLINIC_KNOWLEDGE_BASE, CONFIG_SK, ChangeKind.UPDATED)
        return Ok(self._copy(self._kb))


__all__ = ["MemoryClinicKnowledgeBaseStore"]
