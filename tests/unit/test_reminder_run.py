"""The reminder loop on its own, with no dashboard around it.

``test_remind_and_measure.py`` already drives this through the doctor's button, which is
the right place to prove the button works. These tests exist because a second caller now
exists that has no request, no role and no HTML: the scheduled Lambda calls
:func:`remind_booked_on_day` directly. If the function only ever held together as a
dashboard method, the schedule would be the thing that found out.

So each test here calls it the way the Lambda does — stores, a sender, a pinned clock —
and asserts on the returned :class:`ReminderRun` rather than on a rendered notice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from clinic_front_desk.data_layer.memory import (
    MemoryAppointmentStore,
    MemoryPatientStore,
)
from clinic_front_desk.models import (
    Appointment,
    AppointmentStatus,
    Patient,
    Slot,
    SlotStatus,
    is_ok,
)
from clinic_front_desk.notifications import SmsOutcome
from clinic_front_desk.notifications.reminders import (
    ReminderRun,
    remind_booked_on_day,
)

PROVIDER = "prov-1"
DAY = "2026-09-28"
SERVICE = "ENT Consultation"
FIXED_NOW = datetime(2026, 9, 27, 18, 30, tzinfo=UTC)


class _Sender:
    """Records what it was asked to send, and can be told to fail."""

    def __init__(self, *, sent: bool = True, detail: str = "") -> None:
        self._sent = sent
        self._detail = detail
        self.messages: list[tuple[str, str]] = []

    def send(self, to: str, body: str) -> SmsOutcome:
        self.messages.append((to, body))
        return SmsOutcome(sent=self._sent, to=to, detail=self._detail)


class _UnreadableAppointments(MemoryAppointmentStore):
    """A store whose day listing fails, standing in for a DynamoDB outage."""

    def list_by_provider_and_day(self, provider_id: str, day: str) -> Any:
        from clinic_front_desk.models import Err, StoreError, StoreErrorKind

        return Err(
            StoreError(kind=StoreErrorKind.STORE_FAILURE, detail="table unavailable")
        )


def _book(
    appointments: MemoryAppointmentStore,
    patients: MemoryPatientStore,
    suffix: str,
    time: str,
    *,
    phone: str,
    reminded_at: str = "",
) -> str:
    slot_id = f"slot-{suffix}"
    assert is_ok(
        appointments.add_slots(
            [
                Slot(
                    id=slot_id,
                    provider_id=PROVIDER,
                    service=SERVICE,
                    start=f"{DAY}T{time}:00Z",
                    end=f"{DAY}T{time}:30Z",
                    status=SlotStatus.OPEN,
                )
            ]
        )
    )
    created = patients.create(
        Patient(id=f"pat-{suffix}", name=f"Patient {suffix}", callback_phone=phone)
    )
    assert is_ok(created)
    appointments.claim_slot(slot_id)
    assert is_ok(
        appointments.create(
            Appointment(
                id=f"appt-{suffix}",
                provider_id=PROVIDER,
                patient_id=f"pat-{suffix}",
                service=SERVICE,
                slot_id=slot_id,
                date=DAY,
                time=time,
                status=AppointmentStatus.BOOKED,
                reminded_at=reminded_at,
            )
        )
    )
    return f"appt-{suffix}"


def _run(appointments: Any, patients: Any, sender: Any) -> ReminderRun:
    return remind_booked_on_day(
        day=DAY,
        provider_id=PROVIDER,
        appointments=appointments,
        patients=patients,
        sms_sender=sender,
        clinic_phone="1234567890",
        now=lambda: FIXED_NOW,
    )


def test_it_texts_every_booked_patient_and_counts_them() -> None:
    appointments, patients = MemoryAppointmentStore(), MemoryPatientStore()
    _book(appointments, patients, "a", "09:00", phone="9900011111")
    _book(appointments, patients, "b", "09:30", phone="9900022222")
    sender = _Sender()

    run = _run(appointments, patients, sender)

    assert (run.booked, run.sent, run.failed, run.skipped) == (2, 2, 0, 0)
    assert run.summary() == f"Reminded 2 of 2 patients on {DAY}."
    assert len(sender.messages) == 2
    # The clinic's own number travels in the body so the patient can ring back to
    # cancel — a reminder that frees a slot is worth more than one that merely informs.
    assert "1234567890" in sender.messages[0][1]


def test_an_already_reminded_patient_is_skipped_not_texted_again() -> None:
    # Now that a schedule and a button can both fire on the same day, this stamp is
    # the only thing standing between a patient and two identical messages.
    appointments, patients = MemoryAppointmentStore(), MemoryPatientStore()
    _book(appointments, patients, "a", "09:00", phone="9900011111")
    _book(
        appointments,
        patients,
        "b",
        "09:30",
        phone="9900022222",
        reminded_at="2026-09-27T17:00:00Z",
    )
    sender = _Sender()

    run = _run(appointments, patients, sender)

    assert (run.sent, run.skipped) == (1, 1)
    assert len(sender.messages) == 1
    assert "1 had already been reminded" in run.summary()


def test_running_it_twice_sends_nothing_the_second_time() -> None:
    appointments, patients = MemoryAppointmentStore(), MemoryPatientStore()
    _book(appointments, patients, "a", "09:00", phone="9900011111")
    sender = _Sender()

    first = _run(appointments, patients, sender)
    second = _run(appointments, patients, sender)

    assert first.sent == 1
    assert second.sent == 0 and second.skipped == 1
    assert len(sender.messages) == 1, "the second run was a no-op"


def test_one_failure_does_not_stop_the_rest_of_the_day() -> None:
    appointments, patients = MemoryAppointmentStore(), MemoryPatientStore()
    _book(appointments, patients, "a", "09:00", phone="")  # no number on file
    _book(appointments, patients, "b", "09:30", phone="9900022222")
    sender = _Sender()

    run = _run(appointments, patients, sender)

    assert (run.sent, run.failed) == (1, 1)
    assert len(sender.messages) == 1, "the reachable patient was still texted"
    assert "1 could not be texted" in run.summary(), "counted, not hidden"


def test_a_patient_with_no_number_is_stamped_so_it_is_not_retried_nightly() -> None:
    # "No number" is a fact about the record, not a transient error. Unstamped, a
    # nightly schedule would rediscover it every night for as long as the record lives.
    appointments, patients = MemoryAppointmentStore(), MemoryPatientStore()
    appointment_id = _book(appointments, patients, "a", "09:00", phone="")

    _run(appointments, patients, _Sender())

    found = appointments.get(appointment_id)
    assert is_ok(found) and found.value is not None
    assert found.value.reminded_at == FIXED_NOW.isoformat()
    assert found.value.reminder_failed == "no mobile number on file"


def test_a_bounced_message_is_stamped_with_why() -> None:
    appointments, patients = MemoryAppointmentStore(), MemoryPatientStore()
    appointment_id = _book(appointments, patients, "a", "09:00", phone="9900011111")
    sender = _Sender(sent=False, detail="sandbox: number not verified")

    run = _run(appointments, patients, sender)

    assert (run.sent, run.failed) == (0, 1)
    found = appointments.get(appointment_id)
    assert is_ok(found) and found.value is not None
    assert found.value.reminded_at, "the attempt is recorded either way"
    assert found.value.reminder_failed == "sandbox: number not verified"


def test_a_day_with_nothing_booked_says_so_and_sends_nothing() -> None:
    appointments, patients = MemoryAppointmentStore(), MemoryPatientStore()
    sender = _Sender()

    run = _run(appointments, patients, sender)

    assert (run.booked, run.sent) == (0, 0)
    assert run.summary() == f"No booked appointments on {DAY}."
    assert sender.messages == []


def test_only_booked_appointments_are_reminded() -> None:
    # A completed or no-show appointment is in the past; texting a reminder for one
    # would be telling a patient to attend something that already happened.
    appointments, patients = MemoryAppointmentStore(), MemoryPatientStore()
    assert is_ok(
        patients.create(Patient(id="pat-x", name="Patient X", callback_phone="9900011111"))
    )
    assert is_ok(
        appointments.create(
            Appointment(
                id="appt-x",
                provider_id=PROVIDER,
                patient_id="pat-x",
                service=SERVICE,
                slot_id="slot-x",
                date=DAY,
                time="09:00",
                status=AppointmentStatus.COMPLETED,
            )
        )
    )
    sender = _Sender()

    run = _run(appointments, patients, sender)

    assert run.booked == 0, "only booked appointments are reminded"
    assert sender.messages == []


def test_an_unreadable_day_reports_the_failure_and_sends_nothing() -> None:
    # The scheduled caller has nobody to show an error page to, so the failure has to
    # come back on the result where a log or an alarm can see it.
    sender = _Sender()

    run = _run(_UnreadableAppointments(), MemoryPatientStore(), sender)

    assert run.read_error == "table unavailable"
    assert run.attempted is False
    assert (run.sent, run.failed, run.booked) == (0, 0, 0)
    assert run.summary() == f"Could not read {DAY}: table unavailable"
    assert sender.messages == []


def test_the_stamp_uses_the_injected_clock() -> None:
    # The Lambda runs on AWS's clock and the dashboard on the host's; both must be able
    # to say which, and tests must be able to pin it.
    appointments, patients = MemoryAppointmentStore(), MemoryPatientStore()
    appointment_id = _book(appointments, patients, "a", "09:00", phone="9900011111")

    remind_booked_on_day(
        day=DAY,
        provider_id=PROVIDER,
        appointments=appointments,
        patients=patients,
        sms_sender=_Sender(),
        now=lambda: datetime(2031, 1, 2, 3, 4, 5, tzinfo=UTC),
    )

    found = appointments.get(appointment_id)
    assert is_ok(found) and found.value is not None
    assert found.value.reminded_at.startswith("2031-01-02T03:04:05")


@pytest.mark.parametrize("provider", ["", "prov-unknown"])
def test_an_unknown_provider_has_no_appointments_rather_than_an_error(
    provider: str,
) -> None:
    appointments, patients = MemoryAppointmentStore(), MemoryPatientStore()
    _book(appointments, patients, "a", "09:00", phone="9900011111")

    run = remind_booked_on_day(
        day=DAY,
        provider_id=provider,
        appointments=appointments,
        patients=patients,
        sms_sender=_Sender(),
        now=lambda: FIXED_NOW,
    )

    assert run.booked == 0 and not run.read_error
