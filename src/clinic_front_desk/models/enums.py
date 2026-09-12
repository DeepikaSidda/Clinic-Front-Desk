"""Enumerations for the domain models (design "Data Models").

All are :class:`enum.StrEnum` so their members compare equal to their string
values and serialize directly to the DynamoDB item shape without conversion.
"""

from __future__ import annotations

from enum import StrEnum


class SlotStatus(StrEnum):
    """Lifecycle state of a bookable :class:`~.entities.Slot`.

    Only :attr:`OPEN` slots are offerable, so every other state removes a slot from
    what the agent can propose to a caller. The three unavailable states are kept
    distinct because they are undone in completely different ways:

    - :attr:`HELD` is transient — a reservation during a booking in progress —
      and is released automatically.
    - :attr:`BOOKED` belongs to a patient, and is freed only by cancelling or
      rescheduling their appointment.
    - :attr:`BLOCKED` is the doctor deliberately taking time off the calendar
      (surgery, lunch, leave). It is undone by the doctor unblocking it and by
      nothing else — in particular, republishing a day must not clear it, or the
      time the doctor protected would quietly become bookable again.
    """

    OPEN = "open"
    HELD = "held"
    BOOKED = "booked"
    BLOCKED = "blocked"


class AppointmentStatus(StrEnum):
    """Lifecycle state of an :class:`~.entities.Appointment`."""

    BOOKED = "booked"
    RESCHEDULED = "rescheduled"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    NO_SHOW = "no_show"


class DecisionKind(StrEnum):
    """The category of a Practice_Intelligence :class:`~.entities.Decision`/Finding."""

    GAP_FILL = "gap_fill"
    NO_SHOW_TREND = "no_show_trend"
    SCHEDULE_GAP = "schedule_gap"
    UNMET_DEMAND = "unmet_demand"
    UNOFFERED_SERVICE_DEMAND = "unoffered_service_demand"


class DecisionStatus(StrEnum):
    """Lifecycle state of a :class:`~.entities.Decision` (Req 14.3, 14.4, 14.6)."""

    OPEN = "open"
    APPROVED = "approved"
    DISMISSED = "dismissed"
    ACTION_FAILED = "action_failed"


class CallOutcome(StrEnum):
    """Persisted outcome of a :class:`~.entities.CallSession` (Req 11.5, 12.7)."""

    BOOKED = "booked"
    RESCHEDULED = "rescheduled"
    CANCELLED = "cancelled"
    WAITLISTED = "waitlisted"
    ESCALATED = "escalated"
    NO_ACTION = "no_action"
    INTERRUPTED = "interrupted"


class EscalationReason(StrEnum):
    """Reason an :class:`~.entities.Escalation` was recorded (Req 9.4)."""

    CLINICAL_CONTENT = "clinical_content"
    OUTSIDE_ADMIN_RULES = "outside_admin_rules"
    PATIENT_DISTRESS = "patient_distress"
    PATIENT_REQUEST = "patient_request"


__all__ = [
    "SlotStatus",
    "AppointmentStatus",
    "DecisionKind",
    "DecisionStatus",
    "CallOutcome",
    "EscalationReason",
]
