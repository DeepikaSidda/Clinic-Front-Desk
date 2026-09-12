"""Look a patient up the way the agent does, and show why a lookup missed.

Given whatever the caller said — a code, a name, a mobile number — this prints the
normalized comparison keys the stores actually match on, then the records found.
The point is to tell three different failures apart:

    the record does not exist
    the record exists but the spoken input normalized to something else
    the record exists and the lookup is fine

    python scripts/lookup_patient.py --code SI307
    python scripts/lookup_patient.py --code 's i three zero seven'
    python scripts/lookup_patient.py --name 'Sailaja Devi' --phone 9900012307
    python scripts/lookup_patient.py --all
"""

from __future__ import annotations

import argparse
import os

import boto3

from clinic_front_desk.data_layer.dynamodb.patient_store import DynamoPatientStore
from clinic_front_desk.models import is_ok
from clinic_front_desk.models.matching import (
    normalize_patient_code,
    normalize_person_name,
    normalize_phone,
    patient_code,
    patient_lookup_key,
)

TABLE = os.environ.get("CLINIC_TABLE_NAME", "clinic-front-desk")
REGION = os.environ.get("AWS_REGION", "us-east-1")


def store() -> DynamoPatientStore:
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    return DynamoPatientStore(table)


def show(patient: object) -> str:
    name = getattr(patient, "name", "?")
    phone = getattr(patient, "callback_phone", None) or getattr(patient, "phone", "?")
    code = getattr(patient, "code", None)
    return f"{code or '(no code)':<8} {name:<24} {phone}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", help="a patient code, as spoken or as written")
    parser.add_argument("--name")
    parser.add_argument("--phone")
    parser.add_argument("--all", action="store_true", help="list every patient")
    args = parser.parse_args()

    patients = store()

    if args.all:
        result = patients.list_all() if hasattr(patients, "list_all") else None
        if result is not None and is_ok(result):
            print(f"  {len(result.value)} patient(s):")
            for patient in result.value:
                print("   ", show(patient))
        else:
            print("  could not list patients")
        return

    if args.code:
        normalized = normalize_patient_code(args.code)
        print(f"  said      {args.code!r}")
        print(f"  becomes   {normalized!r}   <- what the store compares")
        result = patients.find_by_code(args.code)
        if is_ok(result):
            if result.value:
                for patient in result.value:
                    print("  found    ", show(patient))
            else:
                print("  found     nothing")
        else:
            print(f"  error     {result}")
        print()

    if args.name or args.phone:
        name = args.name or ""
        phone = args.phone or ""
        print(f"  said      name={name!r} phone={phone!r}")
        print(f"  name key  {normalize_person_name(name)!r}")
        print(f"  phone key {normalize_phone(phone)!r}")
        print(f"  lookup    {patient_lookup_key(name, phone)!r}")
        expected = patient_code(name, phone)
        print(f"  code would be {expected!r}")
        result = patients.find_by_name_and_phone(name, phone)
        if is_ok(result):
            if result.value:
                for patient in (
                    result.value if isinstance(result.value, list) else [result.value]
                ):
                    print("  found    ", show(patient))
            else:
                print("  found     nothing")
        else:
            print(f"  error     {result}")


if __name__ == "__main__":
    main()
