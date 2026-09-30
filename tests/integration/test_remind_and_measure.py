"""Reminding a day's patients, and being able to say whether it helped.

Practice_Intelligence already notices a worsening no-show rate and recommends
reminders. Until now nothing could act on that — the clinic could read the advice and
had no way to follow it — so the loop ended in a suggestion. This closes it.

Two things are being tested, and the second is the one that matters. That the
reminders go out, and that the clinic can afterwards tell whether reminded patients
actually turned up more often. "We send reminders" is an activity; "reminded patients
miss fewer appointments" is a result, and only the second is worth a doctor's
attention.

Deliberately doctor-pressed rather than scheduled: nothing in this deployment runs on
a timer, so a reminder advertised as firing "the day before" would be advertising
something that does not happen.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from clinic_front_desk.dashboard.metrics import Period, no_show_rate_by_reminder
from clinic_front_desk.deployment.app import build_memory_application
from clinic_front_desk.deployment.dashboard_app import DashboardWebApp
from clinic_front_desk.models import (
    Appointment,
    AppointmentStatus,
    ClinicKnowledgeBase,
    DayHours,
    Patient,
    Provider,
    ServiceConfig,
    Slot,
    SlotStatus,
    is_ok,
)
from clinic_front_desk.notifications import SmsOutcome

pytestmark = pytest.mark.integration

PROVIDER = "prov-1"
DAY = "2026-09-28"
SERVICE = "ENT Consultation"


class _RecordingSender:
    def __init__(self, *, sent: bool = True, detail: str = "") -> None:
        self.sent = sent
        self.detail = detail
        self.messages: list[tuple[str, str]] = []

    def send(self, to: str, body: str) -> SmsOutcome:
        self.messages.append((to, body))
        return SmsOutcome(sent=self.sent, to=to, detail=self.detail)


def _clinic(app: Any) -> None:
    app.stores.knowledge_base.save(
        ClinicKnowledgeBase(
            location="Tirupati",
            hours={index: DayHours(open="09:00", close="19:30") for index in range(6)},
            services=[ServiceConfig(name=SERVICE, price=500.0)],
            contact_phone="1234567890",
            providers=[Provider(id=PROVIDER, name="Dr Raana", specialty="ENT")],
            configured=True,
        )
    )


def _booked(app: Any, suffix: str, time: str, *, phone: str) -> str:
    slot_id = f"slot-{suffix}"
    assert is_ok(
        app.stores.appointments.add_slots(
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
    created = app.stores.patients.create(
        Patient(id=f"pat-{suffix}", name=f"Patient {suffix}", callback_phone=phone)
    )
    assert is_ok(created)
    app.stores.appointments.claim_slot(slot_id)
    assert is_ok(
        app.stores.appointments.create(
            Appointment(
                id=f"appt-{suffix}",
                provider_id=PROVIDER,
                patient_id=created.value.id,
                service=SERVICE,
                slot_id=slot_id,
                date=DAY,
                time=time,
                status=AppointmentStatus.BOOKED,
                created_at=f"{DAY}T08:00:00Z",
                updated_at=f"{DAY}T08:00:00Z",
            )
        )
    )
    return f"appt-{suffix}"


def _dashboard(app: Any, sender: Any) -> DashboardWebApp:
    return DashboardWebApp(
        app, sms_sender=sender, now=lambda: datetime(2026, 9, 27, 18, 0, tzinfo=UTC)
    )


def test_pressing_remind_texts_everyone_booked_that_day() -> None:
    app = build_memory_application()
    _clinic(app)
    _booked(app, "a", "09:00", phone="9502285901")
    _booked(app, "b", "09:30", phone="9502285902")
    sender = _RecordingSender()

    message, error = _dashboard(app, sender).remind_day("doctor", day=DAY)

    assert error is None
    assert message == f"Reminded 2 of 2 patients on {DAY}."
    assert len(sender.messages) == 2
    # The message asks them to ring if they cannot come. A reminder that only informs
    # rescues a forgotten appointment; one that invites a cancellation also frees a
    # slot the waiting list can use, which is the more valuable outcome.
    _to, body = sender.messages[0]
    assert "reminder" in body.lower()
    assert "1234567890" in body


def test_pressing_remind_twice_does_not_text_anyone_twice() -> None:
    # The doctor who cannot remember whether she pressed it is exactly the person who
    # presses it again. The stamp on the appointment is what makes that harmless.
    app = build_memory_application()
    _clinic(app)
    _booked(app, "a", "09:00", phone="9502285901")
    sender = _RecordingSender()
    dashboard = _dashboard(app, sender)

    dashboard.remind_day("doctor", day=DAY)
    message, error = dashboard.remind_day("doctor", day=DAY)

    assert error is None
    assert len(sender.messages) == 1, "the second press sent nothing"
    assert "already been reminded" in (message or "")


def test_one_unreachable_number_does_not_stop_the_rest_of_the_day() -> None:
    # Each patient is independent. Aborting the run on the first failure would leave
    # the rest of the day silently un-reminded.
    app = build_memory_application()
    _clinic(app)
    _booked(app, "a", "09:00", phone="")  # nothing to text
    _booked(app, "b", "09:30", phone="9502285902")
    sender = _RecordingSender()

    message, error = _dashboard(app, sender).remind_day("doctor", day=DAY)

    assert error is None
    assert len(sender.messages) == 1, "the reachable patient was still texted"
    assert message is not None
    assert "Reminded 1 of 2" in message
    assert "1 could not be texted" in message, "the failure is counted, not hidden"


def test_a_failed_send_is_still_recorded_as_attempted() -> None:
    # Recording only the successes would make a retry loop that texts an unreachable
    # number every evening, and would leave the clinic unable to tell "never tried"
    # from "tried and bounced".
    app = build_memory_application()
    _clinic(app)
    appointment_id = _booked(app, "a", "09:00", phone="9502285901")
    sender = _RecordingSender(sent=False, detail="sandbox: number not verified")

    _dashboard(app, sender).remind_day("doctor", day=DAY)

    found = app.stores.appointments.get(appointment_id)
    assert is_ok(found) and found.value is not None
    assert found.value.reminded_at, "the attempt is stamped"
    assert found.value.reminder_failed == "sandbox: number not verified"


def test_a_day_with_nothing_booked_says_so_rather_than_claiming_success() -> None:
    app = build_memory_application()
    _clinic(app)
    sender = _RecordingSender()

    message, error = _dashboard(app, sender).remind_day("doctor", day=DAY)

    assert error is None
    assert message == f"No booked appointments on {DAY}."
    assert sender.messages == []


def test_an_assistant_may_remind_but_a_visitor_with_no_role_may_not() -> None:
    from clinic_front_desk.deployment.dashboard_app import DashboardHttpError

    app = build_memory_application()
    _clinic(app)
    _booked(app, "a", "09:00", phone="9502285901")
    dashboard = _dashboard(app, _RecordingSender())

    # Reminding is schedule work, which the assistant already does.
    message, error = dashboard.remind_day("assistant", day=DAY)
    assert error is None and message is not None

    with pytest.raises(DashboardHttpError):
        dashboard.remind_day(None, day=DAY)


# -- did it actually help? ---------------------------------------------------


def _attended(
    suffix: str, *, status: AppointmentStatus, reminded: bool
) -> Appointment:
    return Appointment(
        id=f"appt-{suffix}",
        provider_id=PROVIDER,
        patient_id=f"pat-{suffix}",
        service=SERVICE,
        slot_id=f"slot-{suffix}",
        date=DAY,
        time="09:00",
        status=status,
        reminded_at=f"{DAY}T08:00:00Z" if reminded else "",
    )


def test_the_clinic_can_compare_reminded_against_unreminded() -> None:
    period = Period(
        start=datetime(2026, 9, 1, tzinfo=UTC), end=datetime(2026, 10, 1, tzinfo=UTC)
    )
    appointments = [
        _attended("r1", status=AppointmentStatus.COMPLETED, reminded=True),
        _attended("r2", status=AppointmentStatus.COMPLETED, reminded=True),
        _attended("r3", status=AppointmentStatus.COMPLETED, reminded=True),
        _attended("r4", status=AppointmentStatus.NO_SHOW, reminded=True),
        _attended("u1", status=AppointmentStatus.COMPLETED, reminded=False),
        _attended("u2", status=AppointmentStatus.NO_SHOW, reminded=False),
    ]

    reminded_rate, reminded_n, unreminded_rate, unreminded_n = (
        no_show_rate_by_reminder(appointments, period)
    )

    assert (reminded_n, unreminded_n) == (4, 2)
    assert reminded_rate == pytest.approx(0.25)
    assert unreminded_rate == pytest.approx(0.5)


def test_a_bounced_reminder_counts_as_reminded() -> None:
    # Otherwise every unreachable patient quietly joins the comparison group and
    # flatters the result: the clinic would be comparing people it reached against
    # people it never tried, which is not the question being asked.
    period = Period(
        start=datetime(2026, 9, 1, tzinfo=UTC), end=datetime(2026, 10, 1, tzinfo=UTC)
    )
    bounced = _attended("b", status=AppointmentStatus.NO_SHOW, reminded=True)
    bounced.reminder_failed = "sandbox: number not verified"

    _rate, reminded_n, _unreminded_rate, unreminded_n = no_show_rate_by_reminder(
        [bounced], period
    )

    assert (reminded_n, unreminded_n) == (1, 0)


def test_an_empty_group_reports_zero_rather_than_dividing_by_nothing() -> None:
    period = Period(
        start=datetime(2026, 9, 1, tzinfo=UTC), end=datetime(2026, 10, 1, tzinfo=UTC)
    )

    assert no_show_rate_by_reminder([], period) == (0.0, 0, 0.0, 0)


def test_appointments_that_never_reached_an_outcome_are_excluded() -> None:
    # Still-booked appointments have not happened yet, so counting them would report a
    # falsely low no-show rate for a day that is not over.
    period = Period(
        start=datetime(2026, 9, 1, tzinfo=UTC), end=datetime(2026, 10, 1, tzinfo=UTC)
    )
    pending = _attended("p", status=AppointmentStatus.BOOKED, reminded=True)

    assert no_show_rate_by_reminder([pending], period) == (0.0, 0, 0.0, 0)
