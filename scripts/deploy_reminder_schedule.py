"""Give the reminder a clock: two roles, a Lambda, and a nightly schedule.

Reminders were a button because nothing in this deployment ran on a timer, so a promise
to text patients "the day before" had nothing to keep it. This provisions the thing that
wakes up.

Everything here is idempotent. Re-running updates the function code and the schedule in
place rather than failing on "already exists", because the alternative is a deploy script
nobody dares run twice.

    python scripts/deploy_reminder_schedule.py --dry-run     # show the plan
    python scripts/deploy_reminder_schedule.py               # provision it
    python scripts/deploy_reminder_schedule.py --invoke      # and fire it once now

**Packaging note.** The zip carries the package and ``typing_extensions`` and nothing
else. That is deliberate and it is why the handler lives at
``clinic_front_desk/reminder_lambda.py`` rather than under ``deployment/``: importing
anything inside that subpackage drags in Strands, pydantic, jsonschema and
opentelemetry — 897 modules, measured — and the function would fail to load on the
first scheduled evening. From the package root the same import costs 84 modules and one
third-party file.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import time
import zipfile
from pathlib import Path
from typing import Any

import boto3  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "clinic_front_desk"
IAM_TEMPLATE = ROOT / "deploy" / "reminder-schedule-iam.json"

TABLE = os.environ.get("CLINIC_TABLE_NAME", "clinic-front-desk")
REGION = os.environ.get("AWS_REGION", "us-east-1")
FUNCTION_NAME = "clinic-front-desk-reminders"
HANDLER = "clinic_front_desk.reminder_lambda.handler"
RUNTIME = "python3.12"
LAMBDA_ROLE = "ClinicFrontDeskReminderLambdaRole"
SCHEDULER_ROLE = "ClinicFrontDeskReminderSchedulerRole"
SCHEDULE_NAME = "clinic-front-desk-nightly-reminders"

#: 18:00 in clinic time. Late enough that the day's bookings are in, early enough that
#: a patient reading the text can still ring the clinic back to cancel — which is the
#: outcome worth more than the reminder itself, because it frees the slot.
SCHEDULE_EXPRESSION = "cron(0 18 * * ? *)"
SCHEDULE_TIMEZONE = "Asia/Kolkata"


def say(message: str) -> None:
    print(f"  {message}")


# ---------------------------------------------------------------------------
# IAM
# ---------------------------------------------------------------------------


def _render(document: Any, account: str, *, function_name: str) -> Any:
    """Substitute the placeholders the checked-in policy carries."""
    text = json.dumps(document)
    for needle, value in (
        ("ACCOUNT_ID", account),
        ("REGION", REGION),
        ("TABLE_NAME", TABLE),
        ("FUNCTION_NAME", function_name),
    ):
        text = text.replace(needle, value)
    return json.loads(text)


def _strip_comments(document: Any) -> Any:
    """Drop the explanatory keys IAM does not accept."""
    if isinstance(document, dict):
        return {
            key: _strip_comments(value)
            for key, value in document.items()
            if key not in {"Comment", "SidComment", "ResourceComment"}
        }
    if isinstance(document, list):
        return [_strip_comments(item) for item in document]
    return document


def ensure_role(
    iam: Any, name: str, trust: Any, permissions: Any, *, dry_run: bool
) -> str:
    """Create or update a role and its single inline policy, returning its ARN."""
    if dry_run:
        say(f"would ensure role {name}")
        return f"arn:aws:iam::ACCOUNT:role/{name}"

    try:
        existing = iam.get_role(RoleName=name)
        arn = existing["Role"]["Arn"]
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust))
        say(f"role {name} exists; refreshed its trust policy")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchEntity":
            raise
        created = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="Clinic Front-Desk nightly appointment reminders.",
        )
        arn = created["Role"]["Arn"]
        say(f"created role {name}")

    iam.put_role_policy(
        RoleName=name,
        PolicyName=f"{name}Policy",
        PolicyDocument=json.dumps(permissions),
    )
    say(f"attached inline policy to {name}")
    return str(arn)


# ---------------------------------------------------------------------------
# Lambda
# ---------------------------------------------------------------------------


def build_zip() -> bytes:
    """The deployment package: our code, plus the one third-party module it needs."""
    import typing_extensions

    buffer = io.BytesIO()
    files = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(PACKAGE.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            archive.write(path, str(Path("clinic_front_desk") / path.relative_to(PACKAGE)))
            files += 1
        # The web assets are not needed by the reminder path, and shipping the HTML and
        # CSS into a function that renders nothing would be noise in the artefact.
        archive.write(typing_extensions.__file__, "typing_extensions.py")
        files += 1

    payload = buffer.getvalue()
    say(f"packaged {files} files, {len(payload) // 1024} KB")
    return payload


def ensure_function(
    lam: Any, role_arn: str, payload: bytes, *, dry_run: bool
) -> str:
    environment = {
        "Variables": {
            "CLINIC_TABLE_NAME": TABLE,
            "CLINIC_SMS_ENABLED": "1",
            "CLINIC_REMINDER_LEAD_DAYS": "1",
            "CLINIC_UTC_OFFSET_MINUTES": "330",
        }
    }
    if dry_run:
        say(f"would ensure function {FUNCTION_NAME} ({HANDLER})")
        return f"arn:aws:lambda:{REGION}:ACCOUNT:function:{FUNCTION_NAME}"

    try:
        lam.get_function(FunctionName=FUNCTION_NAME)
        lam.update_function_code(FunctionName=FUNCTION_NAME, ZipFile=payload)
        _wait_updated(lam)
        lam.update_function_configuration(
            FunctionName=FUNCTION_NAME,
            Role=role_arn,
            Handler=HANDLER,
            Runtime=RUNTIME,
            Timeout=120,
            MemorySize=256,
            Environment=environment,
        )
        _wait_updated(lam)
        say(f"updated function {FUNCTION_NAME}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        # A freshly created role is not immediately assumable by Lambda, which reports
        # it as an InvalidParameterValueException about the role. Retry rather than
        # make the operator run the script twice.
        for attempt in range(1, 11):
            try:
                lam.create_function(
                    FunctionName=FUNCTION_NAME,
                    Runtime=RUNTIME,
                    Role=role_arn,
                    Handler=HANDLER,
                    Code={"ZipFile": payload},
                    Description="Texts tomorrow's patients their appointment reminder.",
                    Timeout=120,
                    MemorySize=256,
                    Environment=environment,
                )
                say(f"created function {FUNCTION_NAME}")
                break
            except ClientError as inner:
                code = inner.response["Error"]["Code"]
                if code != "InvalidParameterValueException" or attempt == 10:
                    raise
                say(f"role not assumable yet; retrying ({attempt}/10)")
                time.sleep(5)

    described = lam.get_function(FunctionName=FUNCTION_NAME)
    return str(described["Configuration"]["FunctionArn"])


def _wait_updated(lam: Any) -> None:
    for _ in range(30):
        state = lam.get_function(FunctionName=FUNCTION_NAME)["Configuration"]
        if state.get("LastUpdateStatus") != "InProgress":
            return
        time.sleep(2)


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def ensure_schedule(
    scheduler: Any, function_arn: str, role_arn: str, *, dry_run: bool
) -> None:
    target = {
        "Arn": function_arn,
        "RoleArn": role_arn,
        # An empty payload means "work out tomorrow yourself", which is what the
        # nightly run should do. A date baked in here would silently go stale.
        "Input": json.dumps({}),
        "RetryPolicy": {"MaximumRetryAttempts": 2, "MaximumEventAgeInSeconds": 3600},
    }
    arguments: dict[str, Any] = {
        "Name": SCHEDULE_NAME,
        "GroupName": "default",
        "ScheduleExpression": SCHEDULE_EXPRESSION,
        "ScheduleExpressionTimezone": SCHEDULE_TIMEZONE,
        "Description": "Texts tomorrow's patients at 18:00 clinic time.",
        "State": "ENABLED",
        # No flexible window. The message says "tomorrow", and a reminder that lands
        # at a predictable hour is easier for a clinic to reason about than one that
        # drifts across the evening.
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "Target": target,
    }

    if dry_run:
        say(f"would ensure schedule {SCHEDULE_NAME} — {SCHEDULE_EXPRESSION} {SCHEDULE_TIMEZONE}")
        return

    try:
        scheduler.get_schedule(Name=SCHEDULE_NAME, GroupName="default")
        scheduler.update_schedule(**arguments)
        say(f"updated schedule {SCHEDULE_NAME}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        scheduler.create_schedule(**arguments)
        say(f"created schedule {SCHEDULE_NAME}")


# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="show the plan only")
    parser.add_argument(
        "--invoke", action="store_true", help="fire the function once after deploying"
    )
    parser.add_argument(
        "--day", default="", help="with --invoke, remind this date instead of tomorrow"
    )
    args = parser.parse_args()

    account = boto3.client("sts").get_caller_identity()["Account"]
    template = json.loads(IAM_TEMPLATE.read_text(encoding="utf-8"))
    rendered = _render(
        _strip_comments(template), account, function_name=FUNCTION_NAME
    )

    print(f"account {account}  region {REGION}  table {TABLE}")
    print("\nIAM")
    iam = boto3.client("iam")
    lambda_role = ensure_role(
        iam,
        LAMBDA_ROLE,
        rendered["LambdaTrustPolicy"],
        rendered["LambdaPermissions"],
        dry_run=args.dry_run,
    )
    scheduler_role = ensure_role(
        iam,
        SCHEDULER_ROLE,
        rendered["SchedulerTrustPolicy"],
        rendered["SchedulerPermissions"],
        dry_run=args.dry_run,
    )

    print("\nLambda")
    payload = build_zip()
    function_arn = ensure_function(
        boto3.client("lambda", region_name=REGION),
        lambda_role,
        payload,
        dry_run=args.dry_run,
    )

    print("\nSchedule")
    ensure_schedule(
        boto3.client("scheduler", region_name=REGION),
        function_arn,
        scheduler_role,
        dry_run=args.dry_run,
    )

    if args.invoke and not args.dry_run:
        print("\nInvoking once")
        event = {"day": args.day} if args.day else {}
        response = boto3.client("lambda", region_name=REGION).invoke(
            FunctionName=FUNCTION_NAME,
            Payload=json.dumps(event).encode(),
        )
        body = response["Payload"].read().decode()
        say(f"status {response['StatusCode']}")
        if "FunctionError" in response:
            say(f"FunctionError: {response['FunctionError']}")
        say(body[:1200])

    print("\ndone")


if __name__ == "__main__":
    main()
