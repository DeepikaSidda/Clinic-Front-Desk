"""Create-input type aliases and small store-result shapes.

The design's store signatures accept ``NewAppointment``, ``NewPatient``, and
peers as the *input* shapes for ``create``. Rather than duplicate the entity
definitions, we alias each ``New*`` name to its fully-typed entity dataclass in
:mod:`clinic_front_desk.models`. A concrete store treats server-managed fields
on these inputs (``id`` and ``created_at``/``updated_at`` timestamps, and the
waitlist ``seq`` tiebreaker) as *suggestions*: it may accept a caller-supplied
value or assign its own, but the returned entity always carries the persisted,
authoritative values.

Keeping these as aliases (not new dataclasses) means the interfaces reuse the
single source of truth for entity fields and stay mypy-friendly without any
conversion layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from clinic_front_desk.models import (
    Appointment,
    CallSession,
    Decision,
    Escalation,
    Patient,
    WaitlistEntry,
)

# Create-input shapes (design "Data_Layer Interfaces").
NewAppointment: TypeAlias = Appointment
NewPatient: TypeAlias = Patient
NewWaitlistEntry: TypeAlias = WaitlistEntry
NewDecision: TypeAlias = Decision
NewCallSession: TypeAlias = CallSession
NewEscalation: TypeAlias = Escalation


@dataclass(frozen=True)
class SlotRelease:
    """Result of removing an appointment: the slot returned to ``open``.

    Mirrors the design's ``Result<{ releasedSlotId: string }>`` for
    :meth:`AppointmentStore.remove` (Req 5.7).
    """

    released_slot_id: str


@dataclass(frozen=True)
class OpenSlotSpan:
    """How far a provider's bookable calendar runs, and how much of it is open.

    Answers "does this clinic have time published around then" without reading
    the slots themselves. That question is asked once per call, up front, so the
    agent knows whether a date the caller names is inside the published calendar
    before it has to say anything — rather than guessing while a tool call is
    still in flight.
    """

    #: Start of the earliest open slot, ISO ``YYYY-MM-DDTHH:MM``.
    earliest_start: str
    #: Start of the latest open slot.
    latest_start: str


__all__ = [
    "NewAppointment",
    "NewPatient",
    "NewWaitlistEntry",
    "NewDecision",
    "NewCallSession",
    "NewEscalation",
    "OpenSlotSpan",
    "SlotRelease",
]
