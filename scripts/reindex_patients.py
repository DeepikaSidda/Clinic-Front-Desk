"""Rewrite every patient's name+phone lookup key.

Patient lookup used to key on the raw name and phone, so a caller whose record read
"Lakshmi Prasad" could not be found when she said her own name — speech-to-text
lower-cases what it transcribes. The key is normalised now, but records written
before that change still carry the old one and remain unfindable.

This re-puts each patient unchanged, which recomputes the key. The records
themselves are not modified: the stored name and number stay exactly as entered,
because that is what the doctor reads.

    python scripts/reindex_patients.py --dry-run
    python scripts/reindex_patients.py

Safe to re-run. A record already on the new key is rewritten to the same value.
"""

from __future__ import annotations

import argparse

from clinic_front_desk.deployment.runtime import build_runtime_application
from clinic_front_desk.deployment.server import runtime_config_from_env
from clinic_front_desk.models import is_err, patient_code, patient_lookup_key


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    app = build_runtime_application(runtime_config_from_env())
    store = app.stores.patients

    # No list_all on the interface — patients are found by id or by name+phone,
    # both of which need to know the answer already. So this reads the table
    # directly, which is what a one-off migration is allowed to do.
    import boto3
    from boto3.dynamodb.conditions import Attr

    config = runtime_config_from_env()
    table = boto3.resource("dynamodb", region_name=config.region).Table(
        config.table_name
    )

    items: list[dict[str, object]] = []
    response = table.scan(FilterExpression=Attr("entity").eq("Patient"))
    items.extend(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        response = table.scan(
            FilterExpression=Attr("entity").eq("Patient"),
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        items.extend(response.get("Items", []))

    print(f"  {len(items)} patient records\n")

    stale = 0
    for item in items:
        patient_id = str(item.get("id", ""))
        name = str(item.get("name", ""))
        phone = str(item.get("callback_phone", ""))
        current_key = str(item.get("GSI3PK", ""))
        wanted_key = f"NAMEPHONE#{patient_lookup_key(name, phone)}"
        current_code = str(item.get("code", ""))
        wanted_code = patient_code(name, phone)

        needs_key = current_key != wanted_key
        # Only *assign* a missing code. A code already issued is never changed:
        # the patient was read it out on a call and may have written it down.
        needs_code = not current_code and bool(wanted_code)
        if not needs_key and not needs_code:
            continue

        stale += 1
        print(f"  {name!r} {phone!r}")
        if needs_key:
            print(f"    key  {current_key}  ->  {wanted_key}")
        if needs_code:
            print(f"    code (none)  ->  {wanted_code}")
        if args.dry_run:
            continue

        found = store.get(patient_id)
        if is_err(found) or found.value is None:
            print(f"    ! could not read {patient_id}")
            continue
        patient = found.value
        if needs_code:
            patient.code = wanted_code
        written = store.update(patient)
        if is_err(written):
            print(f"    ! could not rewrite: {written.error.detail}")

    print()
    if args.dry_run:
        print(f"  DRY RUN — {stale} records would be updated")
    else:
        print(f"  updated {stale} records")


if __name__ == "__main__":
    main()
