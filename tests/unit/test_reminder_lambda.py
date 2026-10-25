"""The scheduled run: which day it picks, and what it does with several providers.

The date arithmetic is the part worth testing hardest, because it is the part that can
be wrong without anything failing. A schedule firing at 18:00 in Tirupati is 12:30 UTC,
and on most evenings UTC and clinic time agree about what tomorrow is — they stop
agreeing after 18:30 local. A reminder run that targets the right date on twenty-three
hours out of twenty-four is exactly the kind of bug that survives a demo.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from clinic_front_desk.data_layer.memory import (
    MemoryAppointmentStore,
    MemoryClinicKnowledgeBaseStore,
    MemoryPatientStore,
)
from clinic_front_desk.reminder_lambda import clinic_day, run_reminders
from clinic_front_desk.models import (
    Appointment,
    AppointmentStatus,
    ClinicKnowledgeBase,
    DayHours,
    Patient,
    Provider,
    ServiceConfig,
    is_ok,
)
from clinic_front_desk.notifications import SmsOutcome

IST = 330  # minutes ahead of UTC


class _Sender:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def send(self, to: str, body: str) -> SmsOutcome:
        self.messages.append((to, body))
        return SmsOutcome(sent=True, to=to, detail="")


class _Stores:
    """Just the three stores the reminder path touches."""

    def __init__(self, knowledge_base: Any) -> None:
        self.appointments = MemoryAppointmentStore()
        self.patients = MemoryPatientStore()
        self.clinic_knowledge_base = knowledge_base


# ---------------------------------------------------------------------------
# Which day gets reminded
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fired_utc", "expected"),
    [
        # 18:00 IST on the 20th -> remind the 21st. UTC still says the 20th here.
        (datetime(2026, 10, 20, 12, 30, tzinfo=UTC), "2026-10-21"),
        # 23:30 IST on the 20th. UTC has rolled to the 20th 18:00 but clinic time is
        # still the 20th, so tomorrow is still the 21st.
        (datetime(2026, 10, 20, 18, 0, tzinfo=UTC), "2026-10-21"),
        # 00:30 IST on the 21st — UTC is still the 20th. Clinic time has turned over,
        # so "tomorrow" is the 22nd. Computing this in UTC would say the 21st and the
        # clinic would remind a day it already reminded.
        (datetime(2026, 10, 20, 19, 0, tzinfo=UTC), "2026-10-22"),
    ],
)
def test_the_day_is_resolved_in_clinic_time_not_utc(
    fired_utc: datetime, expected: str
) -> None:
    assert clinic_day(lead_days=1, offset_minutes=IST, now=fired_utc) == expected


def test_lead_days_zero_means_today() -> None:
    # The manual re-run case: the evening schedule was missed, remind today instead.
    now = datetime(2026, 10, 20, 12, 30, tzinfo=UTC)
    assert clinic_day(lead_days=0, offset_minutes=IST, now=now) == "2026-10-20"


def test_a_month_boundary_rolls_over_correctly() -> None:
    now = datetime(2026, 10, 31, 12, 30, tzinfo=UTC)
    assert clinic_day(lead_days=1, offset_minutes=IST, now=now) == "2026-11-01"


def test_a_utc_clinic_is_supported_too() -> None:
    # The offset is configuration, not a constant, so this artefact runs for a clinic
    # that is not in India without a code change.
    now = datetime(2026, 10, 20, 23, 0, tzinfo=UTC)
    assert clinic_day(lead_days=1, offset_minutes=0, now=now) == "2026-10-21"


# ---------------------------------------------------------------------------
# What the run does
# ---------------------------------------------------------------------------

DAY = "2026-10-21"


def _knowledge_base(*provider_ids: str) -> MemoryClinicKnowledgeBaseStore:
    store = MemoryClinicKnowledgeBaseStore()
    store.save(
        ClinicKnowledgeBase(
            location="Tirupati",
            hours={index: DayHours(open="09:00", close="19:30") for index in range(6)},
            services=[ServiceConfig(name="ENT Consultation", price=500.0)],
            contact_phone="1234567890",
            providers=[
                Provider(id=pid, name=f"Dr {pid}", specialty="ENT")
                for pid in provider_ids
            ],
            configured=True,
        )
    )
    return store


def _book(stores: _Stores, provider_id: str, suffix: str, *, phone: str) -> None:
    created = stores.patients.create(
        Patient(id=f"pat-{suffix}", name=f"Patient {suffix}", callback_phone=phone)
    )
    assert is_ok(created)
    assert is_ok(
        stores.appointments.create(
            Appointment(
                id=f"appt-{suffix}",
                provider_id=provider_id,
                patient_id=f"pat-{suffix}",
                service="ENT Consultation",
                slot_id=f"slot-{suffix}",
                date=DAY,
                time="09:00",
                status=AppointmentStatus.BOOKED,
            )
        )
    )


def test_it_reminds_every_configured_provider() -> None:
    # A solo practice has one provider today. A clinic that grows should not find that
    # only the first one's patients were told.
    stores = _Stores(_knowledge_base("prov-a", "prov-b"))
    _book(stores, "prov-a", "a", phone="9900011111")
    _book(stores, "prov-b", "b", phone="9900022222")
    sender = _Sender()

    runs = run_reminders(stores=stores, sms_sender=sender, day=DAY)

    assert len(runs) == 2
    assert sum(run.sent for run in runs) == 2
    assert len(sender.messages) == 2


def test_the_clinic_number_reaches_the_message() -> None:
    stores = _Stores(_knowledge_base("prov-a"))
    _book(stores, "prov-a", "a", phone="9900011111")
    sender = _Sender()

    run_reminders(stores=stores, sms_sender=sender, day=DAY)

    assert "1234567890" in sender.messages[0][1], "so the patient can ring to cancel"


def test_no_configured_providers_is_a_quiet_no_op() -> None:
    # An unconfigured clinic should produce nothing, not an exception that wakes
    # somebody at six in the morning.
    stores = _Stores(_knowledge_base())
    sender = _Sender()

    runs = run_reminders(stores=stores, sms_sender=sender, day=DAY)

    assert runs == []
    assert sender.messages == []


def test_a_night_with_nothing_booked_sends_nothing() -> None:
    stores = _Stores(_knowledge_base("prov-a"))
    sender = _Sender()

    runs = run_reminders(stores=stores, sms_sender=sender, day=DAY)

    assert [run.booked for run in runs] == [0]
    assert sender.messages == []


def test_running_the_schedule_twice_in_one_evening_texts_nobody_twice() -> None:
    # The schedule has a retry policy, and the doctor still has her button. Both can
    # land on the same evening, so the second pass must be inert.
    stores = _Stores(_knowledge_base("prov-a"))
    _book(stores, "prov-a", "a", phone="9900011111")
    sender = _Sender()

    first = run_reminders(stores=stores, sms_sender=sender, day=DAY)
    second = run_reminders(stores=stores, sms_sender=sender, day=DAY)

    assert first[0].sent == 1
    assert second[0].sent == 0 and second[0].skipped == 1
    assert len(sender.messages) == 1
