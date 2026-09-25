"""Try to create an Amazon Connect instance in every region that offers it.

``CreateInstance`` was refused in us-east-1 with an AISPL account-type error. That
message reads as account-level, but "reads as" is not "is" — so this tries every
region Connect runs in, including ap-south-1 (Mumbai), on the theory that an
Indian-billed account might be permitted in the Indian region.

Stops at the first success. If one region works, the whole Connect handover becomes
live rather than implemented-but-unprovisioned.

    python scripts/try_connect_regions.py            # report only, creates nothing
    python scripts/try_connect_regions.py --create   # actually create on first success
"""

from __future__ import annotations

import argparse

import boto3
import botocore.exceptions

#: Regions offering Amazon Connect. ap-south-1 is first because it is the one most
#: likely to behave differently for an AISPL account.
REGIONS = (
    "ap-south-1",
    "us-east-1",
    "us-west-2",
    "ap-southeast-1",
    "ap-southeast-2",
    "ap-northeast-1",
    "eu-central-1",
    "eu-west-2",
    "ca-central-1",
)

ALIAS = "clinic-front-desk"


def probe(region: str, *, create: bool) -> tuple[bool, str]:
    """Return (usable, detail) for one region."""
    try:
        client = boto3.client("connect", region_name=region)
    except Exception as exc:  # noqa: BLE001
        return False, f"client error: {type(exc).__name__}"

    # An existing instance is even better than a creatable one.
    try:
        existing = client.list_instances().get("InstanceSummaryList", [])
        if existing:
            summary = existing[0]
            return True, (
                f"ALREADY EXISTS  id={summary['Id']} alias={summary.get('InstanceAlias')}"
            )
    except botocore.exceptions.ClientError as exc:
        error = exc.response.get("Error", {})
        return False, (
            f"list_instances {error.get('Code')}: {str(error.get('Message'))[:90]}"
        )

    if not create:
        # Probe with an alias that is valid client-side (botocore only checks
        # length) but rejected server-side, because it contains an underscore.
        # The *order* of the two errors is the answer: if the account is blocked,
        # AWS says so before it ever looks at the alias. If it complains about the
        # alias, the account is not the obstacle and a real alias would work.
        try:
            client.create_instance(
                IdentityManagementType="CONNECT_MANAGED",
                InstanceAlias="clinic_probe_invalid",
                InboundCallsEnabled=False,
                OutboundCallsEnabled=False,
            )
            return True, "created (unexpected - delete this probe instance)"
        except botocore.exceptions.ClientError as exc:
            error = exc.response.get("Error", {})
            code = str(error.get("Code"))
            message = str(error.get("Message", ""))
            if "AISPL" in message:
                return False, f"{code}: AISPL account-level block"
            # NOT a pass. The assumption above — that AWS reports an account block
            # before it looks at the alias — is false. With an invalid alias every
            # region answered "Invalid Input. Please check instance alias." and this
            # printed "account NOT the blocker" for all eight; running --create with a
            # real alias then returned the AISPL error in every one of them. The alias
            # is validated first, so this probe cannot see the account restriction at
            # all, and reporting a pass here is worse than reporting nothing.
            return False, (
                f"INCONCLUSIVE ({code}: {message[:50]}) — "
                "alias rejected before the account was checked; use --create to know"
            )
        except botocore.exceptions.ParamValidationError as exc:
            return False, f"client-side validation: {str(exc)[:70]}"

    try:
        created = client.create_instance(
            IdentityManagementType="CONNECT_MANAGED",
            InstanceAlias=ALIAS,
            InboundCallsEnabled=False,
            OutboundCallsEnabled=True,
        )
        return True, f"CREATED id={created['Id']}"
    except botocore.exceptions.ClientError as exc:
        error = exc.response.get("Error", {})
        return False, f"{error.get('Code')}: {str(error.get('Message'))[:90]}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--create", action="store_true", help="create an instance in the first region that allows it"
    )
    args = parser.parse_args()

    print(f"  alias {ALIAS!r}   create={args.create}\n")
    if not args.create:
        print(
            "  NOTE: without --create this cannot tell you whether the account is\n"
            "        allowed. AWS validates the instance alias before the account\n"
            "        type, so a probe with a deliberately invalid alias never reaches\n"
            "        the account check. Only --create gives a real answer.\n"
        )
    winners: list[str] = []
    for region in REGIONS:
        usable, detail = probe(region, create=args.create)
        print(f"  {'OK  ' if usable else 'no  '} {region:<16} {detail}")
        if usable:
            winners.append(region)
            if args.create:
                break

    print()
    if winners:
        print(f"  Connect is usable in: {', '.join(winners)}")
    else:
        print("  No region allows an instance. The block is account-level (AISPL).")


if __name__ == "__main__":
    main()
