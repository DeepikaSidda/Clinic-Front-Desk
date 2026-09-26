"""Dump the whole DynamoDB table to a local JSON file before a destructive change.

Written because publishing the dashboard meant deleting real patient records and the
call sessions holding real voices, and a deletion you cannot undo is a deletion you
should not make casually. The file lands in ``.backups/``, which is gitignored: it
holds real names, mobile numbers and call transcripts.

    python scripts/backup_table.py
    python scripts/backup_table.py --restore .backups/clinic-front-desk-<stamp>.json
"""

from __future__ import annotations

import argparse
import datetime
import decimal
import json
import os
import pathlib
from typing import Any

import boto3

TABLE = os.environ.get("CLINIC_TABLE_NAME", "clinic-front-desk")
REGION = os.environ.get("AWS_REGION", "us-east-1")
OUT_DIR = pathlib.Path(__file__).resolve().parent.parent / ".backups"


class _DecimalSafe(json.JSONEncoder):
    """DynamoDB hands numbers back as ``Decimal``, which ``json`` refuses."""

    def default(self, o: Any) -> Any:
        if isinstance(o, decimal.Decimal):
            return int(o) if o == o.to_integral_value() else float(o)
        if isinstance(o, set):
            return sorted(o)
        return super().default(o)


def _table() -> Any:
    return boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


def scan_all() -> list[dict[str, Any]]:
    table = _table()
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {}
    while True:
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        key = page.get("LastEvaluatedKey")
        if not key:
            break
        kwargs["ExclusiveStartKey"] = key
    return items


def backup() -> pathlib.Path:
    items = scan_all()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    path = OUT_DIR / f"{TABLE}-{stamp}.json"
    path.write_text(json.dumps(items, cls=_DecimalSafe, indent=2), encoding="utf-8")
    print(f"{len(items)} items -> {path}")
    return path


def restore(path: pathlib.Path) -> None:
    items = json.loads(path.read_text(encoding="utf-8"))
    table = _table()
    with table.batch_writer() as batch:
        for item in items:
            batch.put_item(Item=item)
    print(f"restored {len(items)} items from {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restore", metavar="FILE", help="put a backup file back")
    args = parser.parse_args()
    if args.restore:
        restore(pathlib.Path(args.restore))
    else:
        backup()


if __name__ == "__main__":
    main()
