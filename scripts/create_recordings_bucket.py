"""Provision the S3 bucket for call recordings, with the controls it needs.

Call audio is PHI, so the bucket is created with protections rather than left to
be hardened later:

- **All public access blocked.** Recordings must never be reachable without a
  signed request.
- **Default encryption at rest** (SSE-S3, or SSE-KMS with ``--kms-key-id``), with
  an S3 Bucket Key to cut KMS request costs.
- **A lifecycle rule that expires recordings** after a retention period. This is
  the only thing that deletes audio — the application never does, which is why the
  object keys are laid out by date.
- **Versioning left off, deliberately.** With versioning on, a lifecycle
  expiration only adds a delete marker and the audio is retained as a
  noncurrent version — the opposite of what a retention policy is for.
- **TLS-only bucket policy**, so audio is never fetched over plain HTTP.

Idempotent: re-running against an existing bucket re-applies the settings rather
than failing.

    python scripts/create_recordings_bucket.py --bucket clinic-recordings-<account>
    python scripts/create_recordings_bucket.py --bucket ... --retention-days 30
"""

from __future__ import annotations

import argparse
import json
import sys

import boto3
from botocore.exceptions import ClientError

PREFIX = "call-recordings/"


def bucket_exists(s3: object, bucket: str) -> bool:
    try:
        s3.head_bucket(Bucket=bucket)  # type: ignore[attr-defined]
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] in {"404", "NoSuchBucket", "NotFound"}:
            return False
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True, help="bucket name to create")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument(
        "--retention-days",
        type=int,
        default=90,
        help="days after which recordings expire (default: %(default)s)",
    )
    parser.add_argument(
        "--kms-key-id",
        default=None,
        help="use SSE-KMS with this key instead of SSE-S3",
    )
    args = parser.parse_args(argv)

    s3 = boto3.client("s3", region_name=args.region)

    if bucket_exists(s3, args.bucket):
        print(f"bucket {args.bucket!r} already exists; re-applying settings")
    else:
        print(f"creating bucket {args.bucket!r} in {args.region} ...")
        # us-east-1 must NOT be given a LocationConstraint; every other region must.
        if args.region == "us-east-1":
            s3.create_bucket(Bucket=args.bucket)
        else:
            s3.create_bucket(
                Bucket=args.bucket,
                CreateBucketConfiguration={"LocationConstraint": args.region},
            )

    print("  blocking all public access ...")
    s3.put_public_access_block(
        Bucket=args.bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )

    if args.kms_key_id:
        print(f"  enabling default SSE-KMS encryption ({args.kms_key_id}) ...")
        rule = {
            "ApplyServerSideEncryptionByDefault": {
                "SSEAlgorithm": "aws:kms",
                "KMSMasterKeyID": args.kms_key_id,
            },
            "BucketKeyEnabled": True,
        }
    else:
        print("  enabling default SSE-S3 encryption ...")
        rule = {
            "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
            "BucketKeyEnabled": True,
        }
    s3.put_bucket_encryption(
        Bucket=args.bucket, ServerSideEncryptionConfiguration={"Rules": [rule]}
    )

    print(f"  expiring {PREFIX}* after {args.retention_days} days ...")
    s3.put_bucket_lifecycle_configuration(
        Bucket=args.bucket,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "expire-call-recordings",
                    "Status": "Enabled",
                    "Filter": {"Prefix": PREFIX},
                    "Expiration": {"Days": args.retention_days},
                    # Clean up interrupted multipart uploads so they cannot linger
                    # as unbilled-but-charged storage.
                    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
                }
            ]
        },
    )

    print("  requiring TLS for every request ...")
    s3.put_bucket_policy(
        Bucket=args.bucket,
        Policy=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "DenyInsecureTransport",
                        "Effect": "Deny",
                        "Principal": "*",
                        "Action": "s3:*",
                        "Resource": [
                            f"arn:aws:s3:::{args.bucket}",
                            f"arn:aws:s3:::{args.bucket}/*",
                        ],
                        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                    }
                ],
            }
        ),
    )

    print(f"\ndone. set CLINIC_RECORDINGS_BUCKET={args.bucket}")
    print(
        "note: versioning is intentionally OFF so the lifecycle rule actually "
        "deletes expired audio."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
