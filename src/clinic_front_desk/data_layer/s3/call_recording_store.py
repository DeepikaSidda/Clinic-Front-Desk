"""Amazon S3 :class:`CallRecordingStore` (Req 16.2, 16.5, 16.6).

Call audio is stored as one object per Call_Session:

    s3://<bucket>/<prefix>/<YYYY>/<MM>/<DD>/<call_session_id>.wav

The date path is deliberate: it lets a single S3 **lifecycle rule** expire
recordings after a retention period without the application tracking ages, which
is the practical way to bound how long patient audio is kept. Keying the object on
the Call_Session id makes ``put`` idempotent, so a retried upload overwrites
rather than accumulating copies.

Encryption is requested on every write (``AES256`` by default, or ``aws:kms`` with
a supplied key id). This is belt-and-braces: bucket default encryption should also
be enabled, but a bucket created without it would otherwise store patient audio in
the clear.

``playback_url`` issues a presigned GET so the dashboard can play a recording
without proxying megabytes through the app or handing the browser credentials.
Presigned URLs are bearer capabilities — anyone holding one can fetch the audio
until it expires — hence the deliberately short default TTL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from clinic_front_desk.data_layer.interfaces import (
    DEFAULT_PLAYBACK_URL_TTL_SECONDS,
    CallRecordingStore,
)
from clinic_front_desk.models import (
    Err,
    Ok,
    RecordingRef,
    StoreError,
    StoreErrorKind,
    StoreResult,
)

_STORE = "S3CallRecordingStore"

#: Default key prefix inside the bucket.
DEFAULT_PREFIX = "call-recordings"


def _date_path(started_at: str | None) -> str:
    """Return ``YYYY/MM/DD`` for the call's start, falling back to today (UTC)."""
    if started_at:
        try:
            parsed = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.now(UTC)
    else:
        parsed = datetime.now(UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y/%m/%d")


class S3CallRecordingStore(CallRecordingStore):
    """Stores Call_Session audio as S3 objects.

    Args:
        bucket: The bucket recordings are written to.
        client: A boto3 S3 client. Injected so tests can use a moto-mocked client
            and so this class performs no AWS construction of its own.
        prefix: Key prefix inside the bucket.
        sse: Server-side encryption mode — ``"AES256"`` (S3-managed) or
            ``"aws:kms"``. Pass ``None`` to rely solely on bucket defaults.
        kms_key_id: Required when ``sse`` is ``"aws:kms"``.
    """

    def __init__(
        self,
        bucket: str,
        client: Any,
        *,
        prefix: str = DEFAULT_PREFIX,
        sse: str | None = "AES256",
        kms_key_id: str | None = None,
    ) -> None:
        self._bucket = bucket
        self._client = client
        self._prefix = prefix.strip("/")
        self._sse = sse
        self._kms_key_id = kms_key_id
        # Keys are derived from the call's start date, so reads have to know where
        # a past write landed. Cached on put; recovered by listing when absent
        # (e.g. a different process is serving playback).
        self._keys: dict[str, str] = {}

    # -- key layout ---------------------------------------------------------

    def _key(self, call_session_id: str, started_at: str | None) -> str:
        return f"{self._prefix}/{_date_path(started_at)}/{call_session_id}.wav"

    def uri_for(self, key: str) -> str:
        """The ``s3://`` URI for a key, as stored on the CallSession."""
        return f"s3://{self._bucket}/{key}"

    def _resolve_key(self, call_session_id: str) -> str | None:
        """Find the object key for a call, by cache then by listing.

        The listing fallback is bounded to the id's own suffix, so it is a narrow
        prefix scan rather than a bucket walk. Returns ``None`` when the call has
        no recording.
        """
        cached = self._keys.get(call_session_id)
        if cached is not None:
            return cached
        paginator_kwargs = {
            "Bucket": self._bucket,
            "Prefix": self._prefix,
        }
        try:
            response = self._client.list_objects_v2(**paginator_kwargs)
        except Exception:
            return None
        wanted = f"/{call_session_id}.wav"
        for item in response.get("Contents", []) or []:
            key = str(item.get("Key", ""))
            if key.endswith(wanted):
                self._keys[call_session_id] = key
                return key
        return None

    # -- CallRecordingStore -------------------------------------------------

    def put(
        self,
        call_session_id: str,
        audio: bytes,
        *,
        content_type: str = "audio/wav",
        started_at: str | None = None,
    ) -> StoreResult[RecordingRef]:
        key = self._key(call_session_id, started_at)
        extra: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": audio,
            "ContentType": content_type,
        }
        if self._sse:
            extra["ServerSideEncryption"] = self._sse
            if self._sse == "aws:kms" and self._kms_key_id:
                extra["SSEKMSKeyId"] = self._kms_key_id
        try:
            self._client.put_object(**extra)
        except Exception as exc:  # noqa: BLE001 - surfaced as a store failure
            # Losing a recording must never fail the call, so this is an Err the
            # caller can ignore, not a raise (Req 16.6).
            return Err(
                StoreError(
                    kind=StoreErrorKind.STORE_FAILURE,
                    detail=f"failed to store recording for {call_session_id!r}: {exc}",
                    store=_STORE,
                )
            )
        self._keys[call_session_id] = key
        return Ok(
            RecordingRef(
                call_session_id=call_session_id,
                uri=self.uri_for(key),
                byte_size=len(audio),
                content_type=content_type,
            )
        )

    def get(self, call_session_id: str) -> StoreResult[bytes | None]:
        key = self._resolve_key(call_session_id)
        if key is None:
            return Ok(None)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            body: bytes = response["Body"].read()
        except Exception as exc:  # noqa: BLE001
            return Err(
                StoreError(
                    kind=StoreErrorKind.STORE_FAILURE,
                    detail=f"failed to read recording for {call_session_id!r}: {exc}",
                    store=_STORE,
                )
            )
        return Ok(body)

    def playback_url(
        self,
        call_session_id: str,
        *,
        expires_in: int = DEFAULT_PLAYBACK_URL_TTL_SECONDS,
    ) -> StoreResult[str | None]:
        key = self._resolve_key(call_session_id)
        if key is None:
            return Ok(None)
        try:
            url: str = self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        except Exception as exc:  # noqa: BLE001
            return Err(
                StoreError(
                    kind=StoreErrorKind.STORE_FAILURE,
                    detail=f"failed to sign playback URL for {call_session_id!r}: {exc}",
                    store=_STORE,
                )
            )
        return Ok(url)


__all__ = ["S3CallRecordingStore", "DEFAULT_PREFIX"]
