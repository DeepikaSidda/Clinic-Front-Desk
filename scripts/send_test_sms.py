"""Send one real cancellation SMS, to prove the path end to end.

Uses the same code the clinic uses, not a shortcut: the number is normalised by
``to_e164`` and the body is built by ``cancellation_message``, so a message arriving
here means the clinic's own path works.

    python scripts/send_test_sms.py --to 9502285901
    python scripts/send_test_sms.py --to 9502285901 --dry-run
"""

from __future__ import annotations

import argparse

from clinic_front_desk.notifications import SnsSmsSender, cancellation_message, to_e164


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", required=True, help="mobile number, e.g. 9502285901")
    parser.add_argument("--name", default="Sidda Deepika")
    parser.add_argument("--service", default="ENT Consultation")
    parser.add_argument("--date", default="Monday 28 September")
    parser.add_argument("--time", default="09:30")
    parser.add_argument("--clinic-phone", default="1234567890")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    normalised = to_e164(args.to)
    print(f"\n  number  {args.to!r} -> {normalised!r}")
    if normalised is None:
        print("  refused: not a usable mobile number\n")
        return 1

    body = cancellation_message(
        patient_name=args.name,
        service=args.service,
        date=args.date,
        time=args.time,
        clinic_phone=args.clinic_phone,
    )
    print(f"  body    {body}")
    print(f"  length  {len(body)} characters")

    if args.dry_run:
        print("\n  dry run - nothing sent\n")
        return 0

    outcome = SnsSmsSender(region=args.region).send(args.to, body)
    print()
    if outcome.sent:
        print(f"  SENT to {outcome.to}  (message id {outcome.message_id})")
        print("  check the handset\n")
        return 0

    print(f"  NOT SENT to {outcome.to}")
    print(f"  reason: {outcome.detail}")
    print(
        "\n  If this mentions the sandbox, verify the number first:\n"
        "    python scripts/verify_sms_number.py --start  --number "
        f"{normalised}\n"
        "    python scripts/verify_sms_number.py --finish --number "
        f"{normalised} --code <code>\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
