"""``WaitlistStore`` — waitlist entry data access (Req 7, 8, 16).

Contract:
    - **Ordering (Req 7.3).** ``list_by_service_ordered`` returns entries for a
      service in ascending ``added_at`` order, using the monotonic ``seq`` field
      to break ties so entries recorded at the same instant keep their original
      insertion order.
    - **Atomicity / non-destruction (Req 16.6).** A failed ``add`` or ``remove``
      returns an ``Err`` and leaves the waitlist unchanged (Req 7.4, 8.5).
    - **Change emission (Req 16.6).** A successful ``add`` emits a ``CREATED``
      and a successful ``remove`` a ``REMOVED`` waitlist-entry
      :class:`~clinic_front_desk.data_layer.events.ChangeEvent`.
    - **Empty initialization (Req 16.4).** Before any write, ``find_active``
      returns ``Ok(None)`` and ``list_by_service_ordered`` returns ``Ok([])``.

Active-duplicate suppression (Req 7.5) is a tool-layer concern: the tool calls
:meth:`find_active` first and declines to ``add`` when a match exists. The store
does not itself reject duplicates.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from clinic_front_desk.models import StoreResult, WaitlistEntry

from .inputs import NewWaitlistEntry


class WaitlistStore(ABC):
    """Read/write interface for :class:`~clinic_front_desk.models.WaitlistEntry` records."""

    @abstractmethod
    def add(self, e: NewWaitlistEntry) -> StoreResult[WaitlistEntry]:
        """Append a waitlist entry, assigning ordering fields as needed (Req 7.1)."""
        raise NotImplementedError

    @abstractmethod
    def find_active(
        self, patient_id: str, service: str, slot_type: str
    ) -> StoreResult[WaitlistEntry | None]:
        """Return the patient's active entry for a service/slot type, else ``Ok(None)`` (Req 7.5)."""
        raise NotImplementedError

    @abstractmethod
    def list_by_service_ordered(self, service: str) -> StoreResult[list[WaitlistEntry]]:
        """Return active entries for a service, ascending by ``added_at`` then ``seq`` (Req 7.3)."""
        raise NotImplementedError

    @abstractmethod
    def remove(self, id: str) -> StoreResult[None]:
        """Remove a waitlist entry (Req 8.4). Failure leaves the entry unchanged (Req 8.5)."""
        raise NotImplementedError


__all__ = ["WaitlistStore"]
