"""DynamoDB :class:`CallSessionStore` (task 4.1, Req 11, 12, 15, 16).

Single-table layout: ``PK=CALLSESSION``, ``SK=<startedAt>#<id>``. All sessions
share one partition, so :meth:`list_recent` queries that partition and returns
sessions most-recent-first (descending ``started_at``, then descending ``id`` as
a stable tiebreak), capped at ``limit`` (Req 15.2). :meth:`finalize` leaves the
SK unchanged (the started-at/id are fixed), so it overwrites the item in place.
"""

from __future__ import annotations

from typing import Any

from clinic_front_desk.data_layer.events import ChangeEmitter, ChangeEntity, ChangeKind
from clinic_front_desk.data_layer.interfaces import CallSessionStore, NewCallSession
from clinic_front_desk.models import (
    CALLSESSION_PK,
    CallOutcome,
    CallSession,
    Err,
    ISODateTime,
    Ok,
    PatientRef,
    StoreError,
    StoreErrorKind,
    StoreResult,
    call_session_from_item,
    call_session_to_item,
)

from ._support import DynamoStoreBase, Key

_STORE = "DynamoCallSessionStore"


class DynamoCallSessionStore(CallSessionStore, DynamoStoreBase):
    """Single-table :class:`CallSessionStore` honouring the full store contract."""

    def __init__(self, table: Any, emitter: ChangeEmitter | None = None) -> None:
        DynamoStoreBase.__init__(self, table, emitter)

    def create(self, s: NewCallSession) -> StoreResult[CallSession]:
        item = call_session_to_item(s)
        # Replace-by-id semantics, matching the in-memory store (Req 16.5).
        #
        # The SK embeds ``started_at``, so creating twice for the same id — which a
        # reconnect on the same AgentCore runtime session id does — otherwise writes
        # *two* items. Observed against the real table: the duplicate left a phantom
        # call in the activity log with no outcome, and ``finalize`` updated the
        # older of the two, so the live call's outcome landed on the wrong record.
        existing = self._find_by_entity_id("CallSession", s.id)
        if existing is not None and (existing["PK"], existing["SK"]) != (
            item["PK"],
            item["SK"],
        ):
            self._delete(existing["PK"], existing["SK"])
        self._put(item)
        self._emit(ChangeEntity.CALL_SESSION, s.id, ChangeKind.CREATED)
        return Ok(call_session_from_item(item))

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
        item = self._find_by_entity_id("CallSession", id)
        if item is None:
            return Err(
                StoreError(
                    kind=StoreErrorKind.NOT_FOUND,
                    detail=f"call session {id!r} not found",
                    store=_STORE,
                )
            )
        session = call_session_from_item(item)
        session.outcome = outcome
        session.patient_ref = patient_info
        if ended_at is not None:
            session.ended_at = ended_at
        # Only overwrite when supplied, so a failed transcript render or recording
        # upload cannot erase a value already stored.
        if transcript is not None:
            session.transcript = transcript
        if recording_uri is not None:
            session.recording_uri = recording_uri
        new_item = call_session_to_item(session)
        self._put(new_item)
        self._emit(ChangeEntity.CALL_SESSION, id, ChangeKind.UPDATED)
        return Ok(call_session_from_item(new_item))

    def list_recent(self, limit: int) -> StoreResult[list[CallSession]]:
        items = self._query(
            Key("PK").eq(CALLSESSION_PK),
            scan_index_forward=False,
        )
        sessions = [call_session_from_item(i) for i in items]
        sessions.sort(key=lambda s: (s.started_at, s.id), reverse=True)
        capped = sessions[:limit] if limit >= 0 else sessions
        return Ok(capped)


__all__ = ["DynamoCallSessionStore"]
