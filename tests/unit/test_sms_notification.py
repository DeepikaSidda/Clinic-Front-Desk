"""Texting a patient whose appointment the clinic cancelled.

Every other path in this system is inbound, which left one real gap: a patient whose
appointment is cancelled is not on the line, and nothing could reach them. They would
arrive to a locked door. The rest of the system works hard never to tell a caller
something untrue, and letting their appointment silently evaporate is the same harm by
another route.

The interesting tests here are the refusals. A half-understood mobile number must not
be texted at all — sending a patient's appointment details to a stranger is worse than
sending nothing — and a failed send must never take the cancellation down with it.
"""

from __future__ import annotations

from typing import Any

import pytest

from clinic_front_desk.notifications import (
    NullSmsSender,
    SnsSmsSender,
    cancellation_message,
    to_e164,
)


class _FakeSns:
    def __init__(self, *, explode: Exception | None = None) -> None:
        self.published: list[dict[str, Any]] = []
        self.explode = explode

    def publish(self, **kwargs: Any) -> dict[str, Any]:
        if self.explode is not None:
            raise self.explode
        self.published.append(kwargs)
        return {"MessageId": "mid-1"}


# -- number handling: the part that must never guess ------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9502285901", "+919502285901"),
        ("+919502285901", "+919502285901"),
        ("+91 95022 85901", "+919502285901"),
        ("095022-85901", "+919502285901"),
        ("91 9502285901", "+919502285901"),
        ("  9502285901  ", "+919502285901"),
    ],
)
def test_a_number_is_normalised_however_it_was_written(raw: str, expected: str) -> None:
    """Numbers are captured by speech, so they arrive in every shape."""
    assert to_e164(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "12345",
        "1234567890",  # starts with 1: not an Indian mobile
        "5502285901",  # starts with 5: not an Indian mobile
        "nine five zero two",  # words the normaliser never converted
        "abc",
    ],
)
def test_an_untrustworthy_number_is_refused_not_guessed(raw: str) -> None:
    """The one that matters: texting a misheard number tells a stranger about a patient."""
    assert to_e164(raw) is None


def test_a_refused_number_is_never_published() -> None:
    fake = _FakeSns()
    sender = SnsSmsSender(region="us-east-1", client=fake)

    outcome = sender.send("12345", "anything")

    assert outcome.sent is False
    assert "not a usable mobile number" in outcome.detail
    assert fake.published == [], "nothing may be sent to an unparseable number"


# -- sending ----------------------------------------------------------------


def test_a_message_is_sent_as_transactional() -> None:
    """A cancelled medical appointment is not marketing, and the class affects routing."""
    fake = _FakeSns()
    sender = SnsSmsSender(region="us-east-1", client=fake)

    outcome = sender.send("9502285901", "Your appointment was cancelled.")

    assert outcome.sent is True
    assert outcome.message_id == "mid-1"
    sent = fake.published[0]
    assert sent["PhoneNumber"] == "+919502285901"
    assert (
        sent["MessageAttributes"]["AWS.SNS.SMS.SMSType"]["StringValue"]
        == "Transactional"
    )


def test_a_failure_is_reported_rather_than_raised() -> None:
    """The clinic's record of the cancellation outranks the notice about it.

    Both the SMS sandbox and an Indian DLT rejection surface here, and both mean the
    same thing to a doctor: this patient has not been told, so ring them.
    """
    fake = _FakeSns(explode=RuntimeError("not verified in the SMS sandbox"))
    sender = SnsSmsSender(region="us-east-1", client=fake)

    outcome = sender.send("9502285901", "body")

    assert outcome.sent is False
    assert "sandbox" in outcome.detail


def test_nothing_is_configured_by_default() -> None:
    """An unconfigured deployment says so instead of pretending patients were told."""
    outcome = NullSmsSender().send("+919502285901", "body")

    assert outcome.sent is False
    assert "not configured" in outcome.detail


# -- what the patient actually reads ---------------------------------------


def test_the_message_says_the_clinic_cancelled_and_how_to_rebook() -> None:
    """Read on a lock screen in one glance: what, when, and what to do."""
    body = cancellation_message(
        patient_name="Sailaja Devi",
        service="ENT Consultation",
        date="Monday 14 September",
        time="09:00",
        clinic_phone="1234567890",
    )

    assert "Sailaja" in body
    assert "ENT Consultation" in body
    assert "Monday 14 September" in body
    assert "09:00" in body
    # It must be clear the clinic cancelled: a patient who thinks they did will not
    # ring back.
    assert "cancelled by the clinic" in body
    assert "1234567890" in body


def test_the_message_still_works_without_a_configured_number() -> None:
    body = cancellation_message(
        patient_name="Sailaja Devi",
        service="ENT Consultation",
        date="Monday",
        time="09:00",
    )

    assert "call the clinic" in body.lower()
    assert "None" not in body


def test_the_message_carries_no_clinical_detail() -> None:
    """A text is read by whoever picks the phone up."""
    body = cancellation_message(
        patient_name="Sailaja Devi",
        service="ENT Consultation",
        date="Monday",
        time="09:00",
        clinic_phone="1234567890",
    )

    for leak in ("blood", "weight", "height", "symptom", "diagnosis"):
        assert leak not in body.lower()


def test_a_missing_name_does_not_produce_a_broken_greeting() -> None:
    body = cancellation_message(
        patient_name="", service="Hearing Test", date="Tuesday", time="10:00"
    )

    assert body.startswith("Hello there,")
