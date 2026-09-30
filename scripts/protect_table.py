"""Make the clinic table hard to lose.

The data itself never expires — DynamoDB is not a cache — so "permanent" is not about
storage. It is about the two ways this table can actually be emptied:

**Someone deletes the table.** A stray ``--teardown``, a tidy-up in the console.
Deletion protection refuses the call outright.

**Someone changes the data.** The demo dashboard is published with no authentication,
so any visitor can press *Cancel & notify* on a booked cell or block a range. Nothing
stops that by design — the point of the demo is that the controls work. Point-in-time
recovery gives a rolling 35-day window to restore the table to any second before it
happened, and ``backup_table.py`` keeps a local JSON snapshot as the belt to that
braces.

    python scripts/protect_table.py            # report only
    python scripts/protect_table.py --enable    # turn both on
"""

from __future__ import annotations

import argparse
import os

import boto3

TABLE = os.environ.get("CLINIC_TABLE_NAME", "clinic-front-desk")
REGION = os.environ.get("AWS_REGION", "us-east-1")


def report(client: object) -> tuple[bool, str]:
    described = client.describe_table(TableName=TABLE)["Table"]  # type: ignore[attr-defined]
    deletion = bool(described.get("DeletionProtectionEnabled", False))

    backups = client.describe_continuous_backups(TableName=TABLE)[  # type: ignore[attr-defined]
        "ContinuousBackupsDescription"
    ]
    pitr = backups.get("PointInTimeRecoveryDescription", {})
    status = str(pitr.get("PointInTimeRecoveryStatus", "UNKNOWN"))

    print(f"table                  {TABLE}")
    print(f"deletion protection    {'ENABLED' if deletion else 'DISABLED'}")
    print(f"point-in-time recovery {status}")
    if pitr.get("EarliestRestorableDateTime"):
        print(f"  restorable from      {pitr['EarliestRestorableDateTime']}")
        print(f"  restorable to        {pitr['LatestRestorableDateTime']}")
    return deletion, status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--enable", action="store_true", help="turn on deletion protection and PITR"
    )
    args = parser.parse_args()

    client = boto3.client("dynamodb", region_name=REGION)
    deletion, status = report(client)

    if not args.enable:
        print("\n(report only; pass --enable to turn these on)")
        return

    if not deletion:
        client.update_table(TableName=TABLE, DeletionProtectionEnabled=True)
        print("\nenabled deletion protection")
    if status != "ENABLED":
        client.update_continuous_backups(
            TableName=TABLE,
            PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True},
        )
        print("enabled point-in-time recovery (35-day rolling window)")

    print()
    report(client)


if __name__ == "__main__":
    main()
