"""``ClinicKnowledgeBaseStore`` — onboarded clinic config data access (Req 1, 16).

The knowledge base is a singleton (one clinic configuration).

Contract:
    - **Atomic save, no partial update (Req 1.6, 16.6).** ``save`` either
      persists the entire configuration or, on failure, returns an ``Err`` and
      leaves the previously stored configuration completely unchanged — the
      store never applies a partial update.
    - **Provider-id enforcement (Req 16.7).** Because the configuration carries
      the provider roster and their schedules, ``save`` rejects a configuration
      whose providers or their schedule-owning records lack a provider id, with
      a ``StoreError`` of kind ``validation``.
    - **Change emission (Req 16.6, 1.8).** A successful ``save`` emits an
      ``UPDATED`` clinic-knowledge-base event (drives the ≤ 5 s live
      config-update propagation).
    - **Empty initialization (Req 16.4).** Before the first save, ``get``
      returns ``Ok(None)``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from clinic_front_desk.models import ClinicKnowledgeBase, StoreResult


class ClinicKnowledgeBaseStore(ABC):
    """Read/write interface for the singleton
    :class:`~clinic_front_desk.models.ClinicKnowledgeBase`."""

    @abstractmethod
    def get(self) -> StoreResult[ClinicKnowledgeBase | None]:
        """Return the stored configuration, or ``Ok(None)`` before onboarding (Req 16.4)."""
        raise NotImplementedError

    @abstractmethod
    def save(self, kb: ClinicKnowledgeBase) -> StoreResult[ClinicKnowledgeBase]:
        """Atomically persist the full configuration (Req 1.4, 1.6). No partial update."""
        raise NotImplementedError


__all__ = ["ClinicKnowledgeBaseStore"]
