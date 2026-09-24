"""Publish (or unpublish) the live-call console on the deployed voice agent.

Why this is needed at all: live calls are tracked in memory, per process. A caller on
the public URL is registered inside that container, so a console running anywhere else
sees an empty list however much it is permitted to see. To take a real call, the
console has to be served by the process holding it.

What it publishes is narrow on purpose — the live console only. Stored patient
records, the calendar, documents, onboarding and the ``/invocations`` tool surface stay
unrouted even with a valid token. The exposure is calls in progress, not the clinic's
history.

The secret is written to ``/etc/clinic-console.env`` on the instance, root-readable
only, and picked up by the systemd unit's ``EnvironmentFile``. It is never written into
this repo.

    python deploy/publish_console.py              # publish, using .secrets/console_token.txt
    python deploy/publish_console.py --revoke     # remove it; back to voice-only
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

TOKEN_FILE = Path(".secrets/console_token.txt")
REMOTE_ENV = "/etc/clinic-console.env"
UNIT_DROPIN = "/etc/systemd/system/clinic.service.d/console.conf"


def remote(command: str) -> int:
    """Run a shell command on the instance over SSM, via remote_exec."""
    return subprocess.call(
        [sys.executable, "deploy/remote_exec.py", command],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--revoke",
        action="store_true",
        help="remove the secret and return the host to voice-only",
    )
    args = parser.parse_args()

    if args.revoke:
        print("  revoking the console on the deployed host")
        command = (
            f"rm -f {REMOTE_ENV} {UNIT_DROPIN}; "
            "systemctl daemon-reload; "
            "systemctl restart clinic; "
            "sleep 6; "
            "systemctl is-active clinic; "
            "curl -sS -o /dev/null -w 'live=%{http_code}\\n' localhost/live; "
            "curl -sS -o /dev/null -w 'voice=%{http_code}\\n' localhost/voice"
        )
        return remote(command)

    if not TOKEN_FILE.exists():
        print(f"  no token at {TOKEN_FILE}")
        print("  run: python scripts/make_console_token.py")
        return 1
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if len(token) < 24:
        print(f"  token is only {len(token)} characters; the server requires 24+")
        return 1

    print(f"  publishing the console with a {len(token)}-character secret")

    # A systemd drop-in as well as the env file: the running unit predates the
    # EnvironmentFile line in deploy_voice_agent.py, and a drop-in applies without
    # rebuilding the instance or rewriting a unit that is currently serving calls.
    command = (
        f"install -m 600 /dev/null {REMOTE_ENV}; "
        f"printf 'CLINIC_CONSOLE_TOKEN=%s\\n' '{token}' > {REMOTE_ENV}; "
        f"chmod 600 {REMOTE_ENV}; "
        f"mkdir -p $(dirname {UNIT_DROPIN}); "
        f"printf '[Service]\\nEnvironmentFile=-{REMOTE_ENV}\\n' > {UNIT_DROPIN}; "
        "systemctl daemon-reload; "
        "systemctl restart clinic; "
        "sleep 8; "
        "systemctl is-active clinic; "
        "echo '--- token is set in the unit environment ---'; "
        "systemctl show clinic -p Environment | "
        "  sed 's/CLINIC_CONSOLE_TOKEN=[^ ]*/CLINIC_CONSOLE_TOKEN=***REDACTED***/'; "
        "echo '--- local probes ---'; "
        "curl -sS -o /dev/null -w 'voice=%{http_code}\\n' localhost/voice; "
        "curl -sS -o /dev/null -w 'live_no_token=%{http_code}\\n' "
        "  'localhost/live?role=doctor'; "
        f"curl -sS -o /dev/null -w 'live_with_token=%{{http_code}}\\n' "
        f"  'localhost/live?role=doctor&k={token}'; "
        f"curl -sS -o /dev/null -w 'slots_with_token=%{{http_code}}\\n' "
        f"  'localhost/slots?role=doctor&k={token}'"
    )
    return remote(command)


if __name__ == "__main__":
    raise SystemExit(main())
