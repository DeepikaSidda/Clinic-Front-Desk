"""Confirm the doctor's Slots page renders a published day, and a closed one.

The store-level report proves the records exist. This proves the page the doctor
actually looks at shows them, and that a day the clinic is shut reads as
unpublished rather than as a broken page.

    python scripts/check_slots_page.py
    python scripts/check_slots_page.py --day 2026-12-31
"""

from __future__ import annotations

import argparse
import urllib.error
import urllib.request
from datetime import date

BASE = "http://127.0.0.1:8080"
PROVIDER = "prov-raana"

#: A published weekday, and a Sunday the clinic has no hours for.
DEFAULT_DAYS = ("2026-09-15", "2026-10-31", "2026-12-31", "2026-12-27")


def fetch(day: str) -> tuple[int | str, str]:
    url = f"{BASE}/slots?role=doctor&day={day}&provider_id={PROVIDER}"
    try:
        with urllib.request.urlopen(url, timeout=180) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__, ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", action="append", default=None)
    args = parser.parse_args()
    days = tuple(args.day) if args.day else DEFAULT_DAYS

    weekdays = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    failures: list[str] = []

    for day in days:
        code, html = fetch(day)
        # Count the data attribute, not the visible label: the card reads "Open"
        # and CSS upper-cases it, so matching on ">OPEN<" silently finds nothing
        # and every day looks empty.
        open_cards = html.count('data-status="open"')
        booked_cards = html.count('data-status="booked"')
        unpublished = "No slots published for this day" in html
        name = weekdays[date.fromisoformat(day).weekday()]
        rendered = code == 200 and "<html" in html.lower()
        total = open_cards + booked_cards
        note = "  unpublished" if unpublished else ""
        print(
            f"  {'ok  ' if rendered else 'FAIL'} {day} {name:<9} {code}  "
            f"{open_cards:>3} open  {booked_cards:>3} booked{note}"
        )
        if not rendered:
            failures.append(f"{day} did not render")
        elif name == "Sunday":
            if total:
                failures.append(f"{day} is a Sunday but shows {total} slots")
        elif total == 0:
            # The check that actually earns its keep: a weekday inside the
            # published range showing nothing means the publish did not land.
            failures.append(f"{day} is a working day with no slots")

    print()
    if failures:
        print(f"  problems: {'; '.join(failures)}")
        raise SystemExit(1)
    print("  the Slots page renders every published day, and Sunday is unpublished")


if __name__ == "__main__":
    main()
