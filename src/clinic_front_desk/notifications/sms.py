"""Telling a patient something they need to know, by text message.

Built for one case first: the clinic cancels an appointment the patient is expecting.
Until now nothing in this system could reach a patient at all — every path was inbound,
so a cancelled appointment was invisible to the person it belonged to, and they would
have arrived to a locked door. The rest of the system works hard never to tell a caller
something untrue; letting their appointment silently evaporate is the same harm
arriving by a different route.

Three rules shape everything here.

**A failed message must never lose the cancellation.** The clinic's record of what
happened is more important than the notification about it, so every send returns an
outcome rather than raising, and the caller decides what to do with it.

**What was attempted is recorded either way.** A doctor who cannot tell whether the
patient was told will assume they were. So the outcome is returned for storing against
the appointment, and a failure is something she can see and act on by picking up the
phone.

**Indian numbers, said plainly.** Delivery to an Indian mobile needs more than AWS
permission. The account is in the SMS sandbox, so only verified numbers receive
anything; and production delivery to arbitrary Indian mobiles additionally requires
TRAI DLT registration — an entity and pre-approved templates lodged through an Indian
telecom operator — without which the *carriers* reject the message however happily AWS
accepts it. This module does not pretend otherwise: it reports what happened.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: Default country for a bare local number. The clinic is in Tirupati.
DEFAULT_COUNTRY_CODE = "+91"

#: A plausible Indian mobile: ten digits starting 6-9.
_INDIAN_MOBILE = re.compile(r"^[6-9]\d{9}$")


@dataclass(frozen=True)
class SmsOutcome:
    """What happened when a message was attempted.

    ``sent`` is the only thing a caller may report to a doctor as done. ``detail``
    carries the reason when it is false, because "the patient was not told" is
    actionable only if she knows why.
    """

    sent: bool
    to: str
    detail: str = ""
    message_id: str | None = None


def to_e164(number: str, *, country_code: str = DEFAULT_COUNTRY_CODE) -> str | None:
    """Normalise a stored number to E.164, or ``None`` if it cannot be trusted.

    Patient numbers are captured by speech, so they arrive with spaces, dashes and
    sometimes a country code already attached. ``None`` rather than a guess: texting a
    half-understood number sends a patient's appointment details to a stranger.

    >>> to_e164("9502285901")
    '+919502285901'
    >>> to_e164("+91 95022 85901")
    '+919502285901'
    >>> to_e164("12345") is None
    True
    """
    raw = (number or "").strip()
    if not raw:
        return None

    had_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None

    if had_plus:
        # Already international; trust the digits as given.
        return f"+{digits}" if 8 <= len(digits) <= 15 else None

    if digits.startswith("0") and _INDIAN_MOBILE.match(digits[1:]):
        # Domestic trunk prefix, as people write it down.
        return f"{country_code}{digits[1:]}"
    if digits.startswith("91") and _INDIAN_MOBILE.match(digits[2:]):
        return f"+{digits}"
    if _INDIAN_MOBILE.match(digits):
        return f"{country_code}{digits}"
    return None


class SmsSender(Protocol):
    """Anything that can attempt a text message."""

    def send(self, to: str, body: str) -> SmsOutcome:
        """Attempt to deliver ``body`` to ``to``. Never raises."""
        ...


class NullSmsSender:
    """Sends nothing and says so.

    The default, so an unconfigured deployment is honest rather than silently
    pretending patients were notified.
    """

    def send(self, to: str, body: str) -> SmsOutcome:
        logger.info("SMS not configured; would have texted %s: %s", to, body[:60])
        return SmsOutcome(sent=False, to=to, detail="SMS is not configured")


@dataclass
class SnsSmsSender:
    """Sends via Amazon SNS.

    SNS rather than Pinpoint because it needs no application, no campaign and no
    origination identity to send a one-off transactional message — and this is exactly
    that: one message, to one patient, about one appointment.

    Sent as ``Transactional`` deliberately. It changes the delivery path's priority and
    is the correct classification: a cancelled medical appointment is not marketing.
    """

    region: str
    client: Any | None = None
    sender_id: str | None = None

    def _sns(self) -> Any:
        if self.client is None:
            import boto3  # type: ignore[import-untyped]

            self.client = boto3.client("sns", region_name=self.region)
        return self.client

    def send(self, to: str, body: str) -> SmsOutcome:
        number = to_e164(to)
        if number is None:
            # Not an error to shout about: a number we cannot parse is a number we
            # must not text, and the doctor needs to ring them instead.
            return SmsOutcome(
                sent=False, to=to, detail=f"{to!r} is not a usable mobile number"
            )

        attributes: dict[str, Any] = {
            "AWS.SNS.SMS.SMSType": {"DataType": "String", "StringValue": "Transactional"}
        }
        if self.sender_id:
            attributes["AWS.SNS.SMS.SenderID"] = {
                "DataType": "String",
                "StringValue": self.sender_id,
            }

        try:
            response = self._sns().publish(
                PhoneNumber=number,
                Message=body,
                MessageAttributes=attributes,
            )
        except Exception as exc:  # noqa: BLE001 - a lost text must not lose the record
            detail = f"{type(exc).__name__}: {exc}"
            # Sandbox and DLT rejections both land here, and both mean the same thing
            # to a doctor: this patient has not been told, so call them.
            logger.warning("SMS to %s failed: %s", number, detail)
            return SmsOutcome(sent=False, to=number, detail=detail[:300])

        message_id = str(response.get("MessageId") or "")
        logger.info("texted %s (message id %s)", number, message_id)
        return SmsOutcome(sent=True, to=number, message_id=message_id)


def cancellation_message(
    *,
    patient_name: str,
    service: str,
    date: str,
    time: str,
    clinic_phone: str = "",
) -> str:
    """The text a patient receives when the clinic cancels on them.

    Written to be read on a lock screen in one glance: what was cancelled, when it
    was, and what to do next. It says the **clinic** cancelled, because a patient who
    thinks they cancelled will not ring back — and it names the number, since "contact
    the clinic" is not an instruction anyone can follow without it.

    Deliberately carries no clinical detail: a text message is read by whoever picks
    up the phone.
    """
    who = (patient_name or "").strip().split(" ")[0] or "there"
    when = f"{date} at {time}".strip()
    lines = [
        f"Hello {who}, your {service} on {when} has been cancelled by the clinic.",
        "Sorry for the inconvenience.",
    ]
    if clinic_phone.strip():
        lines.append(f"Please call {clinic_phone.strip()} to rebook.")
    else:
        lines.append("Please call the clinic to rebook.")
    return " ".join(lines)


__all__ = [
    "DEFAULT_COUNTRY_CODE",
    "NullSmsSender",
    "SmsOutcome",
    "SmsSender",
    "SnsSmsSender",
    "cancellation_message",
    "to_e164",
]
