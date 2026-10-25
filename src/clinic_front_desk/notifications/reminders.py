"""Texting tomorrow's patients, from wherever the clock happens to live.

This logic began as a method on the dashboard, because the only thing that could start
it was the doctor pressing a button. That was an honest limitation and the docstring
said so: nothing on the host ran on a timer, so a reminder advertised as arriving "the
day before" would have been advertising something untrue.

Giving it a timer meant the work had to leave the dashboard. A scheduled run has no
request, no role to authorize, and no HTML to return — it has a table, a sender and a
date. So the loop lives here, over narrow store interfaces, and both callers drive the
same function:

- :meth:`~clinic_front_desk.deployment.dashboard_app.DashboardApp.remind_day` resolves
  the day and the provider from the request, checks the doctor may see the schedule,
  then calls this.
- The scheduled Lambda resolves "tomorrow" in clinic time and calls this.

Keeping one implementation is the point. Two copies of "who still needs telling" would
drift, and the failure mode of drift here is a patient told twice or not at all.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import logging

from clinic_front_desk.data_layer.interfaces import AppointmentStore, PatientStore
from clinic_front_desk.models import AppointmentStatus, ISODate, is_ok
from clinic_front_desk.notifications.sms import SmsSender, reminder_message

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReminderRun:
    """What one pass over a day's booked appointments did.

    ``failed`` counts patients the clinic tried and could not reach, which includes
    those with no number on file. They are counted rather than hidden because a run
    that quietly failed for half a day is worse than no run at all: the clinic then
    believes those patients know.
    """

    day: ISODate
    provider_id: str
    booked: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    #: Set when the day could not be read at all, in which case the counts are zero
    #: and nothing was attempted.
    read_error: str = ""

    @property
    def attempted(self) -> bool:
        return not self.read_error

    def summary(self) -> str:
        """One line a human can act on, for the dashboard notice or a log."""
        if self.read_error:
            return f"Could not read {self.day}: {self.read_error}"
        if not self.booked:
            return f"No booked appointments on {self.day}."
        parts = [f"Reminded {self.sent} of {self.booked} patients on {self.day}."]
        if self.failed:
            parts.append(f"{self.failed} could not be texted — check the day's list.")
        if self.skipped:
            parts.append(f"{self.skipped} had already been reminded.")
        return " ".join(parts)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def remind_booked_on_day(
    *,
    day: ISODate,
    provider_id: str,
    appointments: AppointmentStore,
    patients: PatientStore,
    sms_sender: SmsSender,
    clinic_phone: str = "",
    now: Callable[[], datetime] = _utc_now,
) -> ReminderRun:
    """Text every booked patient on ``day`` who has not been reminded yet.

    Skips anyone already carrying a ``reminded_at`` stamp, which is what makes a second
    run harmless. Without it, a doctor unsure whether she pressed the button presses it
    again — and now a nightly schedule and that button can both fire on the same day,
    so the stamp matters more than it did.

    Never aborts part-way. Each patient is independent, and one unreachable number must
    not stop the rest of the day being told.
    """
    listed = appointments.list_by_provider_and_day(provider_id, day)
    if not is_ok(listed):
        return ReminderRun(
            day=day, provider_id=provider_id, read_error=listed.error.detail
        )

    booked = [
        appointment
        for appointment in listed.value
        if str(getattr(appointment, "status", "")) == AppointmentStatus.BOOKED.value
    ]
    if not booked:
        return ReminderRun(day=day, provider_id=provider_id, booked=0)

    sent = failed = skipped = 0
    for appointment in booked:
        if getattr(appointment, "reminded_at", ""):
            skipped += 1
            continue

        name = ""
        phone = ""
        if appointment.patient_id:
            found = patients.get(appointment.patient_id)
            if is_ok(found) and found.value is not None:
                name = found.value.name
                phone = found.value.callback_phone

        if not phone:
            failed += 1
            # Stamped anyway, with the reason. "No number" is a fact about the record,
            # not a transient error, so re-attempting it every evening would only
            # re-discover it — and a nightly schedule would re-discover it nightly.
            appointments.mark_reminded(
                appointment.id,
                at=now().isoformat(),
                failed="no mobile number on file",
            )
            continue

        outcome = sms_sender.send(
            phone,
            reminder_message(
                patient_name=name,
                service=appointment.service,
                date=appointment.date,
                time=appointment.time,
                clinic_phone=clinic_phone,
            ),
        )
        if outcome.sent:
            sent += 1
        else:
            failed += 1
            logger.warning(
                "reminder for appointment %s was not delivered: %s",
                appointment.id,
                outcome.detail,
            )
        appointments.mark_reminded(
            appointment.id,
            at=now().isoformat(),
            failed="" if outcome.sent else outcome.detail,
        )

    return ReminderRun(
        day=day,
        provider_id=provider_id,
        booked=len(booked),
        sent=sent,
        failed=failed,
        skipped=skipped,
    )


__all__ = ["ReminderRun", "remind_booked_on_day"]
