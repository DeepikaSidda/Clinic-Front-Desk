"""Narrow the published calendar to the clinic's real opening hours.

The calendar was published midnight to midnight, which means the agent can offer
a caller 03:00. This closes everything outside a working window across every
published day, and updates the configured opening hours to match so the agent
does not tell callers one thing and book another.

Slots outside the window are **deleted** by default. Blocking them instead was the
first attempt, and it left the doctor reading past 26 struck-through overnight rows
every morning — noise on the one screen that has to be scannable. Blocking is right
for a lunch hour the doctor wants to see; hours the clinic never opens should not be
on the calendar at all.

Booked slots are never touched, and their presence fails the batch: removing one
would strand a patient holding an appointment on time that no longer exists. Freeing
that time means cancelling their appointment first, deliberately and separately.

    python scripts/set_clinic_hours.py --open 09:00 --close 20:00
    python scripts/set_clinic_hours.py --open 09:00 --close 20:00 --dry-run
    python scripts/set_clinic_hours.py --open 09:00 --close 20:00 --block

Re-runnable, and reversible: republishing a day from the slots portal recreates the
removed slots, because slot ids are derived from the day and time rather than random.
"""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime, timedelta

from clinic_front_desk.deployment.runtime import build_runtime_application
from clinic_front_desk.deployment.server import runtime_config_from_env
from clinic_front_desk.models import DayHours, SlotStatus, is_err

WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


def parse_clock(value: str, *, field: str) -> str:
    parts = value.strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise SystemExit(f"--{field} must be HH:MM, got {value!r}")
    hours, minutes = int(parts[0]), int(parts[1])
    if not (0 <= hours <= 23 and 0 <= minutes < 60):
        raise SystemExit(f"--{field} is not a valid time: {value!r}")
    return f"{hours:02d}:{minutes:02d}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--open", dest="opens", default="09:00", help="first bookable time")
    parser.add_argument("--close", dest="closes", default="20:00", help="last bookable time")
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    parser.add_argument(
        "--block",
        action="store_true",
        help="block the out-of-hours slots instead of deleting them",
    )
    parser.add_argument(
        "--from",
        dest="since",
        default="",
        help="earliest day to tidy (ISO date); defaults to today",
    )
    args = parser.parse_args()

    opens = parse_clock(args.opens, field="open")
    closes = parse_clock(args.closes, field="close")
    if closes <= opens:
        raise SystemExit(f"--close ({closes}) must be after --open ({opens})")

    app = build_runtime_application(runtime_config_from_env())
    store = app.stores.appointments

    config = app.stores.knowledge_base.get()
    if is_err(config) or config.value is None:
        raise SystemExit("the clinic is not configured yet")
    kb = config.value
    providers = [p.id for p in kb.providers if p.id]
    if not providers:
        raise SystemExit("no provider is configured, so there is no calendar to narrow")

    print(f"  window        : {opens} to {closes}")
    print(f"  providers     : {', '.join(providers)}")
    if args.dry_run:
        print("  DRY RUN — nothing will be written\n")

    # Work out how far the calendar runs, so only published days are visited.
    # Defaults to today: there is no point reorganising days that have already
    # happened. `--from` reaches back when an earlier day still needs tidying.
    today = (
        date.fromisoformat(args.since) if args.since else datetime.now(UTC).date()
    )
    spans = []
    for provider_id in providers:
        span = store.open_slot_span(provider_id, today.isoformat())
        if is_err(span):
            raise SystemExit(f"could not read the calendar: {span.error.detail}")
        if span.value is not None:
            spans.append(span.value)
    if not spans:
        raise SystemExit("no open slots are published, so there is nothing to narrow")

    first = min(s.earliest_start for s in spans)[:10]
    last = max(s.latest_start for s in spans)[:10]
    print(f"  published     : {first} to {last}\n")

    blocked_total = 0
    days_touched = 0
    start_day = date.fromisoformat(first)
    end_day = date.fromisoformat(last)

    for offset in range((end_day - start_day).days + 1):
        day = (start_day + timedelta(days=offset)).isoformat()
        for provider_id in providers:
            read = store.list_slots_for_day(provider_id, day)
            if is_err(read):
                raise SystemExit(f"could not read {day}: {read.error.detail}")

            # Anything not booked and outside the window. Blocked ones are included
            # so a day closed by an earlier --block run gets cleaned up too.
            outside = [
                slot
                for slot in read.value
                if slot.status != SlotStatus.BOOKED
                and not (opens <= slot.start.partition("T")[2][:5] < closes)
            ]
            if not outside:
                continue
            days_touched += 1
            blocked_total += len(outside)
            verb = "block" if args.block else "remove"
            if args.dry_run:
                print(f"  {day}: would {verb} {len(outside)} slots outside the window")
                continue
            if args.block:
                written = store.set_slot_statuses(outside, SlotStatus.BLOCKED)
            else:
                written = store.remove_slots(outside)
            if is_err(written):
                raise SystemExit(f"could not {verb} {day}: {written.error.detail}")
            print(f"  {day}: {verb}d {len(outside)} slots outside the window")

    print()
    action = "blocked" if args.block else "removed"
    print(f"  {blocked_total} slots {action} across {days_touched} days")

    # Keep the stated hours and the bookable calendar in agreement. Leaving the
    # configured hours at 00:00-23:59 would have the agent telling callers the
    # clinic is open all night while every one of those slots is closed.
    open_days = sorted(kb.hours)
    if args.dry_run:
        print(
            f"  would set opening hours to {opens}-{closes} on "
            f"{', '.join(WEEKDAYS[d] for d in open_days)}"
        )
        return

    kb.hours = {day: DayHours(open=opens, close=closes) for day in open_days}
    saved = app.save_config(kb)
    if not saved.ok:
        raise SystemExit(f"config rejected: {saved.validation}")
    print(
        f"  opening hours set to {opens}-{closes} on "
        f"{', '.join(WEEKDAYS[d] for d in open_days)}"
    )


if __name__ == "__main__":
    main()
