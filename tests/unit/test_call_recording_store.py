"""Tests for the ``CallRecordingStore`` implementations (Req 16.2, 16.4, 16.5, 16.6).

Both implementations are held to the same observable contract, since the point of
the interface is that storage is swappable. The S3 store runs against a
moto-mocked bucket, so the real key layout, encryption request, and presigned-URL
path are exercised without touching AWS.
"""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from moto import mock_aws

from clinic_front_desk.data_layer.memory import (
    MemoryCallRecordingStore,
    memory_recording_uri,
)
from clinic_front_desk.data_layer.s3 import S3CallRecordingStore
from clinic_front_desk.models import is_err, is_ok

BUCKET = "clinic-recordings-test"
REGION = "us-east-1"
AUDIO = b"RIFF....WAVEfake-audio-bytes"


@pytest.fixture()
def memory_store() -> MemoryCallRecordingStore:
    return MemoryCallRecordingStore()


@pytest.fixture()
def s3_store() -> Any:
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET)
        yield S3CallRecordingStore(BUCKET, client)


# ---------------------------------------------------------------------------
# Shared contract
# ---------------------------------------------------------------------------


def test_memory_empty_init_returns_none(memory_store: MemoryCallRecordingStore) -> None:
    """Req 16.4: reads before any write succeed with nothing, not an error."""
    got = memory_store.get("no-such-call")
    url = memory_store.playback_url("no-such-call")

    assert is_ok(got) and got.value is None
    assert is_ok(url) and url.value is None


def test_s3_empty_init_returns_none(s3_store: Any) -> None:
    got = s3_store.get("no-such-call")
    url = s3_store.playback_url("no-such-call")

    assert is_ok(got) and got.value is None
    assert is_ok(url) and url.value is None


def test_memory_round_trip(memory_store: MemoryCallRecordingStore) -> None:
    put = memory_store.put("call-1", AUDIO)

    assert is_ok(put)
    assert put.value.uri == memory_recording_uri("call-1")
    assert put.value.byte_size == len(AUDIO)
    got = memory_store.get("call-1")
    assert is_ok(got) and got.value == AUDIO


def test_s3_round_trip(s3_store: Any) -> None:
    put = s3_store.put("call-1", AUDIO, started_at="2026-03-04T09:00:00Z")

    assert is_ok(put)
    assert put.value.uri.startswith(f"s3://{BUCKET}/")
    assert put.value.byte_size == len(AUDIO)
    got = s3_store.get("call-1")
    assert is_ok(got) and got.value == AUDIO


def test_put_is_idempotent_per_call(memory_store: MemoryCallRecordingStore) -> None:
    """A retried upload must overwrite, not accumulate a second copy."""
    memory_store.put("call-1", AUDIO)
    memory_store.put("call-1", b"second-attempt")

    assert memory_store.recorded_session_ids == ["call-1"]
    got = memory_store.get("call-1")
    assert is_ok(got) and got.value == b"second-attempt"


def test_s3_put_is_idempotent_per_call(s3_store: Any) -> None:
    started = "2026-03-04T09:00:00Z"
    first = s3_store.put("call-1", AUDIO, started_at=started)
    second = s3_store.put("call-1", b"second-attempt", started_at=started)

    assert is_ok(first) and is_ok(second)
    assert first.value.uri == second.value.uri
    got = s3_store.get("call-1")
    assert is_ok(got) and got.value == b"second-attempt"


# ---------------------------------------------------------------------------
# S3 specifics: key layout, encryption, presigning, failure
# ---------------------------------------------------------------------------


def test_s3_keys_are_laid_out_by_call_date(s3_store: Any) -> None:
    """The date path is what a single lifecycle rule uses to expire old audio."""
    put = s3_store.put("call-1", AUDIO, started_at="2026-03-04T09:00:00Z")

    assert is_ok(put)
    assert put.value.uri == f"s3://{BUCKET}/call-recordings/2026/03/04/call-1.wav"


def test_s3_falls_back_to_today_for_an_unparseable_start(s3_store: Any) -> None:
    put = s3_store.put("call-1", AUDIO, started_at="not-a-timestamp")

    assert is_ok(put)
    assert put.value.uri.endswith("/call-1.wav")


def test_s3_requests_server_side_encryption(s3_store: Any) -> None:
    """A bucket created without default encryption would otherwise store PHI clear."""
    s3_store.put("call-1", AUDIO, started_at="2026-03-04T09:00:00Z")

    head = boto3.client("s3", region_name=REGION).head_object(
        Bucket=BUCKET, Key="call-recordings/2026/03/04/call-1.wav"
    )

    assert head["ServerSideEncryption"] == "AES256"
    assert head["ContentType"] == "audio/wav"


def test_s3_playback_url_is_presigned_and_expiring(s3_store: Any) -> None:
    s3_store.put("call-1", AUDIO, started_at="2026-03-04T09:00:00Z")

    url = s3_store.playback_url("call-1", expires_in=60)

    assert is_ok(url)
    assert url.value is not None
    assert "call-recordings/2026/03/04/call-1.wav" in url.value
    # Presigned (the browser needs no credentials) and time-limited. Asserted
    # without pinning the signature version, since that is a property of the
    # injected client's botocore config, not of this store.
    assert "Signature" in url.value
    assert "Expires" in url.value


def test_create_recording_store_forces_sigv4_presigning() -> None:
    """SigV2 presigned URLs are rejected by every region created after 2014."""
    from clinic_front_desk.data_layer.s3 import create_recording_store

    with mock_aws():
        store = create_recording_store(BUCKET, region=REGION)

    assert store._client.meta.config.signature_version == "s3v4"


def test_s3_resolves_a_key_written_by_another_process(s3_store: Any) -> None:
    """Playback may be served by a process that did not perform the upload."""
    s3_store.put("call-1", AUDIO, started_at="2026-03-04T09:00:00Z")
    fresh = S3CallRecordingStore(BUCKET, s3_store._client)

    got = fresh.get("call-1")

    assert is_ok(got) and got.value == AUDIO


def test_s3_put_failure_is_an_err_not_an_exception() -> None:
    """Req 16.6: losing a recording must never fail the call."""
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        # Bucket deliberately not created.
        store = S3CallRecordingStore("nonexistent-bucket", client)

        result = store.put("call-1", AUDIO)

    assert is_err(result)
    assert "failed to store recording" in result.error.detail
