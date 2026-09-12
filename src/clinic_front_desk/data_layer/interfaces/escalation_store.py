"""``EscalationStore`` — human-escalation data access (Req 9, 15, 16).

Contract:
    - **Atomicity / non-destruction (Req 16.6).** A failed ``create`` returns an
      ``Err`` and persists no partial escalation (Req 9.9).
    - **Recent-first log (Req 15.2, 9.6).** ``list_recent`` returns the most
      recent escalations first, for surfacing in the dashboard activity log.
    - **Change emission (Req 16.6, 9.6).** A successful ``create`` emits a
      ``CREATED`` escalation event (drives the ≤ 5 s activity-log surfacing).
    - **Empty initialization (Req 16.4).** Before any write, ``list_recent``
      returns ``Ok([])``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from clinic_front_desk.models import Escalation, StoreResult

from .inputs import NewEscalation


class EscalationStore(ABC):
    """Read/write interface for :class:`~clinic_front_desk.models.Escalation` records."""

    @abstractmethod
    def create(self, e: NewEscalation) -> StoreResult[Escalation]:
        """Record an escalation with reason, patient identity when known, and context (Req 9.4)."""
        raise NotImplementedError

    @abstractmethod
    def list_recent(self, limit: int) -> StoreResult[list[Escalation]]:
        """Return up to ``limit`` most-recent-first escalations (Req 15.2, 9.6)."""
        raise NotImplementedError


__all__ = ["EscalationStore"]
