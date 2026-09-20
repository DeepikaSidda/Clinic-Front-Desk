"""Author the clinic's symptom-to-service routing.

This is the doctor writing down, once, what she would tell a caller who describes a
problem. The agent then relays it. Nothing here is inferred by a model, which is the
entire point: a caller gets a useful answer and the clinical judgement behind it
belongs to a clinician.

Rules are matched in order, so an urgent rule placed above a routine one covering the
same words wins. Urgent rules stop a booking and give the caller the doctor's
instruction instead — the one thing worse than refusing to help is quietly offering
next Tuesday to someone who needs to be seen today.

    python scripts/set_symptom_routes.py --show
    python scripts/set_symptom_routes.py --seed-ent
    python scripts/set_symptom_routes.py --clear

``--seed-ent`` writes a starter set for this ENT clinic. It is a starting point for
the doctor to review, not medical authority — every line should be checked by her
before it answers a real caller.
"""

from __future__ import annotations

import argparse
import os

import boto3

from clinic_front_desk.data_layer.dynamodb.clinic_knowledge_base_store import (
    DynamoClinicKnowledgeBaseStore,
)
from clinic_front_desk.models import SymptomRoute, is_err

TABLE = os.environ.get("CLINIC_TABLE_NAME", "clinic-front-desk")
REGION = os.environ.get("AWS_REGION", "us-east-1")

#: Urgent rules first, so they take precedence over the routine ones below.
ENT_ROUTES: tuple[SymptomRoute, ...] = (
    SymptomRoute(
        phrases=[
            "sudden hearing loss",
            "lost my hearing",
            "cannot hear suddenly",
            "severe bleeding",
            "heavy bleeding",
            "swallowed",
            "something stuck in my throat",
            "cannot breathe",
            "difficulty breathing",
            "severe pain",
        ],
        urgent=True,
        urgent_instruction=(
            "That needs to be looked at today rather than at a booked appointment. "
            "Please come to the clinic during opening hours, and if you cannot, "
            "go to a hospital casualty department."
        ),
    ),
    SymptomRoute(
        phrases=[
            "itching in nose",
            "itch inside my nose",
            "itchy nose",
            "blocked nose",
            "nose block",
            "runny nose",
            "sneezing",
            "sinus pain",
            "smell",
        ],
        service="ENT Consultation",
        advice=(
            "Dr Raana sees nasal and sinus problems under an ENT consultation, and "
            "will decide during the visit whether anything further is needed."
        ),
    ),
    SymptomRoute(
        phrases=[
            "ear pain",
            "ear ache",
            "earache",
            "ear discharge",
            "ear blocked",
            "blocked ear",
            "ringing",
            "buzzing",
            "dizzy",
            "vertigo",
        ],
        service="ENT Consultation",
        advice=(
            "Dr Raana sees ear complaints under an ENT consultation. She will examine "
            "the ear and advise from there."
        ),
    ),
    SymptomRoute(
        phrases=[
            "cannot hear properly",
            "hearing problem",
            "hard of hearing",
            "hearing reduced",
            "muffled",
        ],
        service="Hearing Test",
        advice=(
            "For reduced hearing the clinic starts with a hearing test, and Dr Raana "
            "reviews the result with you."
        ),
    ),
    SymptomRoute(
        phrases=[
            "sore throat",
            "throat pain",
            "voice",
            "hoarse",
            "tonsil",
            "snoring",
            "cough",
        ],
        service="ENT Consultation",
        advice=(
            "Throat and voice complaints are seen under an ENT consultation."
        ),
    ),
)


def store() -> DynamoClinicKnowledgeBaseStore:
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    return DynamoClinicKnowledgeBaseStore(table)


def show(routes: list[SymptomRoute]) -> None:
    if not routes:
        print("  no routes configured — every described symptom escalates to a human")
        return
    print(f"  {len(routes)} route(s), matched in this order:\n")
    for index, route in enumerate(routes, start=1):
        target = "URGENT — do not book" if route.urgent else f"-> {route.service}"
        print(f"  {index}. {target}")
        print(f"     phrases: {', '.join(route.phrases)}")
        wording = route.urgent_instruction if route.urgent else route.advice
        if wording:
            print(f"     says:    {wording}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--show", action="store_true")
    group.add_argument("--seed-ent", action="store_true")
    group.add_argument("--clear", action="store_true")
    args = parser.parse_args()

    kb_store = store()
    result = kb_store.get()
    if is_err(result) or result.value is None:
        raise SystemExit("the clinic is not configured yet")
    kb = result.value

    if args.show:
        show(kb.symptom_routes)
        return

    kb.symptom_routes = [] if args.clear else list(ENT_ROUTES)
    saved = kb_store.save(kb)
    if is_err(saved):
        raise SystemExit(f"  save failed: {saved.error}")

    print(f"  wrote {len(kb.symptom_routes)} route(s)\n")
    show(saved.value.symptom_routes)
    if not args.clear:
        print("  Review every line before it answers a real caller.")


if __name__ == "__main__":
    main()
