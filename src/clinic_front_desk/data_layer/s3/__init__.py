"""Amazon S3 Data_Layer implementations.

Only call audio lives here. Every other persisted record type fits the DynamoDB
single-table design; a few minutes of speech does not (it is megabytes against a
400 KB item limit), so recordings get object storage and the ``CallSession`` keeps
a ``recording_uri`` pointing at them.

Like the DynamoDB package, this is an *implementation* of a Data_Layer interface —
callers depend on
:class:`~clinic_front_desk.data_layer.interfaces.CallRecordingStore`, never on this
module, so storage stays swappable (Req 16.5).
"""

from __future__ import annotations

from typing import Any

from .call_recording_store import DEFAULT_PREFIX, S3CallRecordingStore
from .clinic_document_store import (
    DEFAULT_PREFIX as DEFAULT_DOCUMENT_PREFIX,
)
from .clinic_document_store import S3ClinicDocumentStore

__all__ = [
    "S3CallRecordingStore",
    "DEFAULT_PREFIX",
    "create_recording_store",
    "S3ClinicDocumentStore",
    "DEFAULT_DOCUMENT_PREFIX",
    "create_document_store",
]


def create_recording_store(
    bucket: str,
    *,
    region: str | None = None,
    prefix: str = DEFAULT_PREFIX,
    sse: str | None = "AES256",
    kms_key_id: str | None = None,
    client: Any | None = None,
) -> S3CallRecordingStore:
    """Build an :class:`S3CallRecordingStore`, creating a boto3 client if needed.

    boto3 is imported lazily so the rest of the system stays importable without
    the AWS SDK present, matching the DynamoDB package's boundary.
    """
    if client is None:
        import boto3  # type: ignore[import-untyped]
        from botocore.config import Config  # type: ignore[import-untyped]

        # Force SigV4 presigning. botocore may otherwise fall back to the legacy
        # SigV2 scheme, whose presigned URLs are rejected outright by every S3
        # region created after 2014 — playback would fail only in some regions,
        # which is a miserable thing to debug in deployment.
        client = boto3.client(
            "s3",
            region_name=region,
            config=Config(signature_version="s3v4"),
        )
    return S3CallRecordingStore(
        bucket, client, prefix=prefix, sse=sse, kms_key_id=kms_key_id
    )


def create_document_store(
    bucket: str,
    *,
    region: str | None = None,
    prefix: str = DEFAULT_DOCUMENT_PREFIX,
    sse: str | None = "AES256",
    kms_key_id: str | None = None,
    client: Any | None = None,
) -> S3ClinicDocumentStore:
    """Build an :class:`S3ClinicDocumentStore`, creating a boto3 client if needed.

    May share a bucket with recordings — the prefixes differ, and so should their
    lifecycle rules: recordings expire on a retention schedule, clinic documents
    should persist until the doctor removes them.
    """
    if client is None:
        # Already imported (and ignore-annotated) by create_recording_store above.
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3", region_name=region, config=Config(signature_version="s3v4")
        )
    return S3ClinicDocumentStore(
        bucket, client, prefix=prefix, sse=sse, kms_key_id=kms_key_id
    )
