"""``CallRecordingStore`` — call-audio data access (Req 16.1, 16.5, 16.6).

Call audio is the one persisted record that does not belong in the single
DynamoDB table: a few minutes of speech is megabytes, well past the 400 KB item
limit, and it is written once and read rarely. So it gets its own store interface
backed by object storage (Amazon S3), while the *pointer* to it lives on the
:class:`~clinic_front_desk.models.CallSession` as ``recording_uri``.

Keeping it behind an interface follows the same rule as every other record type
(Req 16.1): the voice agent and the dashboard depend on this interface, never on
boto3 or a bucket name, so swapping storage means swapping the implementation
(Req 16.5).

Contract:
    - **Write-once per call.** ``put`` stores the audio for a Call_Session and
      returns a :class:`~clinic_front_desk.models.RecordingRef` describing where
      it landed. Re-putting the same session id overwrites, so a retried upload
      does not leave two copies.
    - **Atomicity (Req 16.6).** A failed ``put`` returns an ``Err`` and stores
      nothing; the caller keeps the Call_Session and simply records no
      ``recording_uri``. Losing a recording must never fail the call.
    - **Empty initialization (Req 16.4).** Before any write, ``get`` returns
      ``Ok(None)`` rather than an error.
    - **Playback without credentials.** ``playback_url`` returns a
      time-limited URL so the dashboard can play a recording without proxying the
      bytes or handing the browser AWS credentials.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from clinic_front_desk.models import RecordingRef, StoreResult

#: Default lifetime of a playback URL. Short on purpose: the URL grants
#: unauthenticated access to patient audio, so it should outlive a click and
#: little else.
DEFAULT_PLAYBACK_URL_TTL_SECONDS = 300


class CallRecordingStore(ABC):
    """Read/write interface for Call_Session audio recordings."""

    @abstractmethod
    def put(
        self,
        call_session_id: str,
        audio: bytes,
        *,
        content_type: str = "audio/wav",
        started_at: str | None = None,
    ) -> StoreResult[RecordingRef]:
        """Store the audio for a Call_Session (Req 16.6).

        Args:
            call_session_id: The call this recording belongs to.
            audio: The encoded audio bytes (a WAV container by default).
            content_type: MIME type to store alongside the object.
            started_at: The call's start timestamp, used to lay out storage keys
                by date so a bucket lifecycle rule can expire old recordings.

        Returns:
            ``Ok(RecordingRef)`` on success, or ``Err(StoreError)`` having stored
            nothing.
        """
        raise NotImplementedError

    @abstractmethod
    def get(self, call_session_id: str) -> StoreResult[bytes | None]:
        """Return the stored audio for a call, or ``Ok(None)`` if there is none."""
        raise NotImplementedError

    @abstractmethod
    def playback_url(
        self,
        call_session_id: str,
        *,
        expires_in: int = DEFAULT_PLAYBACK_URL_TTL_SECONDS,
    ) -> StoreResult[str | None]:
        """Return a time-limited playback URL, or ``Ok(None)`` if not recorded."""
        raise NotImplementedError


__all__ = ["CallRecordingStore", "DEFAULT_PLAYBACK_URL_TTL_SECONDS"]
