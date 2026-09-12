"""``ChangeEvent`` and the change-emitter hook (design "Dashboard", Req 16).

The Data_Layer emits a :class:`ChangeEvent` on **every successful mutation** so
the Dashboard BFF can fan changes out to connected clients over its real-time
channel (Req 9.6, 14.5, 14.8, 15.4). The event is intentionally tiny — it
carries only *what* changed (``entity``), *which* record (``id``), and *how*
(``kind``); consumers re-read through the appropriate store to get the current
value.

This module defines only the event shape and the emitter *hook interface*. The
concrete in-memory and DynamoDB stores (tasks 3.2 / 4.1) accept a
:class:`ChangeEmitter` and call :meth:`ChangeEmitter.emit` after — and only
after — a mutation has succeeded and left the store in its new, consistent
state. A failed write emits nothing (Req 16.6).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class ChangeEntity(StrEnum):
    """The kind of record a :class:`ChangeEvent` refers to.

    One member per persisted record type behind the Data_Layer, plus ``SLOT``
    because slot status transitions (open ↔ booked) are observable schedule
    changes even though slots live alongside appointments (Req 15.4).
    """

    APPOINTMENT = "appointment"
    SLOT = "slot"
    PATIENT = "patient"
    WAITLIST_ENTRY = "waitlist_entry"
    DECISION = "decision"
    CLINIC_KNOWLEDGE_BASE = "clinic_knowledge_base"
    CALL_SESSION = "call_session"
    ESCALATION = "escalation"


class ChangeKind(StrEnum):
    """How a record changed in a successful mutation.

    - ``CREATED`` — a new record was written (e.g. a booked appointment).
    - ``UPDATED`` — an existing record changed (e.g. a slot became ``booked``,
      a decision was resolved).
    - ``REMOVED`` — a record was deleted (e.g. a cancelled appointment, a
      filled waitlist entry).
    """

    CREATED = "created"
    UPDATED = "updated"
    REMOVED = "removed"


@dataclass(frozen=True)
class ChangeEvent:
    """A single successful-mutation notification (design ``ChangeEvent``).

    Attributes:
        entity: Which record type changed.
        id: The identifier of the changed record. For the singleton
            :class:`~clinic_front_desk.models.ClinicKnowledgeBase` this is the
            fixed config key.
        kind: Whether the record was created, updated, or removed.
    """

    entity: ChangeEntity
    id: str
    kind: ChangeKind


@runtime_checkable
class ChangeEmitter(Protocol):
    """The hook a store calls once a mutation has successfully committed.

    Contract:
        - :meth:`emit` is invoked **exactly once per successful mutation**,
          after the store's state reflects the change (Req 16.6). A write that
          fails must not emit.
        - Implementations must not raise for a well-formed event; delivery
          failures are the emitter's concern, never the caller's, so a store's
          success result is never turned into a failure by the emitter.
    """

    def emit(self, event: ChangeEvent) -> None:
        """Publish ``event`` to downstream consumers."""
        ...


class NullChangeEmitter:
    """A no-op :class:`ChangeEmitter` for contexts with no subscribers.

    Useful as a default so stores can always assume a non-optional emitter and
    for tests that do not assert on change propagation.
    """

    def emit(self, event: ChangeEvent) -> None:  # noqa: D102 - see protocol
        return None


__all__ = [
    "ChangeEntity",
    "ChangeKind",
    "ChangeEvent",
    "ChangeEmitter",
    "NullChangeEmitter",
]
