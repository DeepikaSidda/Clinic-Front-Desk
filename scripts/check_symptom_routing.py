"""What the agent answers when a caller describes a problem, against live config.

Reads the doctor's routing from the real table and shows the decision for a set of
descriptions — including the ones that must NOT match, because the restraint is the
part worth checking. A rule that fires too eagerly is worse than one that never fires:
an unmatched problem goes to a human, while a wrongly matched one books an
appointment the caller did not need.

    python scripts/check_symptom_routing.py
    python scripts/check_symptom_routing.py --say "my ear is blocked"
"""

from __future__ import annotations

import argparse
import os

import boto3

from clinic_front_desk.data_layer.dynamodb.clinic_knowledge_base_store import (
    DynamoClinicKnowledgeBaseStore,
)
from clinic_front_desk.models import is_err
from clinic_front_desk.tools.symptom_router import RouteMatch, route_described_problem

TABLE = os.environ.get("CLINIC_TABLE_NAME", "clinic-front-desk")
REGION = os.environ.get("AWS_REGION", "us-east-1")

#: (what the caller says, what should happen)
CASES: tuple[tuple[str, str], ...] = (
    ("actually there is some kind of itching in nose", "ENT Consultation"),
    ("my nose is blocked since two days", "ENT Consultation"),
    ("I have ear pain", "ENT Consultation"),
    ("there is ringing in my ears", "ENT Consultation"),
    ("I cannot hear properly on the left side", "Hearing Test"),
    ("sore throat for a week", "ENT Consultation"),
    ("I have sudden hearing loss since this morning", "URGENT"),
    ("something stuck in my throat", "URGENT"),
    # Must not match. These are the important ones.
    ("I want a hearing test", "no match"),
    ("sharp pain in my jaw", "no match"),
    ("my knee hurts", "no match"),
    ("I think I have a fever", "no match"),
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--say", action="append", help="an extra description to try")
    args = parser.parse_args()

    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    result = DynamoClinicKnowledgeBaseStore(table).get()
    if is_err(result) or result.value is None:
        raise SystemExit("the clinic is not configured")
    kb = result.value
    print(f"  {len(kb.symptom_routes)} route(s) live in {TABLE}\n")

    cases = CASES + tuple((s, "?") for s in (args.say or []))
    wrong = 0
    for said, expected in cases:
        outcome = route_described_problem(kb, said)
        if isinstance(outcome, RouteMatch):
            actual = "URGENT" if outcome.urgent else outcome.service
        else:
            actual = "no match"

        ok = expected in ("?", actual)
        if not ok:
            wrong += 1
        mark = "ok  " if ok else "WRONG"
        print(f"  {mark} {said!r}")
        print(f"        -> {actual}" + ("" if expected == "?" else f"   (expected {expected})"))
        if isinstance(outcome, RouteMatch):
            said_aloud = outcome.urgent_instruction if outcome.urgent else outcome.advice
            if said_aloud:
                print(f"        says: {said_aloud[:96]}")
        else:
            print("        says: nothing — hands it to a human")
        print()

    if wrong:
        print(f"  {wrong} case(s) routed differently than expected")
        raise SystemExit(1)
    print("  Every case routed as intended, including the ones that must not match.")


if __name__ == "__main__":
    main()
