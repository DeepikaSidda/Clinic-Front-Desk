"""In-memory :class:`CallSessionStore` fake (task 3.2, Req 11, 12, 15, 16).

:meth:`list_recent` returns sessions most-recent-first for the dashboard
call-activity log (Req 15.2); a store-managed insertion counter breaks ties on
equal ``started_at``.
"""

from __future__ import annotations

from clinic_front_desk.data_layer.events import ChangeEmitter, ChangeEntity, ChangeKind
from clinic_front_desk.data_layer.interfaces import CallSessionStore, NewCallSession
from clinic_front_desk.models import (
    CallOutcome,
    CallSession,
    ISODateTime,
    Ok,
    PatientRef,
    StoreResult,
)

from ._support import MemoryStoreBase, not_found_err

_STORE = "MemoryCallSessionStore"


class MemoryCallSessionStore(CallSessionStore, MemoryStoreBase):
    """A dict-backed :class:`CallSessionStore` honouring the full store contract."""

    def __init__(self, emitter: ChangeEmitter | None = None) -> None:
        MemoryStoreBase.__init__(self, emitter)
        self._sessions: dict[str, CallSession] = {}
        self._insertion: dict[str, int] = {}
        self._next_insertion: int = 0

    def create(self, s: NewCallSession) -> StoreResult[CallSession]:
        self._sessions[s.id] = self._copy(s)
        self._insertion[s.id] = self._next_insertion
        self._next_insertion += 1
        self._emit(ChangeEntity.CALL_SESSION, s.id, ChangeKind.CREATED)
        return Ok(self._copy(self._sessions[s.id]))

    def finalize(
        self,
        id: str,
        outcome: CallOutcome,
        patient_info: PatientRef,
        *,
        ended_at: ISODateTime | None = None,
        transcript: str | None = None,
        recording_uri: str | None = None,
    ) -> StoreResult[CallSession]:
        session = self._sessions.get(id)
        if session is None:
            return not_found_err(_STORE, f"call session {id!r} not found")
        session.outcome = outcome
        session.patient_ref = self._copy(patient_info)
        if ended_at is not None:
            session.ended_at = ended_at
        # Only overwrite when a value is supplied, so a failed transcript render
        # or recording upload cannot erase one already stored.
        if transcript is not None:
            session.transcript = transcript
        if recording_uri is not None:
            session.recording_uri = recording_uri
        self._emit(ChangeEntity.CALL_SESSION, id, ChangeKind.UPDATED)
        return Ok(self._copy(session))

    def list_recent(self, limit: int) -> StoreResult[list[CallSession]]:
        ordered = sorted(
            self._sessions.values(),
            key=lambda s: (s.started_at, self._insertion[s.id]),
            reverse=True,
        )
        capped = ordered[:limit] if limit >= 0 else ordered
        return Ok([self._copy(s) for s in capped])


__all__ = ["MemoryCallSessionStore"]
