"""The doctor cancels an appointment, and the patient is told.

This closes the one gap the clinic could not work around. Every other path is inbound —
the patient rings, the agent answers — so a patient whose appointment the clinic
cancelled had no way of finding out and would arrive to a locked door.

It is also the only destructive action a doctor can take from the dashboard, which is
why blocking a booked slot is still refused: hiding the time while leaving the patient
expecting it is the failure this replaces.

The assertions that matter are about **what the doctor is told**. A failed text must
leave her knowing she has to ring them, because a doctor who assumes the patient was
notified will not.
"""

from __future__ import annotations

from typing import Any

import pytest

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


class _RecordingSender:
    """Captures what would have been texted."""

    def __init__(self, *, sent: bool = True, detail: str = "") -> None:
        self.sent = sent
        self.detail = detail
        self.messages: list[tuple[str, str]] = []

    def send(self, to: str, body: str) -> SmsOutcome:
        self.messages.append((to, body))
        return SmsOutcome(
            sent=self.sent,
            to=to if self.sent else to,
            detail=self.detail,
            message_id="mid" if self.sent else None,
        )


def _clinic(app: Any) -> None:
    app.stores.knowledge_base.save(
        ClinicKnowledgeBase(
            location="Tirupati",
            hours={index: DayHours(open="09:00", close="19:30") for index in range(6)},
            services=[ServiceConfig(name="ENT Consultation", price=500.0)],
            contact_phone="1234567890",
            providers=[Provider(id=PROVIDER, name="Dr Raana", specialty="ENT")],
            configured=True,
        )
    )


def _booked(app: Any, *, phone: str = "9502285901") -> tuple[str, str]:
    """Seed a booked appointment. Returns (appointment_id, slot_id)."""
    slot = Slot(
        id="slot-1",
        provider_id=PROVIDER,
        service="ENT Consultation",
        start=f"{DAY}T09:30:00Z",
        end=f"{DAY}T10:00:00Z",
        status=SlotStatus.OPEN,
    )
    assert is_ok(app.stores.appointments.add_slots([slot]))
    created = app.stores.patients.create(
        Patient(id="pat-1", name="Sidda Deepika", callback_phone=phone)
    )
    assert is_ok(created)
    app.stores.appointments.claim_slot("slot-1")
    appointment = app.stores.appointments.create(
        Appointment(
            id="appt-1",
            provider_id=PROVIDER,
            patient_id=created.value.id,
            service="ENT Consultation",
            slot_id="slot-1",
            date=DAY,
            time="09:30",
            status=AppointmentStatus.BOOKED,
            created_at=f"{DAY}T08:00:00Z",
            updated_at=f"{DAY}T08:00:00Z",
        )
    )
    assert is_ok(appointment)
    return "appt-1", "slot-1"


def _dashboard(app: Any, sender: Any) -> DashboardWebApp:
    return DashboardWebApp(app, sms_sender=sender)


def test_cancelling_texts_the_patient_and_says_so() -> None:
    app = build_memory_application()
    _clinic(app)
    appointment_id, _slot = _booked(app)
    sender = _RecordingSender()

    message, error = _dashboard(app, sender).cancel_appointment(
        "doctor", appointment_id=appointment_id
    )

    assert error is None
    assert message is not None
    assert "Cancelled ENT Consultation" in message
    assert "has been texted" in message

    to, body = sender.messages[0]
    assert to == "9502285901"
    assert "cancelled by the clinic" in body
    # The clinic's number, so "call to rebook" is actionable.
    assert "1234567890" in body


def test_the_slot_is_freed_so_someone_else_can_take_it() -> None:
    app = build_memory_application()
    _clinic(app)
    appointment_id, slot_id = _booked(app)

    _dashboard(app, _RecordingSender()).cancel_appointment(
        "doctor", appointment_id=appointment_id
    )

    slot = app.stores.appointments.get_slot(slot_id).unwrap()
    assert slot is not None
    assert slot.status == SlotStatus.OPEN


def test_a_failed_text_is_reported_so_the_doctor_rings_them() -> None:
    """The one that protects the patient.

    A cancellation the doctor believes was communicated, but was not, is worse than no
    notification at all: she stops thinking about it and the patient still turns up.
    """
    app = build_memory_application()
    _clinic(app)
    appointment_id, _slot = _booked(app)
    sender = _RecordingSender(sent=False, detail="not verified in the SMS sandbox")

    message, error = _dashboard(app, sender).cancel_appointment(
        "doctor", appointment_id=appointment_id
    )

    # The cancellation still stands — the record outranks the notice about it.
    assert error is None
    assert message is not None
    assert "NOT texted" in message
    assert "sandbox" in message
    assert "please ring 9502285901 yourself" in message


def test_a_patient_with_no_number_is_flagged_not_silently_skipped() -> None:
    app = build_memory_application()
    _clinic(app)
    slot = Slot(
        id="slot-2",
        provider_id=PROVIDER,
        service="ENT Consultation",
        start=f"{DAY}T11:00:00Z",
        end=f"{DAY}T11:30:00Z",
    )
    app.stores.appointments.add_slots([slot])
    app.stores.appointments.claim_slot("slot-2")
    app.stores.appointments.create(
        Appointment(
            id="appt-2",
            provider_id=PROVIDER,
            patient_id="",
            service="ENT Consultation",
            slot_id="slot-2",
            date=DAY,
            time="11:00",
            status=AppointmentStatus.BOOKED,
            created_at=f"{DAY}T08:00:00Z",
            updated_at=f"{DAY}T08:00:00Z",
        )
    )
    sender = _RecordingSender()

    message, _error = _dashboard(app, sender).cancel_appointment(
        "doctor", appointment_id="appt-2"
    )

    assert message is not None
    assert "No mobile number on file" in message
    assert sender.messages == [], "nothing may be sent with no number"


def test_the_slot_id_from_the_day_view_resolves_to_the_appointment() -> None:
    """The page knows slots, not appointments; the button passes a slot id."""
    app = build_memory_application()
    _clinic(app)
    _appointment, slot_id = _booked(app)
    sender = _RecordingSender()

    message, error = _dashboard(app, sender).cancel_appointment(
        "doctor", slot_id=slot_id, day=DAY, provider_id=PROVIDER
    )

    assert error is None
    assert message is not None and "Cancelled" in message
    assert sender.messages, "the patient should still have been texted"


def test_an_unknown_slot_is_refused_rather_than_cancelling_something_else() -> None:
    app = build_memory_application()
    _clinic(app)
    _booked(app)
    sender = _RecordingSender()

    message, error = _dashboard(app, sender).cancel_appointment(
        "doctor", slot_id="slot-does-not-exist", day=DAY, provider_id=PROVIDER
    )

    assert message is None
    assert error == "No appointment found for that slot."
    assert sender.messages == []


def test_an_assistant_may_cancel_because_the_schedule_is_their_job() -> None:
    """Gated on SCHEDULE, which the assistant already holds.

    Asserted deliberately rather than left implicit. Cancelling is destructive and
    texts a patient, so it is worth stating that this is intentionally front-desk work
    and not doctor-only: an assistant who can publish and block a day but cannot
    cancel would have to interrupt the doctor mid-consultation to free a slot, which is
    the problem this whole system exists to remove.
    """
    app = build_memory_application()
    _clinic(app)
    appointment_id, _slot = _booked(app)
    sender = _RecordingSender()

    message, error = _dashboard(app, sender).cancel_appointment(
        "assistant", appointment_id=appointment_id
    )

    assert error is None
    assert message is not None and "Cancelled" in message


def test_an_unknown_role_cannot_cancel() -> None:
    """The gate still holds for anyone outside the clinic."""
    from clinic_front_desk.deployment.dashboard_app import DashboardHttpError

    app = build_memory_application()
    _clinic(app)
    appointment_id, _slot = _booked(app)
    sender = _RecordingSender()

    with pytest.raises(DashboardHttpError):
        _dashboard(app, sender).cancel_appointment(
            "patient", appointment_id=appointment_id
        )

    assert sender.messages == [], "no text may go out on a denied request"
