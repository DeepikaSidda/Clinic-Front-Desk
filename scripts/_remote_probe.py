"""Throwaway: can the instance role actually call Polly and Transcribe?

Runs through remote_exec so the check uses the instance's own credentials, which is
the whole point — Polly and Transcribe were verified locally with admin credentials,
which proves nothing about what the deployed agent may do.
"""

from __future__ import annotations

import subprocess
import sys

COMMAND = " ; ".join(
    [
        "echo === POLLY ===",
        (
            "aws polly synthesize-speech --region us-east-1 --text hello "
            "--output-format pcm --voice-id Joanna --engine neural "
            "--sample-rate 16000 /tmp/polly.pcm >/tmp/polly.out 2>&1 "
            "&& echo POLLY=OK || (echo POLLY=DENIED ; tail -2 /tmp/polly.out)"
        ),
        "echo === TRANSCRIBE ===",
        (
            "aws transcribe list-transcription-jobs --region us-east-1 "
            "--max-results 1 >/tmp/tr.out 2>&1 "
            "&& echo TRANSCRIBE=OK || (echo TRANSCRIBE=DENIED ; tail -2 /tmp/tr.out)"
        ),
        "echo === LOG ===",
        "echo polly_failures=$(grep -c 'Polly synthesis failed' /var/log/clinic.log)",
    ]
)

result = subprocess.run(
    [sys.executable, "deploy/remote_exec.py", COMMAND],
    capture_output=True,
    text=True,
)
print(result.stdout[-3000:])
if result.returncode != 0:
    print(result.stderr[-1500:])
