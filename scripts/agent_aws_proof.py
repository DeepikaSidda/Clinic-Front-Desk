"""Prove, from AWS's own audit log, that the coding agent is connected to this account.

The hackathon asks for "a coding agent connected to the AWS console, with documented
proof of the connection". A screenshot of a chat window proves nothing — it is a picture
of text. What settles the question is CloudTrail, because CloudTrail is written by AWS,
not by us, and because the AWS SDK stamps the calling application into every request's
``User-Agent`` header.

Kiro sets ``AWS_SDK_UA_APP_ID=kiro-ide`` in the environment it gives the agent, so every
boto3 call the agent makes arrives at AWS carrying ``app/kiro-ide``, and CloudTrail keeps
it::

    "userAgent": "Boto3/1.43.89 ... lang/python#3.12.3 ... app/kiro-ide Botocore/1.43.89"

A call made by a human in the browser console cannot carry that string. So filtering the
account's trail on ``app/kiro-ide`` isolates exactly the API calls that originated from
the coding agent, and hands back their event IDs — which anyone can paste into CloudTrail
Event history in the console and see for themselves.

This script gathers three things:

1. **Which principal the agent is using** (``sts:GetCallerIdentity``).
2. **A call made right now**, so the proof is live rather than a story about the past.
   Its request ID is printed, then looked up in the trail once AWS surfaces it.
3. **The mutating calls the agent has already made** — the ones that changed
   infrastructure, not just read it.

    python scripts/agent_aws_proof.py                 # collect and print
    python scripts/agent_aws_proof.py --days 30       # widen the history scan
    python scripts/agent_aws_proof.py --write-doc     # refresh docs/AGENT_AWS_PROOF.md

The history scan walks management events one page at a time and AWS throttles that API
hard, so ``--days 30`` takes a few minutes. The default window is deliberately short.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
from pathlib import Path
from typing import Any

import boto3  # type: ignore[import-untyped]

AGENT_STAMP = "app/kiro-ide"
TABLE = os.environ.get("CLINIC_TABLE_NAME", "clinic-front-desk")
REGION = os.environ.get("AWS_REGION", "us-east-1")
DOC_PATH = Path(__file__).resolve().parent.parent / "docs" / "AGENT_AWS_PROOF.md"

# CloudTrail is eventually consistent; AWS documents "within 15 minutes" for management
# events. We poll rather than sleep blind, so the usual case returns in a minute or two.
SURFACE_TIMEOUT_SECONDS = 900
POLL_SECONDS = 30


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def identity() -> dict[str, Any]:
    """Return the principal the agent's credentials resolve to."""
    caller = boto3.client("sts", region_name=REGION).get_caller_identity()
    return {
        "arn": caller["Arn"],
        "account": caller["Account"],
        "user_id": caller["UserId"],
        "sdk_app_id": os.environ.get("AWS_SDK_UA_APP_ID", "(not set)"),
    }


def make_live_call() -> dict[str, Any]:
    """Make one harmless, attributable API call and return its identifiers.

    ``DescribeTable`` is a read, which is the point: proving a *connection* needs no
    mutation, and a proof script should not change the production table as a side effect
    of being run. It is also a management event, so it lands in CloudTrail by default
    without a data-event selector, and this account makes few of them — which keeps the
    verification lookup below cheap.
    """
    client = boto3.client("dynamodb", region_name=REGION)
    called_at = _utc_now()
    response = client.describe_table(TableName=TABLE)
    metadata = response["ResponseMetadata"]
    return {
        "event_name": "DescribeTable",
        "called_at": called_at,
        "request_id": metadata["RequestId"],
        "http_status": metadata["HTTPStatusCode"],
        "table_arn": response["Table"]["TableArn"],
        "item_count": response["Table"]["ItemCount"],
    }


def _agent_events(
    client: Any,
    *,
    start: dt.datetime,
    end: dt.datetime,
    attribute: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return trail records in the window that carry the agent's user-agent stamp."""
    kwargs: dict[str, Any] = {"StartTime": start, "EndTime": end}
    if attribute is not None:
        kwargs["LookupAttributes"] = [attribute]

    found: list[dict[str, Any]] = []
    for page in client.get_paginator("lookup_events").paginate(**kwargs):
        for event in page["Events"]:
            record = json.loads(event["CloudTrailEvent"])
            if AGENT_STAMP in str(record.get("userAgent", "")):
                found.append(record)
    return found


def confirm_live_call(client: Any, call: dict[str, Any], *, wait: bool) -> dict[str, Any] | None:
    """Poll the trail until the call we just made shows up, and return its record."""
    deadline = time.monotonic() + (SURFACE_TIMEOUT_SECONDS if wait else 0)
    attempt = 0
    while True:
        attempt += 1
        matches = _agent_events(
            client,
            # A minute of slack either side: the local clock and AWS's event time need
            # not agree to the second.
            start=call["called_at"] - dt.timedelta(minutes=1),
            end=_utc_now() + dt.timedelta(minutes=1),
            attribute={"AttributeKey": "EventName", "AttributeValue": call["event_name"]},
        )
        for record in matches:
            if record.get("requestID") == call["request_id"]:
                return record
        if time.monotonic() >= deadline:
            return None
        print(
            f"  not surfaced yet (attempt {attempt}); CloudTrail can take ~15 min, "
            f"waiting {POLL_SECONDS}s"
        )
        time.sleep(POLL_SECONDS)


def agent_write_history(client: Any, *, days: int) -> list[dict[str, Any]]:
    """Return the agent's mutating calls, most recent first.

    Filtering on ``ReadOnly=false`` at the API keeps this to the calls that actually
    changed something. Reads prove the connection; writes prove the agent was trusted
    with it.
    """
    end = _utc_now()
    records = _agent_events(
        client,
        start=end - dt.timedelta(days=days),
        end=end,
        attribute={"AttributeKey": "ReadOnly", "AttributeValue": "false"},
    )
    return sorted(records, key=lambda r: str(r["eventTime"]), reverse=True)


def _mask_ip(address: str) -> str:
    """Blunt the source IP before it goes into a file that gets pushed publicly.

    CloudTrail records the caller's address, which for a laptop is a home connection.
    The address is not part of the proof — the user agent and the event IDs are — so the
    written document keeps only enough of it to show the calls share an origin.
    """
    parts = address.split(".")
    if len(parts) == 4:
        return ".".join(parts[:2] + ["x", "x"])
    return "(redacted)"


def _summarise(record: dict[str, Any]) -> str:
    """One-line description of what a trail record did."""
    params = record.get("requestParameters") or {}
    name = record["eventName"]
    if name == "UpdateTable" and "deletionProtectionEnabled" in params:
        return f"deletion protection -> {params['deletionProtectionEnabled']} on {params.get('tableName')}"
    if name == "UpdateContinuousBackups":
        spec = params.get("pointInTimeRecoverySpecification", {})
        return f"point-in-time recovery -> {spec.get('pointInTimeRecoveryEnabled')} on {params.get('tableName')}"
    if name == "SendCommand":
        targets = ", ".join(params.get("instanceIds", []) or ["?"])
        return f"ran {params.get('documentName')} on {targets}"
    if name == "TagResource":
        return f"tagged {params.get('resourceArn') or params.get('ResourceArn') or '?'}"
    return json.dumps(params, default=str)[:120]


def _print_report(
    who: dict[str, Any],
    call: dict[str, Any],
    confirmed: dict[str, Any] | None,
    writes: list[dict[str, Any]],
    days: int,
) -> None:
    print("=" * 78)
    print("1. Which principal is the coding agent using?")
    print("=" * 78)
    print(f"  caller ARN      {who['arn']}")
    print(f"  account         {who['account']}")
    print(f"  region          {REGION}")
    print(f"  SDK app id      {who['sdk_app_id']}   <- stamped into every request")

    print()
    print("=" * 78)
    print("2. A call made right now, found again in AWS's own audit log")
    print("=" * 78)
    print(f"  API             dynamodb:{call['event_name']}")
    print(f"  called at       {call['called_at']:%Y-%m-%dT%H:%M:%SZ}")
    print(f"  HTTP status     {call['http_status']}")
    print(f"  request ID      {call['request_id']}")
    print(f"  resource        {call['table_arn']}  ({call['item_count']} items)")
    if confirmed is None:
        print("  trail record    not surfaced yet - re-run to confirm")
    else:
        print(f"  trail event ID  {confirmed['eventID']}")
        print(f"  trail userAgent {confirmed['userAgent']}")
        print(f"  source IP       {confirmed['sourceIPAddress']}")
        print("  ^ CloudTrail attributes this call to the agent, by user agent.")

    print()
    print("=" * 78)
    print(f"3. Mutating calls the agent made in the last {days} day(s)")
    print("=" * 78)
    if not writes:
        print("  none in this window (widen it with --days)")
    for record in writes:
        service = record["eventSource"].split(".")[0]
        print(f"  {record['eventTime']}  {service}:{record['eventName']}")
        print(f"    {_summarise(record)}")
        print(f"    eventID {record['eventID']}")


def _doc_markdown(
    who: dict[str, Any],
    call: dict[str, Any],
    confirmed: dict[str, Any] | None,
    writes: list[dict[str, Any]],
    days: int,
) -> str:
    generated = _utc_now().strftime("%Y-%m-%d %H:%M UTC")
    # Point the reader at a real ID they can paste, preferring the live call because it is
    # the most recent and so needs no time-range widening in the console.
    sample_event_id = (
        confirmed["eventID"]
        if confirmed is not None
        else (writes[0]["eventID"] if writes else "<event id>")
    )
    rows = "\n".join(
        f"| {r['eventTime']} | `{r['eventSource'].split('.')[0]}:{r['eventName']}` | "
        f"{_summarise(r)} | `{r['eventID']}` |"
        for r in writes
    ) or "| _none in window_ | | | |"

    if confirmed is None:
        live_block = (
            f"The call was accepted (HTTP {call['http_status']}, request ID "
            f"`{call['request_id']}`) but had not yet surfaced in the trail when this "
            "document was generated. CloudTrail delivers management events within about "
            "15 minutes; re-run the script to capture the matching record."
        )
    else:
        live_block = (
            f"CloudTrail event `{confirmed['eventID']}` records it, from source IP "
            f"`{_mask_ip(str(confirmed['sourceIPAddress']))}`, with this user agent:\n\n"
            f"```\n{confirmed['userAgent']}\n```\n\n"
            f"The `{AGENT_STAMP}` fragment is the proof. It is set by the IDE, travels in "
            "the HTTP `User-Agent` header of the SDK call, and is recorded by AWS rather "
            "than by this repository."
        )

    return f"""# Proof that the coding agent is connected to AWS

_Generated by `scripts/agent_aws_proof.py` on {generated}. Re-runnable by anyone holding
the account's credentials._

## What counts as proof

A chat transcript is not evidence — it is a picture of text, and it is trivially edited.
The question "did a coding agent really operate this AWS account?" is settled by
[CloudTrail](https://console.aws.amazon.com/cloudtrailv2/home?region={REGION}#/events),
for two reasons:

- **AWS writes it, we don't.** Every record is produced by the control plane.
- **The SDK names the calling application.** Kiro sets `AWS_SDK_UA_APP_ID=kiro-ide` in
  the environment it gives the agent, so boto3 appends `{AGENT_STAMP}` to the
  `User-Agent` header of every request the agent makes, and CloudTrail stores the header
  verbatim.

A human clicking in the browser console cannot produce that string. Filtering the trail
on `{AGENT_STAMP}` therefore isolates exactly the calls that came from the coding agent.

## 1. The identity the agent operates as

| | |
|---|---|
| Caller ARN | `{who['arn']}` |
| Account | `{who['account']}` |
| Region | `{REGION}` |
| SDK app id | `{who['sdk_app_id']}` |

The agent reads credentials from the standard provider chain; nothing in this repository
stores them.

## 2. A call made live, then found in the audit log

The script calls `dynamodb:DescribeTable` on `{TABLE}` and keeps the request ID, so the
evidence is a call made during generation rather than a claim about the past.

| | |
|---|---|
| API | `dynamodb:{call['event_name']}` |
| Called at | {call['called_at']:%Y-%m-%dT%H:%M:%SZ} |
| HTTP status | {call['http_status']} |
| Request ID | `{call['request_id']}` |
| Resource | `{call['table_arn']}` ({call['item_count']} items) |

{live_block}

## 3. Infrastructure the agent changed

Reads prove a connection. These are the calls that changed the account — filtered to
`ReadOnly=false` over the last {days} day(s), and to the agent's user-agent stamp.

| Time (UTC) | API | What it did | CloudTrail event ID |
|---|---|---|---|
{rows}

Every event ID above can be pasted into **CloudTrail → Event history → Lookup attributes
→ Event ID** in the console to see the full record, including the user agent.

## 4. How the connection is wired

- **Credentials.** The agent runs shell commands in the workspace and boto3 resolves the
  standard chain (environment, then `~/.aws/credentials`). There is no bespoke auth path.
- **Attribution.** `AWS_SDK_UA_APP_ID=kiro-ide`, injected by the IDE into the agent's
  environment, is what makes agent traffic separable from human traffic in the trail.
- **MCP.** The workspace also has the AWS Bedrock AgentCore MCP server available
  (`awslabs.amazon-bedrock-agentcore-mcp-server`), configured in
  `~/.kiro/settings/mcp.json`, giving the agent AgentCore documentation and deployment
  tooling alongside raw SDK access.

## 5. Checking it in the console yourself

The table above is only useful if it can be checked against the source, so here is the
path. Nothing below requires trusting this repository.

1. Open **CloudTrail → Event history** in
   [{REGION}](https://console.aws.amazon.com/cloudtrailv2/home?region={REGION}#/events).
2. Set **Lookup attributes** to **Event ID** and paste any ID from the tables above — for
   instance `{sample_event_id}`.
3. Widen the time range if the event is older than the default window.
4. Open the event and expand the raw JSON. Read the `userAgent` field: it ends with
   `{AGENT_STAMP}`.

The screenshots worth keeping are that expanded event with the `userAgent` line visible,
and the `dynamodb:UpdateTable` event showing `deletionProtectionEnabled: true` — the
agent changing production configuration, recorded by AWS.

## 6. Reproducing this document

```bash
python scripts/agent_aws_proof.py --days {days} --write-doc
```

The script makes a fresh call each run, so a regenerated copy carries a new request ID
and a new event ID rather than repeating the ones above.
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        default=2,
        help="how far back to scan for the agent's mutating calls (default: 2)",
    )
    parser.add_argument(
        "--write-doc",
        action="store_true",
        help=f"also write {DOC_PATH.relative_to(DOC_PATH.parent.parent)}",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="do not wait for the live call to surface in CloudTrail",
    )
    args = parser.parse_args()

    trail = boto3.client("cloudtrail", region_name=REGION)

    who = identity()
    call = make_live_call()
    print(f"made a live dynamodb:DescribeTable call (request {call['request_id']})")
    print("looking it up in CloudTrail...")
    confirmed = confirm_live_call(trail, call, wait=not args.no_wait)
    print(f"scanning the last {args.days} day(s) of write events...\n")
    writes = agent_write_history(trail, days=args.days)

    _print_report(who, call, confirmed, writes, args.days)

    if args.write_doc:
        DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
        DOC_PATH.write_text(
            _doc_markdown(who, call, confirmed, writes, args.days), encoding="utf-8"
        )
        print(f"\nwrote {DOC_PATH}")


if __name__ == "__main__":
    main()
