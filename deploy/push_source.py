"""Upload the current source bundle to S3, without touching any infrastructure.

``remote_exec.py --update`` pulls the bundle S3 already holds, so on its own it
redeploys whatever was last uploaded — which is how a code change can appear to
deploy cleanly while the instance keeps running a month-old file. Running the full
``deploy_voice_agent.py`` would re-upload, but it also walks IAM, the security
group, the instance and the distribution, which is a lot of blast radius for a
one-line view fix.

    python deploy/push_source.py                  # upload only
    python deploy/push_source.py --update         # upload, then pull and restart
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy"))

import deploy_voice_agent as dva  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update",
        action="store_true",
        help="after uploading, run remote_exec.py --update to pull it and restart",
    )
    args = parser.parse_args()

    # The bucket name is derived from the caller's account rather than committed,
    # and the deploy script fills it in its own main(). Do the same here, or
    # upload_bundle is handed an empty bucket name.
    account = boto3.client("sts").get_caller_identity()["Account"]
    dva.RECORDINGS_BUCKET = dva.resolve_bucket(account)

    print("SOURCE")
    dva.upload_bundle(boto3.client("s3"))
    print(f"  s3://{dva.RECORDINGS_BUCKET}/{dva.BUNDLE_KEY}")

    if args.update:
        print("INSTANCE")
        subprocess.run(
            [sys.executable, str(ROOT / "deploy" / "remote_exec.py"), "--update"],
            check=False,
        )


if __name__ == "__main__":
    main()
