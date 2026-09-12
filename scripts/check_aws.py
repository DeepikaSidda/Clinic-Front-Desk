"""Safe AWS credential / Bedrock access check (prints no secret values)."""

from __future__ import annotations

import os

import boto3

_PRESENCE_ONLY = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}


def main() -> None:
    for k in [
        "AWS_PROFILE",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ]:
        v = os.environ.get(k)
        if k in _PRESENCE_ONLY:
            print(k, "=", "<set>" if v else "<not set>")
        else:
            print(k, "=", v or "<not set>")

    session = boto3.Session()
    print("resolved_region =", session.region_name or "<none>")
    creds = session.get_credentials()
    print("credentials_found =", bool(creds))
    if not creds:
        print("RESULT = NO_CREDENTIALS")
        return

    try:
        ident = session.client("sts").get_caller_identity()
        print("account =", ident["Account"])
        print("arn =", ident["Arn"])
    except Exception as exc:  # noqa: BLE001
        print("STS_ERROR =", type(exc).__name__, str(exc)[:300])
        return

    region = session.region_name or "us-east-1"
    try:
        bedrock = session.client("bedrock", region_name=region)
        resp = bedrock.list_foundation_models()
        sonic = [
            m["modelId"]
            for m in resp.get("modelSummaries", [])
            if "sonic" in m["modelId"].lower()
        ]
        print("bedrock_region =", region)
        print("nova_sonic_models_visible =", sonic or "<none listed>")
    except Exception as exc:  # noqa: BLE001
        print("BEDROCK_ERROR =", type(exc).__name__, str(exc)[:300])


if __name__ == "__main__":
    main()
