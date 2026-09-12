"""Fill a day with synthetic bookings, leaving a few slots open.

For demonstrating a realistic clinic day: a calendar that is mostly full, with a
handful of gaps scattered through it the way a real day has them, rather than 22
identical empty slots.

Bookings go through the same tools the voice agent uses — ``register_patient``
then ``book_appointment`` — rather than writing records directly. So this also
exercises the real path: if it works here it works on a call, and a slot that
cannot legitimately be booked will not be booked by this either.

    python scripts/seed_demo_day.py --day 2026-09-12
    python scripts/seed_demo_day.py --day 2026-09-12 --leave-open 5
    python scripts/seed_demo_day.py --day 2026-09-12 --dry-run
    python scripts/seed_demo_day.py --day 2026-09-12 --undo

THE DATA IS INVENTED. These are not real patients. Everything written carries a
recognisable marker so it can be found and removed later: every mobile number
begins ``99000``, which is not an allocated Indian mobile prefix, and ``--undo``
removes exactly the appointments this script created for that day.
"""

from __future__ import annotations

import argparse

from clinic_front_desk.deployment.runtime import build_runtime_application
from clinic_front_desk.deployment.server import runtime_config_from_env
from clinic_front_desk.models import SlotStatus, is_err
from clinic_front_desk.voice.agent import BoundToolset

#: Marker prefix on every synthetic mobile number. Deliberately not a real
#: allocated Indian mobile prefix, so seeded records can never be confused with a
#: patient who actually rang and cannot be dialled by mistake.
FAKE_PREFIX = "99000"

#: Used only when a caller's service cannot be determined.
DEFAULT_SERVICE = "ENT Consultation"

#: Synthetic patients, in the order they fill the day: name, phone tail, age,
#: blood group, weight, height, and **the service that patient asked for**.
#:
#: The service varies per patient on purpose. A slot is generic — the doctor's
#: half hour, published without knowing who will take it — but an appointment is
#: not. It is one named person coming in for one named thing, and the whole point
#: of the doctor's diary is telling them which. A day where all seventeen rows say
#: "ENT Consultation" tells them nothing they could not already see from the clock.
#:
#: Names, ages and measurements are plausible for a Tirupati ENT clinic; none of
#: them are real.
PATIENTS: tuple[tuple[str, str, int, str, float, float, str], ...] = (
    ("Lakshmi Prasad", "12301", 42, "O positive", 68.0, 162.0, "Hearing Test"),
    ("Venkatesh Reddy", "12302", 35, "B positive", 74.5, 172.0, "Sinus Treatment"),
    ("Anitha Rao", "12303", 29, "A positive", 55.0, 158.0, "ENT Consultation"),
    ("Suresh Babu", "12304", 51, "O negative", 81.0, 170.0, "Hearing Test"),
    ("Padmavathi Naidu", "12305", 64, "AB positive", 62.5, 152.0, "Hearing Test"),
    ("Kiran Kumar", "12306", 27, "B negative", 70.0, 176.0, "Sinus Treatment"),
    ("Sailaja Devi", "12307", 38, "A positive", 58.0, 160.0, "ENT Consultation"),
    ("Ramesh Chandra", "12308", 46, "O positive", 77.0, 168.0, "Sinus Treatment"),
    ("Bhavani Sankar", "12309", 33, "B positive", 66.0, 165.0, "Hearing Test"),
    ("Divya Sree", "12310", 24, "A negative", 51.0, 155.0, "ENT Consultation"),
    ("Mohan Krishna", "12311", 58, "O positive", 84.0, 174.0, "Sinus Treatment"),
    ("Jyothi Lakshmi", "12312", 41, "AB negative", 60.0, 157.0, "Hearing Test"),
    ("Srinivasa Rao", "12313", 69, "B positive", 72.0, 166.0, "Hearing Test"),
    ("Harika Reddy", "12314", 31, "O positive", 57.5, 161.0, "Sinus Treatment"),
    ("Naveen Chowdary", "12315", 22, "A positive", 65.0, 178.0, "ENT Consultation"),
    ("Vijaya Kumari", "12316", 55, "O negative", 69.0, 154.0, "Hearing Test"),
    ("Prakash Rao", "12317", 47, "B positive", 79.5, 171.0, "Sinus Treatment"),
    ("Meenakshi Amma", "12318", 72, "A positive", 54.0, 149.0, "Hearing Test"),
    ("Chaitanya Varma", "12319", 30, "AB positive", 73.0, 175.0, "ENT Consultation"),
    ("Radha Krishnan", "12320", 61, "O positive", 67.0, 163.0, "Sinus Treatment"),
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", required=True, help="ISO date, e.g. 2026-09-12")
    parser.add_argument(
        "--leave-open",
        type=int,
        default=5,
        help="how many slots to leave bookable (default 5)",
    )
    parser.add_argument("--provider", default="prov-raana")
    parser.add_argument(
        "--service",
        default="",
        help=(
            "force every booking onto one service. Left empty (the default) each "
            "patient keeps the service they asked for, which is what the doctor "
            "needs to see."
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--undo",
        action="store_true",
        help="cancel every synthetic booking on that day and reopen the slots",
    )
    args = parser.parse_args()

    app = build_runtime_application(runtime_config_from_env())
    store = app.stores.appointments
    toolset = BoundToolset(stores=app.stores.voice_stores())

    if args.undo:
        _undo(app, args.provider, args.day, dry_run=args.dry_run)
        return

    day_slots = store.list_slots_for_day(args.provider, args.day)
    if is_err(day_slots):
        raise SystemExit(f"could not read {args.day}: {day_slots.error.detail}")

    openable = sorted(
        (s for s in day_slots.value if s.status == SlotStatus.OPEN),
        key=lambda s: s.start,
    )
    if not openable:
        raise SystemExit(
            f"{args.day} has no open slots. Publish the day first, and check the "
            "working hours have not blocked it."
        )

    to_fill = max(0, len(openable) - max(0, args.leave_open))
    if to_fill == 0:
        print(f"  {args.day}: {len(openable)} open already at or below the target.")
        return
    if to_fill > len(PATIENTS):
        raise SystemExit(
            f"{to_fill} bookings needed but only {len(PATIENTS)} synthetic patients "
            "are defined. Raise --leave-open or add more."
        )

    # Leave the gaps *scattered*, not bunched at the end of the day. A real diary
    # has holes in the middle where someone cancelled; five in a row at 5pm reads
    # as a calendar nobody has used.
    step = len(openable) / (args.leave_open + 1) if args.leave_open else 0
    keep_open = {
        openable[min(len(openable) - 1, round(step * (n + 1)))].id
        for n in range(args.leave_open)
    }
    targets = [slot for slot in openable if slot.id not in keep_open][:to_fill]

    print(f"  {args.day}: {len(openable)} open now")
    print(f"  service: {args.service or 'as each patient asked for'}")
    print(f"  filling {len(targets)}, leaving {len(openable) - len(targets)} open")
    print("  DATA IS SYNTHETIC — mobile numbers all begin " + FAKE_PREFIX)
    if args.dry_run:
        for slot, person in zip(targets, PATIENTS, strict=False):
            print(f"    would book {slot.start[11:16]}  {person[0]} ({person[6]})")
        gaps = [s.start[11:16] for s in openable if s.id in keep_open]
        print(f"    would leave open: {', '.join(gaps)}")
        return

    booked = 0
    for slot, person in zip(targets, PATIENTS, strict=False):
        name, tail, age, blood, weight, height, wanted = person
        # The caller's own service, unless one was forced on the command line.
        service = args.service or wanted or DEFAULT_SERVICE
        registered = toolset.register_patient(
            name=name,
            callback_phone=f"{FAKE_PREFIX}{tail}",
            age=age,
            blood_group=blood,
            weight_kg=weight,
            height_cm=height,
        )
        if is_err(registered):
            print(f"    ! {name}: {registered.error}")
            continue
        result = toolset.book_appointment(
            provider_id=args.provider,
            patient_id=registered.value.id,
            slot_id=slot.id,
            service=service,
        )
        if is_err(result):
            print(f"    ! {slot.start[11:16]} {name}: {result.error}")
            continue
        booked += 1
        print(f"    {slot.start[11:16]}  {name:18} {service:18} {blood:12} age {age}")

    after = store.list_slots_for_day(args.provider, args.day)
    still_open = (
        0
        if is_err(after)
        else sum(1 for s in after.value if s.status == SlotStatus.OPEN)
    )
    print(f"\n  booked {booked}; {still_open} slots left open on {args.day}")


def _undo(app: object, provider: str, day: str, *, dry_run: bool) -> None:
    """Cancel every synthetic booking on ``day`` and reopen those slots."""
    store = app.stores.appointments  # type: ignore[attr-defined]
    patients = app.stores.patients  # type: ignore[attr-defined]

    appts = store.list_by_provider_and_day(provider, day)
    if is_err(appts):
        raise SystemExit(f"could not read {day}: {appts.error.detail}")

    removed = 0
    for appointment in appts.value:
        found = patients.get(appointment.patient_id)
        if is_err(found) or found.value is None:
            continue
        if not found.value.callback_phone.startswith(FAKE_PREFIX):
            # A real booking. Never touched by this script.
            continue
        if dry_run:
            print(f"    would cancel {appointment.time} {found.value.name}")
            removed += 1
            continue
        result = store.remove(appointment.id)
        if is_err(result):
            print(f"    ! {appointment.id}: {result.error.detail}")
            continue
        removed += 1
        print(f"    cancelled {appointment.time}  {found.value.name}")

    print(f"\n  {'would remove' if dry_run else 'removed'} {removed} synthetic bookings")


if __name__ == "__main__":
    main()
