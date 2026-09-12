"""Make every service the clinic document lists bookable by name.

The onboarding form had three services on it while the uploaded practice document
described thirty. The agent may only book a service it can match *exactly* against
the configured list — it must never guess which service a procedure name maps to —
so a caller asking for a Nasal Endoscopy was correctly refused, and correctly
refused something the clinic actually does.

The names here are taken verbatim from the clinic's own document, under the heading
that says: "The services listed below are the ones you can ask for by name when
booking." Wording matters, because matching is exact.

    python scripts/set_clinic_services.py --dry-run
    python scripts/set_clinic_services.py

No prices. The document says fees are not listed and to ask reception, so quoting a
number here would be inventing money as the clinic's word. Preparation notes are
set only where the document gives one.
"""

from __future__ import annotations

import argparse

from clinic_front_desk.deployment.runtime import build_runtime_application
from clinic_front_desk.deployment.server import runtime_config_from_env
from clinic_front_desk.models import ServiceConfig, is_err

#: Every bookable service, grouped as the clinic document groups them. Names are
#: copied from the document exactly; the agent matches on them character for
#: character after normalising case and spacing.
SERVICES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Consultations",
        (
            "ENT Consultation",
            "Follow-up Consultation",
            "Second Opinion Consultation",
        ),
    ),
    (
        "Ear",
        (
            "Ear Examination",
            "Ear Wax Removal",
            "Hearing Test",
            "Tympanometry",
            "Ear Discharge Treatment",
            "Grommet Insertion",
            "Tympanoplasty",
            "Mastoidectomy",
        ),
    ),
    (
        "Nose and sinus",
        (
            "Nasal Endoscopy",
            "Sinus Treatment",
            "Septoplasty",
            "Sinus Surgery",
            "Nasal Polyp Removal",
            "Nose Bleed Treatment",
            "Allergy Testing",
        ),
    ),
    (
        "Throat and voice",
        (
            "Throat Examination",
            "Laryngoscopy",
            "Tonsillectomy",
            "Adenoidectomy",
            "Voice and Hoarseness Assessment",
            "Snoring and Sleep Apnoea Assessment",
        ),
    ),
    (
        "Head and neck",
        (
            "Neck Swelling Assessment",
            "Thyroid Swelling Assessment",
            "Head and Neck Screening",
        ),
    ),
    (
        "Other",
        (
            "Vertigo and Balance Assessment",
            "Foreign Body Removal",
            "Speech Therapy Referral",
        ),
    ),
)

#: Preparation notes, only where the clinic document actually gives one. An
#: invented instruction about how to prepare for a procedure would be clinical
#: guidance dressed as administration.
PREPARATION: dict[str, str] = {
    "Hearing Test": (
        "Avoid loud noise for 24 hours beforehand, and tell the audiologist if "
        "your ears feel blocked on the day. If you have had a hearing test "
        "elsewhere, bring the audiogram."
    ),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    app = build_runtime_application(runtime_config_from_env())

    result = app.stores.knowledge_base.get()
    if is_err(result) or result.value is None:
        raise SystemExit("the clinic is not configured yet; run onboarding first")
    kb = result.value

    existing = {service.name: service for service in kb.services}
    wanted: list[ServiceConfig] = []
    for _group, names in SERVICES:
        for name in names:
            previous = existing.get(name)
            wanted.append(
                ServiceConfig(
                    name=name,
                    # Keep a price the doctor has already entered; never invent one.
                    price=previous.price if previous is not None else None,
                    prep_instructions=PREPARATION.get(
                        name,
                        previous.prep_instructions if previous is not None else None,
                    ),
                )
            )

    added = [s.name for s in wanted if s.name not in existing]
    dropped = [name for name in existing if name not in {s.name for s in wanted}]

    for group, names in SERVICES:
        print(f"  {group}:")
        for name in names:
            mark = "+" if name in added else " "
            print(f"    {mark} {name}")
    print(f"\n  {len(wanted)} services total, {len(added)} new")
    if dropped:
        print(f"  ! these were configured and are NOT in the document: {dropped}")
        print("    they would be removed. Add them to this script to keep them.")

    if args.dry_run:
        print("\n  DRY RUN — nothing written")
        return

    kb.services = wanted
    saved = app.save_config(kb)
    if not saved.ok:
        raise SystemExit(f"config rejected: {saved.validation}")
    print(f"\n  saved. The agent can now book any of the {len(wanted)} by name.")


if __name__ == "__main__":
    main()
