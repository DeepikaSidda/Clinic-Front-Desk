"""Change-event types and the change-emitter hook for the Data_Layer.

Re-exports :class:`ChangeEvent`, its ``entity``/``kind`` enumerations, and the
:class:`ChangeEmitter` hook interface invoked on every successful mutation
(design "Dashboard" real-time section).
"""

from __future__ import annotations

from .change_event import (
    ChangeEmitter,
    ChangeEntity,
    ChangeEvent,
    ChangeKind,
    NullChangeEmitter,
)

__all__ = [
    "ChangeEntity",
    "ChangeKind",
    "ChangeEvent",
    "ChangeEmitter",
    "NullChangeEmitter",
]
