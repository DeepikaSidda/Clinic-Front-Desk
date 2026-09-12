"""Shared helpers for the in-memory fake stores (task 3.2).

Every fake shares the same contract with its DynamoDB counterpart:

- **Empty initialization (Req 16.4).** A freshly constructed store holds no
  records, so reads return empty results until something is written.
- **Atomicity / non-destruction (Req 16.6).** Each write validates *before*
  mutating internal state, so a rejected write leaves every prior record
  exactly as it was. Records are stored and returned as deep copies, so a caller
  mutating a returned entity can never reach back into the store's state (and a
  half-built entity handed to a failed write is never retained).
- **Change emission on success only (Req 16.6).** A store emits a
  :class:`~clinic_front_desk.data_layer.events.ChangeEvent` through its injected
  :class:`~clinic_front_desk.data_layer.events.ChangeEmitter` *after* — and only
  after — a mutation has committed. A failed write emits nothing. The emitter
  defaults to :class:`~clinic_front_desk.data_layer.events.NullChangeEmitter`.
- **Provider-id enforcement (Req 16.3, 16.7).** Schedule-owning writes reject a
  missing/blank ``provider_id`` with a ``validation`` :class:`StoreError`.
"""

from __future__ import annotations

import copy
from typing import TypeVar

from clinic_front_desk.data_layer.events import (
    ChangeEmitter,
    ChangeEntity,
    ChangeEvent,
    ChangeKind,
    NullChangeEmitter,
)
from clinic_front_desk.models import Err, StoreError, StoreErrorKind

T = TypeVar("T")


class MemoryStoreBase:
    """Common state for the in-memory fakes: the change emitter and copy/emit
    helpers. Not a store on its own."""

    def __init__(self, emitter: ChangeEmitter | None = None) -> None:
        # Default to a no-op emitter so stores can always assume one is present
        # (Req 16.6). Tests may inject a recording emitter to assert propagation.
        self._emitter: ChangeEmitter = emitter or NullChangeEmitter()

    @staticmethod
    def _copy(value: T) -> T:
        """Return a deep copy so store state and caller state never alias."""
        return copy.deepcopy(value)

    def _emit(self, entity: ChangeEntity, id: str, kind: ChangeKind) -> None:
        """Publish a successful-mutation event (Req 16.6)."""
        self._emitter.emit(ChangeEvent(entity=entity, id=id, kind=kind))


def validation_err(store: str, field: str, detail: str) -> Err[StoreError]:
    """Build an ``Err`` for a rejected schedule-owning write (Req 16.7)."""
    return Err(
        StoreError(
            kind=StoreErrorKind.VALIDATION,
            detail=detail,
            store=store,
            field=field,
        )
    )


def not_found_err(store: str, detail: str) -> Err[StoreError]:
    """Build an ``Err`` for a read/write targeting a missing record."""
    return Err(StoreError(kind=StoreErrorKind.NOT_FOUND, detail=detail, store=store))


__all__ = ["MemoryStoreBase", "validation_err", "not_found_err"]
