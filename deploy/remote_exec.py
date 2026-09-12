"""Run a shell command on the voice-agent instance, via SSM.

The instance has no SSH key pair and its security group only admits CloudFront,
so this is the only way in. That is deliberate — but it does mean there has to be
*some* channel for looking at logs and restarting the service during the judging
window, and this is it.

    python deploy/remote_exec.py --status
    python deploy/remote_exec.py --logs
    python deploy/remote_exec.py --restart
    python deploy/remote_exec.py -- 'df -h; free -m'

Requires AmazonSSMManagedInstanceCore on the instance role.
"""

from __future__ import annotations

import argparse
import os
import time

import boto3

REGION = "us-east-1"
NAME = "clinic-voice-agent"

STATUS = [
    "echo ACTIVE=$(systemctl is-active clinic)",
    "systemctl --no-pager -l status clinic | head -14 || true",
    "echo; echo --- local ping ---",
    r"curl -sS -o /dev/null -w 'ping=%{http_code}\n' http://127.0.0.1/ping || true",
    "echo; echo --- memory ---",
    "free -m",
]

LOGS = ["tail -60 /var/log/clinic.log || echo 'no log yet'"]

RESTART = [
    "systemctl daemon-reload",
    "systemctl enable --now clinic",
    "sleep 12",
    "echo ACTIVE=$(systemctl is-active clinic)",
    r"curl -sS -o /dev/null -w 'ping=%{http_code}\n' http://127.0.0.1/ping || true",
]

BUNDLE_KEY = "deploy/clinic-front-desk-source.tar.gz"

#: Bucket naming pattern, resolved from the caller's own account at run time
#: rather than hardcoded: an account id committed to a public repository cannot be
#: taken back out of git history, and a derived name works in any account.
#: ``CLINIC_RECORDINGS_BUCKET`` overrides it.
BUCKET_PATTERN = "clinic-recordings-{account}"


def resolve_bucket() -> str:
    override = os.environ.get("CLINIC_RECORDINGS_BUCKET")
    if override:
        return override
    account = boto3.client("sts").get_caller_identity()["Account"]
    return BUCKET_PATTERN.format(account=account)


def update_commands() -> list[str]:
    """Pull the current source bundle onto the instance and restart.

    The instance downloads the bundle in its boot script, so re-uploading to S3
    alone changes nothing on a machine that is already running. The package is
    installed editable, so overwriting the source files is enough — no reinstall,
    unless the dependencies changed.
    """
    bucket = resolve_bucket()
    return [
        f"aws s3 cp s3://{bucket}/{BUNDLE_KEY} /tmp/src.tar.gz --region {REGION}",
        "tar xzf /tmp/src.tar.gz -C /opt/clinic",
        "rm -f /tmp/src.tar.gz",
        "systemctl restart clinic",
        "sleep 12",
        "echo ACTIVE=$(systemctl is-active clinic)",
        r"curl -sS -o /dev/null -w 'ping=%{http_code}\n' http://127.0.0.1/ping || true",
        "echo; echo --- dictated-number handling, on the deployed code ---",
        # Proof the running instance has the fix, not just that S3 does. Written to
        # a file rather than passed with -c: quoting survives fewer layers here.
        "cat >/tmp/check_dictation.py <<'PY'\n"
        "from clinic_front_desk.models.matching import ("
        "normalize_patient_code, normalize_phone)\n"
        "spoken_phone = 'nine nine zero zero zero one two three zero seven'\n"
        "print('phone', repr(normalize_phone(spoken_phone)))\n"
        "print('code ', repr(normalize_patient_code('s i three zero seven')))\n"
        "PY",
        "/opt/clinic/venv/bin/python /tmp/check_dictation.py",
        "rm -f /tmp/check_dictation.py",
    ]


def find_instance(ec2: object) -> str:
    reservations = ec2.describe_instances(  # type: ignore[attr-defined]
        Filters=[
            {"Name": "tag:Name", "Values": [NAME]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )["Reservations"]
    if not reservations:
        raise SystemExit(f"no running instance tagged {NAME}")
    return str(reservations[0]["Instances"][0]["InstanceId"])


def wait_registered(ssm: object, instance_id: str, timeout: int = 420) -> None:
    deadline = time.time() + timeout
    announced = False
    while time.time() < deadline:
        info = ssm.describe_instance_information(  # type: ignore[attr-defined]
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
        )["InstanceInformationList"]
        if info:
            return
        if not announced:
            print("  waiting for the SSM agent to register ...", flush=True)
            announced = True
        time.sleep(10)
    raise SystemExit(
        "the SSM agent never registered. Confirm AmazonSSMManagedInstanceCore "
        "is attached to the instance role."
    )


def run(commands: list[str]) -> int:
    ec2 = boto3.client("ec2", region_name=REGION)
    ssm = boto3.client("ssm", region_name=REGION)

    instance_id = find_instance(ec2)
    wait_registered(ssm, instance_id)

    sent = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": commands},
        TimeoutSeconds=600,
    )
    command_id = sent["Command"]["CommandId"]

    while True:
        result = ssm.get_command_invocation(
            CommandId=command_id, InstanceId=instance_id
        )
        if result["Status"] not in ("Pending", "InProgress", "Delayed"):
            break
        time.sleep(5)

    out = result.get("StandardOutputContent", "")
    err = result.get("StandardErrorContent", "")
    if out.strip():
        print(out.rstrip())
    if err.strip():
        print("--- stderr ---")
        print(err.rstrip()[-4000:])
    if result["Status"] != "Success":
        print(f"[{result['Status']}]")
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--status", action="store_true", help="service + local ping")
    group.add_argument("--logs", action="store_true", help="tail the app log")
    group.add_argument("--restart", action="store_true", help="reload and start")
    group.add_argument(
        "--update",
        action="store_true",
        help="pull the current source bundle from S3, restart, and self-check",
    )
    group.add_argument("command", nargs="?", help="an arbitrary shell command")
    args = parser.parse_args()

    if args.status:
        commands = STATUS
    elif args.logs:
        commands = LOGS
    elif args.restart:
        commands = RESTART
    elif args.update:
        commands = update_commands()
    else:
        commands = [args.command]

    raise SystemExit(run(commands))


if __name__ == "__main__":
    main()
