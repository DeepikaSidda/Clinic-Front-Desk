"""``CallSessionStore`` — call-session data access (Req 11, 12, 15, 16).

Contract:
    - **Atomicity / non-destruction (Req 16.6).** A failed ``create`` or
      ``finalize`` returns an ``Err`` and leaves prior sessions unchanged.
    - **Recent-first log (Req 15.2).** ``list_recent`` returns the most recent
      sessions first, for the dashboard call-activity log.
    - **Change emission (Req 16.6, 15.4).** A successful ``create`` emits a
      ``CREATED`` and ``finalize`` an ``UPDATED`` call-session event.
    - **Empty initialization (Req 16.4).** Before any write, ``list_recent``
      returns ``Ok([])``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from clinic_front_desk.models import (
    CallOutcome,
    CallSession,
    ISODateTime,
    PatientRef,
    StoreResult,
)

from .inputs import NewCallSession


class CallSessionStore(ABC):
    """Read/write interface for :class:`~clinic_front_desk.models.CallSession` records."""

    @abstractmethod
    def create(self, s: NewCallSession) -> StoreResult[CallSession]:
        """Open a new call-session record."""
        raise NotImplementedError

    @abstractmethod
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
        """Persist the session outcome and patient-provided identity on end (Req 11.5, 12.7).

        Args:
            id: The Call_Session to finalize.
            outcome: The terminal outcome.
            patient_info: Patient-provided identity, as far as it was captured.
            ended_at: When the call ended. Supplied by the caller rather than
                defaulted here: two implementations each reading their own clock
                would produce different values for the same operation, breaking
                storage-swap equivalence (Req 16.5). ``None`` leaves the field
                unchanged. :func:`~clinic_front_desk.voice.session_lifecycle.finalize_session`
                is the caller that supplies it, from one injectable clock.
            transcript: The rendered turn-by-turn transcript, when the call was
                transcribed.
            recording_uri: Where the call audio was stored, when the call was
                recorded. ``None`` leaves any existing value untouched, so a
                failed upload does not erase a previously recorded URI.
        """
        raise NotImplementedError

    @abstractmethod
    def list_recent(self, limit: int) -> StoreResult[list[CallSession]]:
        """Return up to ``limit`` most-recent-first sessions (Req 15.2)."""
        raise NotImplementedError


__all__ = ["CallSessionStore"]
