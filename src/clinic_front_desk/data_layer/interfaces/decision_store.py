"""``DecisionStore`` — Practice_Intelligence decision data access (Req 13, 14, 16).

Contract:
    - **Open-feed ordering (Req 14.1).** ``list_open`` returns open decisions
      ordered newest-first (most recently generated first).
    - **Dedupe support (Req 13.3).** ``find_open_by_finding_key`` lets the
      synthesizer avoid creating a second open decision for the same finding.
    - **Atomicity / non-destruction (Req 16.6).** A failed ``create`` or
      ``set_status`` returns an ``Err`` and leaves decisions unchanged; on a
      failed decision persist the finding can be retried without a duplicate
      (Req 13.8).
    - **Change emission (Req 16.6, 14.5, 14.8).** A successful ``create`` emits a
      ``CREATED`` decision event; ``set_status`` emits an ``UPDATED`` decision
      event (feeds the ≤ 5 s add / ≤ 2 s removal dashboard budgets).
    - **Empty initialization (Req 16.4).** Before any write, ``list_open``
      returns ``Ok([])`` and ``find_open_by_finding_key`` returns ``Ok(None)``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from clinic_front_desk.models import (
    Decision,
    DecisionStatus,
    ISODateTime,
    StoreResult,
)

from .inputs import NewDecision


class DecisionStore(ABC):
    """Read/write interface for :class:`~clinic_front_desk.models.Decision` records."""

    @abstractmethod
    def create(self, d: NewDecision) -> StoreResult[Decision]:
        """Persist a generated decision (Req 13.5)."""
        raise NotImplementedError

    @abstractmethod
    def list_open(self) -> StoreResult[list[Decision]]:
        """Return open decisions ordered most-recently-generated first (Req 14.1)."""
        raise NotImplementedError

    @abstractmethod
    def list_by_status(self, status: DecisionStatus) -> StoreResult[list[Decision]]:
        """Return decisions in ``status``, newest-first (same ordering as ``list_open``).

        Needed by the impact-metrics strip: the waitlist-recovered count is the
        number of *approved* ``gap_fill`` decisions in the period (Req 15.3), and
        approving a decision moves it out of the open feed — so it cannot be read
        through :meth:`list_open`.

        ``list_by_status(DecisionStatus.OPEN)`` is equivalent to
        :meth:`list_open`, which is retained as the named accessor the Decisions
        feed uses (Req 14.1).
        """
        raise NotImplementedError

    @abstractmethod
    def find_open_by_finding_key(self, key: str) -> StoreResult[Decision | None]:
        """Return the open decision for ``key``, or ``Ok(None)`` — for dedupe (Req 13.3)."""
        raise NotImplementedError

    @abstractmethod
    def set_status(
        self, id: str, status: DecisionStatus, resolved_at: ISODateTime
    ) -> StoreResult[Decision]:
        """Transition a decision's status (approve/dismiss/action_failed) (Req 14.3, 14.4, 14.6)."""
        raise NotImplementedError


__all__ = ["DecisionStore"]
