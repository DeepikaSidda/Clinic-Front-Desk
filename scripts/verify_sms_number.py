"""Verify a destination mobile number so the SMS sandbox will deliver to it.

The account is in the SMS sandbox (``ACCOUNT_TIER: SANDBOX``), which only delivers to
numbers that have been verified with a one-time code. This starts that verification and
then completes it with the code AWS texts to the phone.

    python scripts/verify_sms_number.py --start  --number +919502285901
    python scripts/verify_sms_number.py --finish --number +919502285901 --code 123456
    python scripts/verify_sms_number.py --list

India, honestly: verification is enough for the sandbox and for a demo. Sending to
*arbitrary* Indian mobiles in production additionally needs TRAI DLT registration — an
entity and pre-approved templates lodged through an Indian telecom operator — without
which the carriers reject the message regardless of what AWS accepts.
"""

from __future__ import annotations

import argparse

import boto3
import botocore.exceptions

REGION = "us-east-1"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--number", help="E.164, e.g. +919502285901")
    parser.add_argument("--code", help="the one-time code AWS texted")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--finish", action="store_true")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    client = boto3.client("pinpoint-sms-voice-v2", region_name=REGION)

    try:
        if args.list or not (args.start or args.finish):
            existing = client.describe_verified_destination_numbers().get(
                "VerifiedDestinationNumbers", []
            )
            print(f"\n  {len(existing)} verified destination number(s)")
            for item in existing:
                print(
                    f"    {item.get('DestinationPhoneNumber')}  "
                    f"status={item.get('Status')}"
                )
            attributes = client.describe_account_attributes().get(
                "AccountAttributes", []
            )
            tier = next(
                (
                    a.get("Value")
                    for a in attributes
                    if a.get("Name") == "ACCOUNT_TIER"
                ),
                "?",
            )
            print(f"  account tier: {tier}")
            if tier == "SANDBOX":
                print("  in sandbox: delivery only to the numbers listed above\n")
            return 0

        if not args.number:
            print("  --number is required (E.164, e.g. +919502285901)")
            return 1

        if args.start:
            created = client.create_verified_destination_number(
                DestinationPhoneNumber=args.number
            )
            print(f"\n  verification started for {args.number}")
            print(f"  id: {created.get('VerifiedDestinationNumberId')}")
            client.send_destination_number_verification_code(
                VerifiedDestinationNumberId=created["VerifiedDestinationNumberId"],
                VerificationChannel="TEXT",
            )
            print("  a code has been texted to that number")
            print(
                "  then run: python scripts/verify_sms_number.py --finish "
                f"--number {args.number} --code <the code>\n"
            )
            return 0

        if args.finish:
            if not args.code:
                print("  --code is required to finish")
                return 1
            existing = client.describe_verified_destination_numbers().get(
                "VerifiedDestinationNumbers", []
            )
            match = next(
                (
                    item
                    for item in existing
                    if item.get("DestinationPhoneNumber") == args.number
                ),
                None,
            )
            if match is None:
                print(f"  {args.number} has no pending verification; run --start first")
                return 1
            client.verify_destination_number(
                VerifiedDestinationNumberId=match["VerifiedDestinationNumberId"],
                VerificationCode=args.code,
            )
            print(f"\n  {args.number} is now verified; the sandbox will deliver to it\n")
            return 0
    except botocore.exceptions.ClientError as exc:
        error = exc.response.get("Error", {})
        print(f"\n  FAILED [{error.get('Code')}] {error.get('Message')}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
