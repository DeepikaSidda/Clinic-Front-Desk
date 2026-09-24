"""Set the clinic's phone number and its consultation fee.

Both are values the agent will state as the clinic's word, so both live in the
configuration rather than in an uploaded document: a number or a fee lifted from a PDF
could be a fax line, a supplier's, or last year's, and the caller acts on it either
way.

Edits in place — hours, services, providers and symptom routing are left untouched,
and a fee already entered for another service is never overwritten.

    python scripts/set_contact_and_fees.py --dry-run
    python scripts/set_contact_and_fees.py
    python scripts/set_contact_and_fees.py --phone 9876543210 --fee 700
"""

from __future__ import annotations

import argparse
import os
from dataclasses import replace

from clinic_front_desk.data_layer.dynamodb import create_stores
from clinic_front_desk.models import format_money, is_err

#: The clinic's own number, for callers the agent cannot finish helping.
DEFAULT_PHONE = "1234567890"

#: What a visit to the doctor costs.
DEFAULT_FEE = 500.0

#: Services the visit fee applies to. Consultations are the doctor's own time, which
#: is what "the fee for a doctor visit" means; tests and procedures are priced
#: separately and are deliberately left alone rather than given the same number.
CONSULTATION_KEYWORDS = ("consultation", "consult", "visit", "review")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phone", default=DEFAULT_PHONE)
    parser.add_argument("--fee", type=float, default=DEFAULT_FEE)
    parser.add_argument(
        "--dry-run", action="store_true", help="show the change without saving"
    )
    args = parser.parse_args()

    table = os.environ.get("CLINIC_TABLE_NAME", "clinic-front-desk")
    region = os.environ.get("AWS_REGION", "us-east-1")
    print(f"\n  table {table} ({region})")

    import boto3

    stores = create_stores(boto3.resource("dynamodb", region_name=region).Table(table))
    current = stores.clinic_knowledge_base.get()
    if is_err(current):
        print(f"  FAIL could not read the clinic configuration: {current.error.detail}")
        return 1
    kb = current.value
    if kb is None:
        print("  FAIL no clinic configuration exists yet; run the onboarding first")
        return 1

    print(f"  contact number : {kb.contact_phone or '(none)'}  ->  {args.phone}")

    services = []
    touched: list[str] = []
    for service in kb.services:
        name = (service.name or "").lower()
        if any(word in name for word in CONSULTATION_KEYWORDS):
            services.append(replace(service, price=args.fee))
            touched.append(service.name)
            was = format_money(service.price) if service.price is not None else "(none)"
            print(f"  {service.name:28} {was}  ->  {format_money(args.fee)}")
        else:
            services.append(service)
            if service.price is not None:
                print(f"  {service.name:28} {format_money(service.price)}  (unchanged)")
            else:
                print(f"  {service.name:28} (no price)  (unchanged)")

    if not touched:
        print(
            "\n  note  no service name looked like a consultation, so no fee was set."
            f"\n        keywords: {', '.join(CONSULTATION_KEYWORDS)}"
        )

    updated = replace(kb, contact_phone=args.phone.strip(), services=services)

    if args.dry_run:
        print("\n  dry run — nothing saved\n")
        return 0

    saved = stores.clinic_knowledge_base.save(updated)
    if is_err(saved):
        print(f"\n  FAIL save rejected: {saved.error.detail}\n")
        return 1

    print(f"\n  saved. {len(touched)} service(s) priced, contact number set.")
    print("  The agent reads this at the start of every call, so no restart is needed.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
