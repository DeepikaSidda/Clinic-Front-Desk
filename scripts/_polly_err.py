"""Throwaway: what is the app's Polly failure, exactly?"""

from __future__ import annotations

import subprocess
import sys

COMMAND = (
    "grep 'Polly synthesis failed' /var/log/clinic.log | tail -3 ; "
    "echo === app voice config === ; "
    "grep -n 'DEFAULT_VOICE_ID\\|DEFAULT_ENGINE\\|POLLY_SAMPLE_RATE' "
    "/opt/clinic/src/clinic_front_desk/handover/live.py | head -6"
)

result = subprocess.run(
    [sys.executable, "deploy/remote_exec.py", COMMAND],
    capture_output=True,
    text=True,
)
print(result.stdout[-3000:])
if result.returncode != 0:
    print(result.stderr[-800:])
