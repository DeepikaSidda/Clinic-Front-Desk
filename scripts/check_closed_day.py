"""Show what the agent is told when a caller asks for a closed day.

Runs the real ``check_availability`` tool against the live table and prints the
payload, so the closed-day notice can be read exactly as the model receives it.

    python scripts/check_closed_day.py
    python scripts/check_closed_day.py --day 2026-09-13
"""

from __future__ import annotations

import argparse
import json
from datetime import date

from clinic_front_desk.deployment.runtime import build_runtime_application
from clinic_front_desk.deployment.server import runtime_config_from_env
from clinic_front_desk.voice.agent import BoundToolset, build_patient_facing_tools

SERVICE = "ENT Consultation"
PROVIDER = "prov-raana"

#: A Sunday the clinic is shut, the Monday after it, and a normal weekday.
DEFAULT_DAYS = ("2026-09-13", "2026-09-14", "2026-09-15", "2026-12-27")

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", action="append", default=None)
    args = parser.parse_args()
    days = tuple(args.day) if args.day else DEFAULT_DAYS

    app = build_runtime_application(runtime_config_from_env())
    stores = app.stores.voice_stores()
    toolset = BoundToolset(stores=stores)
    tools = build_patient_facing_tools(stores, toolset=toolset)

    tool = tools["check_availability"]
    func = getattr(tool, "__wrapped__", None) or getattr(tool, "func", None) or tool

    open_days = sorted(toolset.open_weekdays())
    print(f"  clinic open on weekday indices (Sunday=0): {open_days}")
    print()

    for day in days:
        name = WEEKDAYS[date.fromisoformat(day).weekday()]
        payload = func(service=SERVICE, from_date=day, provider_id=PROVIDER)
        closed = payload.get("clinic_closed_on_requested_date", False)
        slots = payload.get("value") or []
        offered = ", ".join(getattr(s, "start", str(s)) for s in slots[:3])
        print(f"  {day} ({name})")
        print(f"    closed flag : {closed}")
        if closed:
            print(f"    notice      : {payload['closed_notice']}")
        print(f"    offered     : {offered or '(none)'}")
        print()


if __name__ == "__main__":
    main()
