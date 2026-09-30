"""Reaching a patient who is not on the phone right now.

Every other path in this system is inbound: the patient rings, the agent answers. That
leaves the cases the clinic cannot work around — the two moments when the clinic changes
a patient's appointment and the patient is not there to hear it.

**Cancelling.** They are not on the line, and until this existed nothing could reach
them, so a cancelled appointment was invisible to the person it belonged to and they
arrived to a locked door.

**Filling a gap from the waiting list.** The same hole from the opposite direction, and
easier to miss: approving a gap-fill *books* someone, and a patient who is never told
they have an appointment does not attend one. The clinic then records a no-show against
a patient who did nothing wrong, which also poisons the no-show trend the second agent
reports on.
"""

from .sms import (
    DEFAULT_COUNTRY_CODE,
    NullSmsSender,
    SmsOutcome,
    SmsSender,
    SnsSmsSender,
    cancellation_message,
    gap_fill_message,
    reminder_message,
    to_e164,
)

__all__ = [
    "DEFAULT_COUNTRY_CODE",
    "NullSmsSender",
    "SmsOutcome",
    "SmsSender",
    "SnsSmsSender",
    "cancellation_message",
    "gap_fill_message",
    "reminder_message",
    "to_e164",
]
