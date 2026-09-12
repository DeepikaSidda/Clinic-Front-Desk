"""DynamoDB :class:`PatientStore` (task 4.1, Req 3, 16).

Single-table layout: ``PK=PATIENT#<id>``, ``SK=PROFILE``, with GSI3
(``NAMEPHONE#<name>#<phone>``) for the name+phone lookup. A ``get`` is a direct
key read; the lookup is a GSI3 query. Results are ordered deterministically by
``id`` so repeated reads are stable.
"""

from __future__ import annotations

from typing import Any

from clinic_front_desk.data_layer.events import ChangeEmitter, ChangeEntity, ChangeKind
from clinic_front_desk.data_layer.interfaces import NewPatient, PatientStore
from clinic_front_desk.models import (
    Ok,
    Patient,
    normalize_patient_code,
    patient_lookup_key,
    StoreResult,
    patient_from_item,
    patient_to_item,
)

from clinic_front_desk.models import Err, StoreError, StoreErrorKind

from typing import Any

from ._support import GSI3, Attr, DynamoStoreBase, Key, from_dynamo

_STORE = "DynamoPatientStore"


def _not_found_err(detail: str) -> Err[StoreError]:
    """Build an ``Err`` for a write targeting a patient that does not exist."""
    return Err(StoreError(kind=StoreErrorKind.NOT_FOUND, detail=detail, store=_STORE))


class DynamoPatientStore(PatientStore, DynamoStoreBase):
    """Single-table :class:`PatientStore` honouring the full store contract."""

    def __init__(self, table: Any, emitter: ChangeEmitter | None = None) -> None:
        DynamoStoreBase.__init__(self, table, emitter)

    def create(self, p: NewPatient) -> StoreResult[Patient]:
        item = patient_to_item(p)
        self._put(item)
        self._emit(ChangeEntity.PATIENT, p.id, ChangeKind.CREATED)
        return Ok(patient_from_item(item))

    def find_by_name_and_phone(self, name: str, phone: str) -> StoreResult[list[Patient]]:
        items = self._query(
            Key("GSI3PK").eq(f"NAMEPHONE#{patient_lookup_key(name, phone)}"),
            index_name=GSI3,
        )
        patients = [patient_from_item(i) for i in items]
        patients.sort(key=lambda pt: pt.id)
        return Ok(patients)

    def find_by_code(self, code: str) -> StoreResult[list[Patient]]:
        wanted = normalize_patient_code(code)
        if not wanted:
            return Ok([])
        # A filtered scan, deliberately. The code is a five-character convenience
        # for the caller, not a key the schema is built around, and adding a fifth
        # index to serve it would cost a write on every patient update. Same
        # reasoning as `_find_by_entity_id`: a solo-doctor clinic's patient count
        # makes this inexpensive. If the roster ever reaches tens of thousands this
        # is the first thing to give an index.
        filt = Attr("entity").eq("Patient") & Attr("code").eq(wanted)
        raw: list[Any] = []
        response = self._table.scan(FilterExpression=filt)
        raw.extend(response.get("Items", []))
        while "LastEvaluatedKey" in response:
            response = self._table.scan(
                FilterExpression=filt, ExclusiveStartKey=response["LastEvaluatedKey"]
            )
            raw.extend(response.get("Items", []))
        patients = [patient_from_item(from_dynamo(item)) for item in raw]
        patients.sort(key=lambda p: p.id)
        return Ok(patients)

    def get(self, id: str) -> StoreResult[Patient | None]:
        item = self._get(f"PATIENT#{id}", "PROFILE")
        return Ok(patient_from_item(item) if item is not None else None)

    def update(self, p: Patient) -> StoreResult[Patient]:
        if self._get(f"PATIENT#{p.id}", "PROFILE") is None:
            return _not_found_err(f"patient {p.id!r} not found")
        item = patient_to_item(p)
        self._put(item)
        self._emit(ChangeEntity.PATIENT, p.id, ChangeKind.UPDATED)
        stored: Patient = patient_from_item(item)
        return Ok(stored)


__all__ = ["DynamoPatientStore"]
