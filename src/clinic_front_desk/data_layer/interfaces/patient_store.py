"""``PatientStore`` — patient record data access (Req 3, 16).

Contract:
    - **Atomicity / non-destruction (Req 16.6).** A failed ``create`` returns an
      ``Err`` and persists no partial patient record (Req 3.8).
    - **Change emission (Req 16.6).** A successful ``create`` emits a
      ``CREATED`` patient :class:`~clinic_front_desk.data_layer.events.ChangeEvent`.
    - **Empty initialization (Req 16.4).** Before any write, ``get`` returns
      ``Ok(None)`` and ``find_by_name_and_phone`` returns ``Ok([])``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from clinic_front_desk.models import Patient, StoreResult

from .inputs import NewPatient


class PatientStore(ABC):
    """Read/write interface for :class:`~clinic_front_desk.models.Patient` records."""

    @abstractmethod
    def create(self, p: NewPatient) -> StoreResult[Patient]:
        """Create a new patient record (Req 3.4). Failure persists nothing (Req 3.8)."""
        raise NotImplementedError

    @abstractmethod
    def find_by_name_and_phone(self, name: str, phone: str) -> StoreResult[list[Patient]]:
        """Return every patient matching ``name`` and callback ``phone`` (Req 3.1).

        Zero matches → ``Ok([])`` (Req 3.3); more than one → the full candidate
        list for disambiguation (Req 3.6).
        """
        raise NotImplementedError

    @abstractmethod
    def get(self, id: str) -> StoreResult[Patient | None]:
        """Return the patient with ``id``, or ``Ok(None)`` if none exists."""
        raise NotImplementedError

    @abstractmethod
    def find_by_code(self, code: str) -> StoreResult[list[Patient]]:
        """Return every patient whose short code matches ``code``.

        A list, not a single record, because five characters can collide: two
        patients may share a first initial, a final letter and three phone digits.
        The caller must disambiguate when more than one comes back, and must never
        assume the first. Handing one patient another's appointments is worse than
        asking them to repeat their name.

        An empty or unmatched code returns ``Ok([])``, never every patient.
        """
        raise NotImplementedError

    @abstractmethod
    def update(self, p: Patient) -> StoreResult[Patient]:
        """Overwrite the stored record with ``id`` equal to ``p.id``.

        A record that does not exist is a ``not_found`` error, never a create —
        this exists to amend a record the caller has already read, and silently
        creating one would hide a mistaken id.

        Emits an ``UPDATED`` patient change event on success only.

        This exists because without it there was no way to record intake details
        onto an existing patient. The registration tool found the record, returned
        it unchanged, and reported success — so the agent told a caller on a live
        call that her blood group, height and weight were on file when nothing had
        been written. A tool that cannot write must not be able to report success.
        """
        raise NotImplementedError


__all__ = ["PatientStore"]
