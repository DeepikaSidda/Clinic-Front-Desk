"""Run the dashboard locally against seeded in-memory data.

A one-command way to see the whole UI with realistic content: clinic config, a
week of appointments (including no-shows so the metrics have a trend), open slots,
a waitlist, completed calls, an escalation, and open Decisions to approve/dismiss.

    python scripts/demo_dashboard.py
    # then open http://127.0.0.1:8080/?role=doctor

Everything is in-memory, so nothing is persisted and no AWS credentials are
needed for the dashboard. (A voice call on /ws would still need Bedrock.)

Roles to try:
    ?role=doctor     - every view, including the Decisions feed
    ?role=assistant  - schedule + call activity only (Req 15.5)
    (no role)        - access denied, no data regions at all (Req 15.7)
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

from clinic_front_desk.deployment.app import build_memory_application
from clinic_front_desk.deployment.server import create_asgi_app
from clinic_front_desk.models import (
    Appointment,
    AppointmentStatus,
    CallOutcome,
    CallSession,
    ClinicKnowledgeBase,
    DayHours,
    Decision,
    DecisionKind,
    DecisionStatus,
    Escalation,
    EscalationReason,
    PatientRef,
    Provider,
    ServiceConfig,
    Slot,
    SlotStatus,
    WaitlistEntry,
)

PROVIDER = "prov-reyes"
SERVICES = ("Hearing Test", "Sinus Consultation", "Allergy Screening")


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _gap_fill_slot_label(now: datetime) -> str:
    """Clock time of the first seeded Hearing Test slot (``now`` + 45 minutes)."""
    return (now + timedelta(minutes=45)).replace(second=0, microsecond=0).strftime("%H:%M")


def seed(app: object) -> None:
    """Populate the shared Data_Layer with a realistic demo clinic."""
    stores = app.stores  # type: ignore[attr-defined]
    now = datetime.now(UTC)
    today = now.date()

    saved = app.save_config(  # type: ignore[attr-defined]
        ClinicKnowledgeBase(
            location="118 Harbour Road, Suite 4",
            hours={day: DayHours(open="09:00", close="17:00") for day in range(1, 6)},
            services=[
                ServiceConfig(
                    name="Hearing Test",
                    price=180.0,
                    prep_instructions="Avoid loud noise for 12 hours beforehand.",
                ),
                ServiceConfig(
                    name="Sinus Consultation",
                    price=240.0,
                    prep_instructions="Bring a list of current medications.",
                ),
                ServiceConfig(
                    name="Allergy Screening",
                    price=310.0,
                    prep_instructions="Stop antihistamines 72 hours beforehand.",
                ),
            ],
            providers=[
                Provider(id=PROVIDER, name="Dr. Amara Reyes", specialty="ENT"),
            ],
            accepted_insurance=["Acme Health", "Northwind Care", "Self-pay"],
        )
    )
    if not saved.ok:
        raise SystemExit(f"demo config rejected: {saved.validation}")

    # --- Today's schedule: booked appointments + open slots ---------------
    booked = [
        ("09:00", "Hearing Test", "pat-ellis", AppointmentStatus.BOOKED),
        ("10:30", "Sinus Consultation", "pat-nakamura", AppointmentStatus.BOOKED),
        ("13:00", "Allergy Screening", "pat-okafor", AppointmentStatus.COMPLETED),
        ("15:30", "Hearing Test", "pat-silva", AppointmentStatus.NO_SHOW),
    ]
    for i, (time_str, service, patient, status) in enumerate(booked):
        stores.appointments.create(
            Appointment(
                id=f"appt-today-{i}",
                provider_id=PROVIDER,
                patient_id=patient,
                service=service,
                slot_id=f"slot-today-{i}",
                date=today.isoformat(),
                time=time_str,
                status=status,
            )
        )

    # Open slots are seeded *relative to now*, not at fixed clock times, and every
    # service gets several. check_availability never offers a slot that has already
    # started, so fixed times meant the demo ran out of bookable slots as the day
    # went on — after midday a caller asking to book was only ever offered the
    # waitlist, which looks like a broken agent rather than an empty calendar.
    slot_index = 0
    for service in SERVICES:
        for offset in (
            timedelta(minutes=45),
            timedelta(hours=2),
            timedelta(hours=4),
            timedelta(days=1, hours=1),
            timedelta(days=2, hours=3),
        ):
            start = (now + offset).replace(second=0, microsecond=0)
            # seed_slot is the in-memory store's setup hook (slots are created by
            # the provider's calendar, not by the booking interface).
            stores.appointments.seed_slot(
                Slot(
                    id=f"slot-open-{slot_index}",
                    provider_id=PROVIDER,
                    service=service,
                    start=_iso(start),
                    end=_iso(start + timedelta(minutes=45)),
                    status=SlotStatus.OPEN,
                )
            )
            slot_index += 1

    # --- History across the metrics windows -------------------------------
    # A mix of completed and no-show appointments over the last 60 days so the
    # no-show rate has a real value *and* a comparable preceding period.
    for day_offset in range(1, 61):
        day = today - timedelta(days=day_offset)
        # Heavier no-show rate in the older period so the trend reads "improving".
        no_show = day_offset % (4 if day_offset > 30 else 7) == 0
        stores.appointments.create(
            Appointment(
                id=f"appt-hist-{day_offset}",
                provider_id=PROVIDER,
                patient_id=f"pat-hist-{day_offset % 12}",
                service=SERVICES[day_offset % len(SERVICES)],
                slot_id=f"slot-hist-{day_offset}",
                date=day.isoformat(),
                time="10:00",
                status=AppointmentStatus.NO_SHOW if no_show else AppointmentStatus.COMPLETED,
            )
        )

    # --- Handled calls (drive front-desk hours saved) ---------------------
    outcomes = [
        CallOutcome.BOOKED,
        CallOutcome.RESCHEDULED,
        CallOutcome.CANCELLED,
        CallOutcome.BOOKED,
        CallOutcome.WAITLISTED,
        CallOutcome.BOOKED,
    ]
    names = [
        "Dana Ellis",
        "Kenji Nakamura",
        "Chidi Okafor",
        "Marta Silva",
        "Rowan Pierce",
        "Iris Lindqvist",
    ]
    for i in range(48):
        started = now - timedelta(hours=6 * i + 1)
        stores.call_sessions.create(
            CallSession(
                id=f"call-{i}",
                started_at=_iso(started),
                ended_at=_iso(started + timedelta(minutes=4)),
                outcome=outcomes[i % len(outcomes)],
                patient_ref=PatientRef(
                    name=names[i % len(names)],
                    callback_phone=f"555-01{i:02d}",
                ),
            )
        )

    # --- One escalation, so the activity log shows the flagged row --------
    stores.escalations.create(
        Escalation(
            id="esc-1",
            reason=EscalationReason.CLINICAL_CONTENT,
            call_session_id="call-3",
            context="Caller described symptoms and asked whether they need surgery.",
            created_at=_iso(now - timedelta(minutes=25)),
            patient_ref=PatientRef(name="Marta Silva", callback_phone="555-0103"),
        )
    )

    # --- Waitlist (gap-fill candidates) ----------------------------------
    for i, (patient, service) in enumerate(
        [
            ("pat-pierce", "Hearing Test"),
            ("pat-lindqvist", "Hearing Test"),
            ("pat-mbeki", "Sinus Consultation"),
        ]
    ):
        stores.waitlist.add(
            WaitlistEntry(
                id=f"wl-{i}",
                patient_id=patient,
                service=service,
                preferred_slot_type="any",
                added_at=_iso(now - timedelta(days=3 - i)),
                # Monotonic tiebreaker for equal added_at (the store's ordering
                # invariant, Req 7.3).
                seq=i,
            )
        )

    # --- Open Decisions for the feed --------------------------------------
    stores.decisions.create(
        Decision(
            id="dec-gap-fill",
            kind=DecisionKind.GAP_FILL,
            # slot-open-0 is the first Hearing Test slot seeded above, 45 minutes
            # out. The wording is derived from it rather than hardcoded so the
            # Decision never describes a time the calendar does not have.
            finding_key="gap_fill:slot-open-0",
            summary=(
                f"A {_gap_fill_slot_label(now)} Hearing Test slot is unfilled "
                "while 2 patients wait."
            ),
            recommended_action=(
                "Book Rowan Pierce (waiting since "
                f"{(today - timedelta(days=3)).isoformat()}) into the "
                f"{_gap_fill_slot_label(now)} slot."
            ),
            generated_at=_iso(now - timedelta(minutes=8)),
            supporting_record_count=7,
            action_payload={"slot_id": "slot-open-0"},
        )
    )
    stores.decisions.create(
        Decision(
            id="dec-no-show",
            kind=DecisionKind.NO_SHOW_TREND,
            finding_key="no_show_trend:Hearing Test",
            summary="Hearing Test no-shows are concentrated in late-afternoon slots.",
            recommended_action=(
                "Consider a reminder call 24 hours ahead for slots after 15:00."
            ),
            generated_at=_iso(now - timedelta(hours=3)),
            supporting_record_count=9,
        )
    )
    stores.decisions.create(
        Decision(
            id="dec-unmet-demand",
            kind=DecisionKind.UNOFFERED_SERVICE_DEMAND,
            finding_key="unoffered_service:tinnitus assessment",
            summary="6 callers asked for a tinnitus assessment, which is not offered.",
            recommended_action="Evaluate adding a tinnitus assessment service.",
            generated_at=_iso(now - timedelta(days=1)),
            supporting_record_count=6,
        )
    )

    # An already-approved gap-fill so the waitlist-recovered metric is non-zero
    # (this is the read that DecisionStore.list_by_status exists to serve).
    stores.decisions.create(
        Decision(
            id="dec-recovered",
            kind=DecisionKind.GAP_FILL,
            finding_key="gap_fill:slot-recovered",
            summary="Recovered a cancelled slot from the waitlist.",
            recommended_action="Booked the earliest waiting patient.",
            generated_at=_iso(now - timedelta(days=2)),
            supporting_record_count=5,
        )
    )
    stores.decisions.set_status(
        "dec-recovered", DecisionStatus.APPROVED, _iso(now - timedelta(days=2))
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    import uvicorn

    # Recording on, into the in-memory store, so the whole capture → render →
    # store → play-back path is exercised locally without an S3 bucket. Nothing
    # leaves the process and everything is lost on exit.
    app = build_memory_application(record_calls=True)
    seed(app)
    asgi = create_asgi_app(app)

    print(f"\n  Dashboard  http://{args.host}:{args.port}/?role=doctor")
    print(f"  Assistant  http://{args.host}:{args.port}/?role=assistant")
    print(f"  No role    http://{args.host}:{args.port}/            (access denied)")
    print(f"  Onboarding http://{args.host}:{args.port}/onboarding")
    print(f"  Speak      http://{args.host}:{args.port}/voice")
    print("\n  Calls are recorded + transcribed into the in-memory store; fetch one")
    print(f"  with  GET http://{args.host}:{args.port}/dashboard/calls/<id>?role=doctor\n")

    uvicorn.run(asgi, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
