"""Publish a provider's bookable slots across a date range.

The same operation the doctor's Slots page performs, for a range too long to click
through one day at a time. It goes through ``generate_range_slots`` and
``AppointmentStore.add_slots`` — the real publish path — so the rules hold:

*   **Slot ids are derived from day, provider and start time**, so running this
    twice does not create a second set of duplicate slots beside the first.
*   **Booked and blocked slots are preserved.** Republishing a day that already has
    appointments on it will not reopen them. Reopening a booked slot would leave a
    patient holding an appointment on a slot advertising itself as free.
*   **Days the clinic has no configured hours for are skipped** unless you pass
    ``--include-closed``, so publishing the rest of the year does not offer
    appointments on the days the clinic is shut.

    python scripts/publish_slots.py --until 2026-12-31 --dry-run
    python scripts/publish_slots.py --until 2026-12-31
    python scripts/publish_slots.py --from 2026-10-01 --until 2026-10-31 --start 09:00 --end 20:00

Defaults match the day already published in the portal: 09:00 to 20:00 in
30-minute slots, which is 22 slots a day.
"""

from __future__ import annotations

import argparse
import os
from datetime import UTC, date, datetime, timedelta

from clinic_front_desk.deployment.runtime import build_runtime_application
from clinic_front_desk.deployment.server import runtime_config_from_env
from clinic_front_desk.models import SlotStatus, is_err
from clinic_front_desk.scheduling import SlotGenerationError, generate_range_slots

#: Matches the published day in the portal: 09:00-19:30 inclusive, 22 slots.
DEFAULT_START = "09:00"
DEFAULT_END = "20:00"
DEFAULT_MINUTES = 30

#: Slots are published generically and a caller is offered the time for whatever
#: service they ask for, so one service name carries the whole calendar.
DEFAULT_SERVICE = "ENT Consultation"
DEFAULT_PROVIDER = "prov-raana"

_WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


def today_iso() -> str:
    return datetime.now(UTC).date().isoformat()


def year_end(from_day: str) -> str:
    return f"{from_day[:4]}-12-31"


def report(
    store: object,
    provider: str,
    first: str,
    last: str,
    open_weekdays: frozenset[int],
) -> None:
    """Read the calendar back and say what is actually there.

    Checks the two things that could silently be wrong: a day inside the range with
    no slots at all, and a day the clinic is shut carrying slots anyway.
    """
    begins = date.fromisoformat(first)
    ends = date.fromisoformat(last)

    empty: list[str] = []
    on_closed_days: list[str] = []
    totals = {status: 0 for status in ("open", "booked", "blocked", "other")}
    day_counts: dict[str, int] = {}

    day = begins
    while day <= ends:
        iso = day.isoformat()
        # The clinic's convention is Sunday=0; Python's weekday() is Monday=0.
        weekday = (day.weekday() + 1) % 7
        result = store.list_slots_for_day(provider, iso)  # type: ignore[attr-defined]
        slots = [] if is_err(result) else result.value
        day_counts[iso] = len(slots)

        if not slots:
            if weekday in open_weekdays:
                empty.append(iso)
        elif weekday not in open_weekdays:
            on_closed_days.append(iso)

        for slot in slots:
            if slot.status == SlotStatus.OPEN:
                totals["open"] += 1
            elif slot.status == SlotStatus.BOOKED:
                totals["booked"] += 1
            elif slot.status == SlotStatus.BLOCKED:
                totals["blocked"] += 1
            else:
                totals["other"] += 1
        day = day + timedelta(days=1)

    published = [iso for iso, count in day_counts.items() if count]
    total = sum(day_counts.values())
    print(f"  range        {first} to {last}")
    print(f"  days with slots  {len(published)} of {len(day_counts)} calendar days")
    print(f"  slots        {total} total")
    print(f"    open       {totals['open']}")
    print(f"    booked     {totals['booked']}")
    print(f"    blocked    {totals['blocked']}")
    if totals["other"]:
        print(f"    other      {totals['other']}")

    sizes = sorted({count for count in day_counts.values() if count})
    print(f"  slots per published day  {sizes if len(sizes) <= 6 else f'{sizes[:3]}...{sizes[-3:]}'}")

    print()
    if empty:
        print(f"  {len(empty)} open day(s) with NO slots: {', '.join(empty[:8])}")
    else:
        print("  ok  every day the clinic is open has slots")
    if on_closed_days:
        print(
            f"  {len(on_closed_days)} slot(s) on days the clinic is shut: "
            f"{', '.join(on_closed_days[:8])}"
        )
    else:
        print("  ok  no slots on days the clinic has no hours for")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="first", default="", help="ISO date; default today")
    parser.add_argument("--until", default="", help="ISO date; default 31 Dec of that year")
    parser.add_argument("--provider", default=DEFAULT_PROVIDER)
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument("--start", default=DEFAULT_START, help="first slot, HH:MM")
    parser.add_argument("--end", default=DEFAULT_END, help="exclusive end, HH:MM")
    parser.add_argument("--minutes", type=int, default=DEFAULT_MINUTES)
    parser.add_argument(
        "--include-closed",
        action="store_true",
        help="publish every weekday, even ones the clinic has no hours for",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--report",
        action="store_true",
        help="do not publish; report what is actually on the calendar for the range",
    )
    args = parser.parse_args()

    first = args.first.strip() or today_iso()
    last = args.until.strip() or year_end(first)

    app = build_runtime_application(runtime_config_from_env())
    store = app.stores.appointments

    kb_result = app.stores.knowledge_base.get()
    kb = kb_result.value if not is_err(kb_result) else None
    if kb is None:
        raise SystemExit("the clinic is not configured; publish from the portal first")

    offered = [service.name for service in kb.services]
    if args.service not in offered:
        raise SystemExit(
            f"{args.service!r} is not an offered service.\n  offered: {', '.join(offered)}"
        )

    open_weekdays = frozenset(day for day, hours in kb.hours.items() if hours is not None)

    if args.report:
        report(store, args.provider, first, last, open_weekdays)
        return

    if not open_weekdays and not args.include_closed:
        raise SystemExit(
            "the clinic has no configured opening hours, so every day would be "
            "skipped. Set hours first, or pass --include-closed."
        )

    print(f"  provider   {args.provider}")
    print(f"  service    {args.service}")
    print(f"  window     {args.start} to {args.end}, {args.minutes}-minute slots")
    print(f"  range      {first} to {last}")
    if args.include_closed:
        print("  open days  every day (--include-closed)")
    else:
        names = ", ".join(_WEEKDAYS[day] for day in sorted(open_weekdays))
        print(f"  open days  {names}")
        shut = [_WEEKDAYS[d] for d in range(7) if d not in open_weekdays]
        if shut:
            print(f"  skipping   {', '.join(shut)} (no configured hours)")

    try:
        slots = generate_range_slots(
            first,
            last,
            args.provider,
            args.service,
            minutes=args.minutes,
            start=args.start,
            end=args.end,
            open_weekdays=None if args.include_closed else open_weekdays,
        )
    except SlotGenerationError as exc:
        raise SystemExit(f"  cannot publish: {exc}") from exc

    days = sorted({slot.start[:10] for slot in slots})
    per_day = len(slots) // len(days) if days else 0
    print()
    print(f"  {len(slots)} slots over {len(days)} days ({per_day} per day)")

    if args.dry_run:
        print("  dry run; nothing written")
        print(f"  first day {days[0] if days else '-'}   last day {days[-1] if days else '-'}")
        return

    result = store.add_slots(slots)
    if is_err(result):
        raise SystemExit(f"  failed: {result.error.detail}")

    written = len(result.value)
    preserved = len(slots) - written
    print(f"  wrote {written} slots")
    if preserved:
        print(
            f"  left {preserved} untouched — already booked or blocked, and "
            "republishing must not reopen them"
        )


if __name__ == "__main__":
    main()
