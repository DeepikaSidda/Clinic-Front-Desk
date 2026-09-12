"""Time the availability lookup the agent makes on a call, across the new range.

Publishing the rest of the year multiplies the open-slot partition by a hundred, and
the agent asks that partition a question mid-sentence. If the answer takes eight
seconds the caller hears silence and the model is tempted to answer from its own
guess instead of the calendar — which is the failure this whole system exists to
avoid.

So this measures the real tool, ``check_availability``, on dates spread through the
published range.

    python scripts/check_availability_speed.py
    python scripts/check_availability_speed.py --day 2026-12-24
"""

from __future__ import annotations

import argparse
import time

from clinic_front_desk.deployment.runtime import build_runtime_application
from clinic_front_desk.deployment.server import runtime_config_from_env
from clinic_front_desk.models import is_err
from clinic_front_desk.tools.availability import check_availability

#: Spread through the published range: today, next week, then each month end.
DEFAULT_DAYS = (
    "2026-09-12",
    "2026-09-15",
    "2026-09-30",
    "2026-10-31",
    "2026-11-30",
    "2026-12-31",
)

SERVICE = "ENT Consultation"
PROVIDER = "prov-raana"

#: Anything past this and a caller hears dead air before the agent answers.
BUDGET_SECONDS = 3.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", action="append", default=None)
    parser.add_argument("--service", default=SERVICE)
    args = parser.parse_args()

    days = tuple(args.day) if args.day else DEFAULT_DAYS

    app = build_runtime_application(runtime_config_from_env())
    store = app.stores.appointments

    print(f"  service {args.service}   budget {BUDGET_SECONDS}s per lookup")
    print()
    slow: list[str] = []
    for day in days:
        started = time.perf_counter()
        result = check_availability(
            store,
            service=args.service,
            provider_ids=[PROVIDER],
            from_date=day,
        )
        elapsed = time.perf_counter() - started
        if is_err(result):
            print(f"  FAIL {day}  {elapsed:5.2f}s  {result.error}")
            slow.append(day)
            continue
        offered = result.value
        count = len(offered) if hasattr(offered, "__len__") else 0
        first = ""
        if count:
            candidate = offered[0]
            first = getattr(candidate, "start", "") or str(candidate)
        flag = "" if elapsed <= BUDGET_SECONDS else "  <-- over budget"
        print(f"  {'ok  ' if not flag else 'SLOW'} {day}  {elapsed:5.2f}s  "
              f"{count} offered  first {first}{flag}")
        if flag:
            slow.append(day)

    print()
    if slow:
        print(f"  over budget on: {', '.join(slow)}")
        raise SystemExit(1)
    print("  every lookup inside budget")


if __name__ == "__main__":
    main()
