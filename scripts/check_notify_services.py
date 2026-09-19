"""Which notification transports this AWS account can actually use.

Amazon Connect is unavailable on accounts billed through AISPL (the Indian AWS
entity) — ``CreateInstance`` is refused at the account level, so no IAM grant or
quota increase changes it. This checks which alternatives are reachable, so the
human-handover transport is chosen against what the account can do rather than
against what the docs say is possible.

    python scripts/check_notify_services.py
"""

from __future__ import annotations

import boto3
import botocore.exceptions

REGION = "us-east-1"

PROBES: tuple[tuple[str, str, str], ...] = (
    ("connect", "list_instances", "Amazon Connect — contact centre"),
    ("sns", "list_topics", "Amazon SNS — email / SMS fan-out"),
    ("ses", "get_send_quota", "Amazon SES — email"),
    ("events", "list_rules", "Amazon EventBridge — routing"),
    ("chime", "list_voice_connectors", "Amazon Chime SDK — voice"),
)


def main() -> None:
    print(f"  region {REGION}\n")
    for service, call, label in PROBES:
        try:
            client = boto3.client(service, region_name=REGION)
            getattr(client, call)()
            print(f"  ok    {label}")
        except botocore.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "?")
            print(f"  FAIL  {label}  [{code}]")
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {label}  [{type(exc).__name__}]")

    print()
    print("  Connect instance creation, specifically:")
    try:
        boto3.client("connect", region_name=REGION).create_instance(
            IdentityManagementType="CONNECT_MANAGED",
            InstanceAlias="clinic-probe-do-not-keep",
            InboundCallsEnabled=False,
            OutboundCallsEnabled=False,
        )
        print("  ok    an instance can be created (delete the probe instance!)")
    except botocore.exceptions.ClientError as exc:
        error = exc.response.get("Error", {})
        print(f"  FAIL  [{error.get('Code')}] {error.get('Message', '')[:160]}")


if __name__ == "__main__":
    main()
