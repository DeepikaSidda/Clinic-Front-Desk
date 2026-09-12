"""Provision the DynamoDB single table the Data_Layer needs (deploy step 1).

Creates the ``PK``/``SK`` table plus the four GSIs the design's "DynamoDB Table
Design" specifies (GSI1 open slots, GSI2 appointments by patient, GSI3 patient
name+phone, GSI4 decision finding-key dedupe) by delegating to
:func:`clinic_front_desk.data_layer.dynamodb.create_table`, so the deployed
schema is exactly the one the stores and their tests were built against.

Idempotent: if the table already exists it reports that and exits 0 without
touching it, so re-running during a deploy is safe.

Usage::

    python scripts/create_table.py --table clinic-front-desk --region us-east-1

    # against DynamoDB-local
    python scripts/create_table.py --endpoint-url http://localhost:8000
"""

from __future__ import annotations

import argparse
import sys

import boto3

from clinic_front_desk.data_layer.dynamodb import create_table, table_exists


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--table", default="clinic-front-desk", help="table name (default: %(default)s)"
    )
    parser.add_argument("--region", default=None, help="AWS region (default: session region)")
    parser.add_argument(
        "--endpoint-url", default=None, help="DynamoDB endpoint override, e.g. DynamoDB-local"
    )
    args = parser.parse_args(argv)

    dynamodb = boto3.resource(
        "dynamodb", region_name=args.region, endpoint_url=args.endpoint_url
    )

    if table_exists(dynamodb, args.table):
        print(f"table {args.table!r} already exists; nothing to do")
        return 0

    print(f"creating table {args.table!r} with GSI1-GSI4 ...")
    table = create_table(dynamodb, args.table)
    print(f"created {table.name} (status={table.table_status})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
