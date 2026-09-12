"""In-memory :class:`CallRecordingStore` fake (Req 16.2, 16.4, 16.5).

Holds recordings in a dict so the whole recording path — capture, render, upload,
pointer on the Call_Session, playback in the dashboard — runs in tests and local
demos with no S3 bucket and no credentials, observably identically to the real
store (Req 16.5).

Audio is bytes, so this fake will hold whatever it is given for the process
lifetime. That is fine for tests and a demo; it is emphatically not a deployment
backend, which is why ``playback_url`` returns a ``memory://`` URI that only this
process can resolve.
"""

from __future__ import annotations

from clinic_front_desk.data_layer.events import ChangeEmitter
from clinic_front_desk.data_layer.interfaces import (
    DEFAULT_PLAYBACK_URL_TTL_SECONDS,
    CallRecordingStore,
)
from clinic_front_desk.models import Ok, RecordingRef, StoreResult

from ._support import MemoryStoreBase


def memory_recording_uri(call_session_id: str) -> str:
    """The pointer stored on the CallSession for an in-memory recording."""
    return f"memory://recordings/{call_session_id}.wav"


class MemoryCallRecordingStore(CallRecordingStore, MemoryStoreBase):
    """A dict-backed :class:`CallRecordingStore` honouring the full contract."""

    def __init__(self, emitter: ChangeEmitter | None = None) -> None:
        MemoryStoreBase.__init__(self, emitter)
        self._audio: dict[str, bytes] = {}
        self._content_types: dict[str, str] = {}

    def put(
        self,
        call_session_id: str,
        audio: bytes,
        *,
        content_type: str = "audio/wav",
        started_at: str | None = None,
    ) -> StoreResult[RecordingRef]:
        # Overwrite by session id, so a retried upload leaves one copy.
        self._audio[call_session_id] = bytes(audio)
        self._content_types[call_session_id] = content_type
        return Ok(
            RecordingRef(
                call_session_id=call_session_id,
                uri=memory_recording_uri(call_session_id),
                byte_size=len(audio),
                content_type=content_type,
            )
        )

    def get(self, call_session_id: str) -> StoreResult[bytes | None]:
        return Ok(self._audio.get(call_session_id))

    def playback_url(
        self,
        call_session_id: str,
        *,
        expires_in: int = DEFAULT_PLAYBACK_URL_TTL_SECONDS,
    ) -> StoreResult[str | None]:
        if call_session_id not in self._audio:
            return Ok(None)
        return Ok(memory_recording_uri(call_session_id))

    # -- test/demo helpers (not part of the interface) ----------------------

    @property
    def recorded_session_ids(self) -> list[str]:
        """Session ids that have a stored recording."""
        return sorted(self._audio)


__all__ = ["MemoryCallRecordingStore", "memory_recording_uri"]
