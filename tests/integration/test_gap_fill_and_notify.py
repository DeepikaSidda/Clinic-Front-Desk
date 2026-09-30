"""Approving a gap-fill has to tell the patient it booked (Req 8.2-8.5, Req 14.3).

The mirror of ``test_cancel_and_notify``, and it exists because the same hole was
open on the other side. ``fill_gap_from_waitlist`` writes the appointment and removes
the waitlist entry, and nothing told the person it had happened. So a patient who
asked three weeks ago was booked into a slot they had never heard about, did not
attend, and was recorded as a **no-show** — which then feeds the no-show trend the
second agent reports to the doctor. A quiet correctness bug that manufactures its own
misleading statistics.

As with cancellation, the assertions are about **what the doctor is told**. A booking
that could not be announced must leave her knowing to ring them, because otherwise she
assumes the patient knows and nobody rings anyone.
"""

from __future__ import annotations

from typing import Any

import pytest

from clinic_front_desk.deployment.app import build_memory_application
from clinic_front_desk.deployment.dashboard_app import DashboardWebApp
from clinic_front_desk.models import (
    ClinicKnowledgeBase,
    DayHours,
    Decision,
    DecisionKind,
    DecisionStatus,
    Patient,
    Provider,
    ServiceConfig,
    Slot,
    SlotStatus,
    WaitlistEntry,
    is_ok,
)
from clinic_front_desk.notifications import SmsOutcome

pytestmark = pytest.mark.integration

PROVIDER = "prov-1"
DAY = "2026-09-28"
SERVICE = "ENT Consultation"
SLOT_ID = "slot-open-1"


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
            to=to,
            detail=self.detail,
            message_id="mid" if self.sent else None,
        )


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


def _waiting_patient(app: Any, *, phone: str = "9502285901") -> str:
    """An open slot plus someone on the waitlist for it. Returns the patient id."""
    slot = Slot(
        id=SLOT_ID,
        provider_id=PROVIDER,
        service=SERVICE,
        start=f"{DAY}T11:00:00Z",
        end=f"{DAY}T11:30:00Z",
        status=SlotStatus.OPEN,
    )
    assert is_ok(app.stores.appointments.add_slots([slot]))

    created = app.stores.patients.create(
        Patient(id="pat-waiting", name="Ramesh Chandra", callback_phone=phone)
    )
    assert is_ok(created)

    entry = app.stores.waitlist.add(
        WaitlistEntry(
            id="wait-1",
            patient_id=created.value.id,
            service=SERVICE,
            preferred_slot_type="any",
            added_at=f"{DAY}T08:00:00Z",
            seq=1,
            active=True,
        )
    )
    assert is_ok(entry)
    return str(created.value.id)


def _gap_fill_decision(app: Any) -> str:
    """An open gap-fill Decision pointing at the open slot. Returns its id."""
    decision = app.stores.decisions.create(
        Decision(
            id="dec-gap-1",
            kind=DecisionKind.GAP_FILL,
            finding_key=f"gap_fill#{SLOT_ID}",
            summary=f"{SLOT_ID} is open and someone is waiting for {SERVICE}.",
            recommended_action="Book the earliest matching waitlisted patient.",
            action_payload={"slot_id": SLOT_ID},
            supporting_record_count=5,
            status=DecisionStatus.OPEN,
            generated_at=f"{DAY}T09:00:00Z",
        )
    )
    assert is_ok(decision)
    return "dec-gap-1"


def _dashboard(app: Any, sender: Any) -> DashboardWebApp:
    return DashboardWebApp(app, sms_sender=sender)


def test_approving_a_gap_fill_texts_the_patient_it_just_booked() -> None:
    app = build_memory_application()
    _clinic(app)
    _waiting_patient(app)
    decision_id = _gap_fill_decision(app)
    sender = _RecordingSender()

    result = _dashboard(app, sender).resolve_decision("doctor", decision_id, "approve")

    assert result["outcome"] == "approved"
    assert result["error"] is None
    assert result["notified"] == "The patient has been texted on 9502285901."

    assert len(sender.messages) == 1, "the booked patient must be told exactly once"
    to, body = sender.messages[0]
    assert to == "9502285901"
    # It has to say why they are hearing from the clinic: an appointment nobody asked
    # for, arriving unexplained, reads like a mistake.
    assert "waiting list" in body
    assert SERVICE in body
    assert "11:00" in body
    # And offer a way out, because being given a slot is not the same as being able
    # to take it.
    assert "1234567890" in body


def test_a_failed_text_still_books_and_tells_the_doctor_to_ring() -> None:
    # The booking is the clinic's record of what is true; the message is a
    # notification about it. A lost text must not roll back a slot that is now
    # legitimately taken — but it must not be silent either.
    app = build_memory_application()
    _clinic(app)
    _waiting_patient(app)
    decision_id = _gap_fill_decision(app)
    sender = _RecordingSender(sent=False, detail="sandbox: number not verified")

    result = _dashboard(app, sender).resolve_decision("doctor", decision_id, "approve")

    assert result["outcome"] == "approved", "the booking stands"
    assert result["error"] is None
    assert "NOT texted" in result["notified"]
    assert "sandbox: number not verified" in result["notified"]
    assert "9502285901" in result["notified"], "she needs the number to ring"

    # The appointment really exists, despite the failed text.
    booked = app.stores.appointments.list_by_provider_and_day(PROVIDER, DAY)
    assert is_ok(booked)
    assert len(booked.value) == 1


def test_a_patient_with_no_mobile_is_reported_rather_than_silently_skipped() -> None:
    # This is the case that manufactures a false no-show: booked, uninformed, absent.
    app = build_memory_application()
    _clinic(app)
    _waiting_patient(app, phone="")
    decision_id = _gap_fill_decision(app)
    sender = _RecordingSender()

    result = _dashboard(app, sender).resolve_decision("doctor", decision_id, "approve")

    assert result["outcome"] == "approved"
    assert result["notified"] == (
        "No mobile number on file — please contact them directly."
    )
    assert sender.messages == [], "an unparseable or absent number must not be texted"


def test_dismissing_a_gap_fill_texts_nobody() -> None:
    # Dismiss books nothing, so there is nothing to announce. Texting here would tell
    # a patient about an appointment that does not exist.
    app = build_memory_application()
    _clinic(app)
    _waiting_patient(app)
    decision_id = _gap_fill_decision(app)
    sender = _RecordingSender()

    result = _dashboard(app, sender).resolve_decision("doctor", decision_id, "dismiss")

    assert result["outcome"] == "dismissed"
    assert result["notified"] is None
    assert sender.messages == []


def test_an_advisory_decision_texts_nobody() -> None:
    # Only gap_fill books a person. The other kinds are advice to the doctor, and a
    # patient must never hear about one.
    app = build_memory_application()
    _clinic(app)
    created = app.stores.decisions.create(
        Decision(
            id="dec-advisory",
            kind=DecisionKind.NO_SHOW_TREND,
            finding_key="no_show_trend#2026-09",
            summary="No-shows are up on the preceding period.",
            recommended_action="Consider reminders.",
            supporting_record_count=6,
            status=DecisionStatus.OPEN,
            generated_at=f"{DAY}T09:00:00Z",
        )
    )
    assert is_ok(created)
    sender = _RecordingSender()

    result = _dashboard(app, sender).resolve_decision("doctor", "dec-advisory", "approve")

    assert result["outcome"] == "approved"
    assert result["notified"] is None
    assert sender.messages == []
