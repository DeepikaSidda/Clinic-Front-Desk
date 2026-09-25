"""Reaching a patient who is not on the phone right now.

Every other path in this system is inbound: the patient rings, the agent answers. That
leaves one gap the clinic cannot work around — telling someone their appointment has
been cancelled. They are not on the line, and until now nothing could reach them, so a
cancelled appointment was invisible to the person it belonged to.
"""

from .sms import (
    DEFAULT_COUNTRY_CODE,
    NullSmsSender,
    SmsOutcome,
    SmsSender,
    SnsSmsSender,
    cancellation_message,
    to_e164,
)

__all__ = [
    "DEFAULT_COUNTRY_CODE",
    "NullSmsSender",
    "SmsOutcome",
    "SmsSender",
    "SnsSmsSender",
    "cancellation_message",
    "to_e164",
]
