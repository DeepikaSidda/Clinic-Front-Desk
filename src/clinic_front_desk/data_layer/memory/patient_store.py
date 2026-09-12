"""In-memory :class:`PatientStore` fake (task 3.2, Req 3, 16)."""

from __future__ import annotations

from clinic_front_desk.data_layer.events import ChangeEmitter, ChangeEntity, ChangeKind
from clinic_front_desk.data_layer.interfaces import NewPatient, PatientStore
from clinic_front_desk.models import (
    Ok,
    Patient,
    StoreResult,
    normalize_patient_code,
    patient_lookup_key,
)

from ._support import MemoryStoreBase, not_found_err

_STORE = "MemoryPatientStore"


class MemoryPatientStore(PatientStore, MemoryStoreBase):
    """A dict-backed :class:`PatientStore` honouring the full store contract."""

    def __init__(self, emitter: ChangeEmitter | None = None) -> None:
        MemoryStoreBase.__init__(self, emitter)
        self._patients: dict[str, Patient] = {}

    def create(self, p: NewPatient) -> StoreResult[Patient]:
        # Store a copy so later caller mutation cannot reach store state, then
        # emit CREATED only after the write commits (Req 16.6).
        self._patients[p.id] = self._copy(p)
        self._emit(ChangeEntity.PATIENT, p.id, ChangeKind.CREATED)
        return Ok(self._copy(self._patients[p.id]))

    def find_by_name_and_phone(self, name: str, phone: str) -> StoreResult[list[Patient]]:
        # Compared on the normalised key, matching the DynamoDB GSI3 key exactly,
        # so a spoken name in any casing finds the record the doctor can see.
        wanted = patient_lookup_key(name, phone)
        matches = [
            self._copy(pt)
            for pt in self._patients.values()
            if patient_lookup_key(pt.name, pt.callback_phone) == wanted
        ]
        matches.sort(key=lambda p: p.id)
        return Ok(matches)

    def find_by_code(self, code: str) -> StoreResult[list[Patient]]:
        wanted = normalize_patient_code(code)
        if not wanted:
            return Ok([])
        matches = [
            self._copy(pt)
            for pt in self._patients.values()
            if normalize_patient_code(pt.code) == wanted
        ]
        matches.sort(key=lambda p: p.id)
        return Ok(matches)

    def get(self, id: str) -> StoreResult[Patient | None]:
        found = self._patients.get(id)
        return Ok(self._copy(found) if found is not None else None)

    def update(self, p: Patient) -> StoreResult[Patient]:
        if p.id not in self._patients:
            return not_found_err(_STORE, f"patient {p.id!r} not found")
        self._patients[p.id] = self._copy(p)
        self._emit(ChangeEntity.PATIENT, p.id, ChangeKind.UPDATED)
        stored: Patient = self._copy(self._patients[p.id])
        return Ok(stored)


__all__ = ["MemoryPatientStore"]
