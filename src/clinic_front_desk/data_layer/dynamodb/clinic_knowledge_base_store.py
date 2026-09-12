"""DynamoDB :class:`ClinicKnowledgeBaseStore` (task 4.1, Req 1, 16).

The knowledge base is a singleton stored as **one** item
(``PK=CLINIC#config``, ``SK=CONFIG``) with the provider roster, hours, services
and insurance embedded (see :func:`clinic_kb_to_item`). Storing the whole
configuration in a single item makes :meth:`save` inherently atomic: a single
``put_item`` either replaces the entire configuration or, on failure, leaves the
previously stored configuration completely unchanged — never a partial update
(Req 1.6, 16.6). Because the config carries the provider roster and their
schedules, a provider missing an ``id`` is rejected as a schedule-owning
provider-id violation (Req 16.7) *before* any write.
"""

from __future__ import annotations

from typing import Any

from clinic_front_desk.data_layer.events import ChangeEmitter, ChangeEntity, ChangeKind
from clinic_front_desk.data_layer.interfaces import ClinicKnowledgeBaseStore
from clinic_front_desk.models import (
    CLINIC_PK,
    CONFIG_SK,
    ClinicKnowledgeBase,
    Err,
    Ok,
    StoreError,
    StoreErrorKind,
    StoreResult,
    clinic_kb_from_item,
    clinic_kb_to_item,
)

from ._support import DynamoStoreBase

_STORE = "DynamoClinicKnowledgeBaseStore"


class DynamoClinicKnowledgeBaseStore(ClinicKnowledgeBaseStore, DynamoStoreBase):
    """Single-cell DynamoDB store for the singleton clinic configuration."""

    def __init__(self, table: Any, emitter: ChangeEmitter | None = None) -> None:
        DynamoStoreBase.__init__(self, table, emitter)

    def get(self) -> StoreResult[ClinicKnowledgeBase | None]:
        item = self._get(CLINIC_PK, CONFIG_SK)
        return Ok(clinic_kb_from_item(item) if item is not None else None)

    def save(self, kb: ClinicKnowledgeBase) -> StoreResult[ClinicKnowledgeBase]:
        # Provider-id enforcement (Req 16.7): validate the whole roster before
        # touching stored state so a rejected save is fully non-destructive.
        for provider in kb.providers:
            if not provider.id:
                return Err(
                    StoreError(
                        kind=StoreErrorKind.VALIDATION,
                        detail="every configured provider requires an id",
                        store=_STORE,
                        field="provider_id",
                    )
                )
        # Atomic replace: the entire config is one item, swapped in a single
        # put_item (Req 1.6). No partial update is possible.
        item = clinic_kb_to_item(kb)
        self._put(item)
        self._emit(ChangeEntity.CLINIC_KNOWLEDGE_BASE, CONFIG_SK, ChangeKind.UPDATED)
        return Ok(clinic_kb_from_item(item))


__all__ = ["DynamoClinicKnowledgeBaseStore"]
