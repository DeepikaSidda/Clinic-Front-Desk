"""Unit tests for the ``lookup_patient`` tool and patient creation (task 6.6).

Exercises the patient lookup/creation half of Requirement 3 against the
in-memory :class:`MemoryPatientStore`, with the fault-injection wrapper used to
drive the store-failure paths (Req 3.7, 3.8):

- lookup returns exactly the records matching name + callback phone (Req 3.1)
- lookup returns an empty list when nothing matches — the "create new" signal
  (Req 3.3)
- extra identifiers narrow a multi-match set toward one (Req 3.6)
- a retrieval failure surfaces as a ``store_failure`` ToolError (Req 3.7)
- create_patient persists a new record and it round-trips through lookup
  (Req 3.4)
- a persistence failure surfaces as a ``store_failure`` ToolError and retains
  no partial record (Req 3.8)

_Requirements: 3.1, 3.3, 3.4, 3.6, 3.7, 3.8_
"""

from __future__ import annotations

import pytest

from clinic_front_desk.data_layer.faults import fail_on, wrap
from clinic_front_desk.data_layer.memory import MemoryPatientStore
from clinic_front_desk.models import Err, Ok, Patient, StoreFailure, is_err, is_ok
from clinic_front_desk.tools.patients import create_patient, lookup_patient


def _seed(store: MemoryPatientStore, patient: Patient) -> Patient:
    """Persist a patient directly through the store and return the stored copy."""
    result = store.create(patient)
    assert is_ok(result)
    return result.value


def _fixed_clock() -> str:
    return "2025-06-01T09:00:00+00:00"


# ---------------------------------------------------------------------------
# lookup_patient (Req 3.1, 3.3, 3.6, 3.7)
# ---------------------------------------------------------------------------


def test_lookup_returns_matching_record() -> None:
    """Req 3.1: lookup returns records matching name and callback phone."""
    store = MemoryPatientStore()
    _seed(store, Patient(id="p1", name="Alice Smith", callback_phone="555-0100"))
    _seed(store, Patient(id="p2", name="Bob Jones", callback_phone="555-0200"))

    result = lookup_patient(store, "Alice Smith", "555-0100")

    assert is_ok(result)
    assert isinstance(result, Ok)
    assert [p.id for p in result.value] == ["p1"]


def test_lookup_requires_both_name_and_phone_to_match() -> None:
    """Req 3.1: a matching name with a different phone is not a match."""
    store = MemoryPatientStore()
    _seed(store, Patient(id="p1", name="Alice Smith", callback_phone="555-0100"))

    result = lookup_patient(store, "Alice Smith", "555-9999")

    assert is_ok(result)
    assert isinstance(result, Ok)
    assert result.value == []


def test_lookup_no_match_returns_empty_list() -> None:
    """Req 3.3: no matching record yields an empty list (the create-new signal)."""
    store = MemoryPatientStore()

    result = lookup_patient(store, "Nobody Here", "555-0000")

    assert is_ok(result)
    assert isinstance(result, Ok)
    assert result.value == []


def test_extra_identifiers_disambiguate_multiple_matches() -> None:
    """Req 3.6: extra identifiers narrow a multi-match set toward exactly one."""
    store = MemoryPatientStore()
    _seed(
        store,
        Patient(
            id="p1",
            name="Alex Doe",
            callback_phone="555-0100",
            extra_identifiers={"dob": "1990-01-01"},
        ),
    )
    _seed(
        store,
        Patient(
            id="p2",
            name="Alex Doe",
            callback_phone="555-0100",
            extra_identifiers={"dob": "1985-05-05"},
        ),
    )

    # Without extra identifiers both records match (ambiguous).
    ambiguous = lookup_patient(store, "Alex Doe", "555-0100")
    assert is_ok(ambiguous)
    assert isinstance(ambiguous, Ok)
    assert {p.id for p in ambiguous.value} == {"p1", "p2"}

    # Supplying a distinguishing identifier converges to exactly one (Req 3.6).
    narrowed = lookup_patient(
        store, "Alex Doe", "555-0100", extra_identifiers={"dob": "1990-01-01"}
    )
    assert is_ok(narrowed)
    assert isinstance(narrowed, Ok)
    assert [p.id for p in narrowed.value] == ["p1"]


def test_extra_identifiers_can_narrow_to_none() -> None:
    """Req 3.6: identifiers matching no candidate converge to an empty set."""
    store = MemoryPatientStore()
    _seed(
        store,
        Patient(
            id="p1",
            name="Alex Doe",
            callback_phone="555-0100",
            extra_identifiers={"dob": "1990-01-01"},
        ),
    )

    result = lookup_patient(
        store, "Alex Doe", "555-0100", extra_identifiers={"dob": "2000-12-31"}
    )

    assert is_ok(result)
    assert isinstance(result, Ok)
    assert result.value == []


def test_lookup_retrieval_failure_maps_to_store_failure() -> None:
    """Req 3.7: a store retrieval failure surfaces as a store_failure ToolError."""
    store = wrap(MemoryPatientStore(), fail_on("find_by_name_and_phone"))

    result = lookup_patient(store, "Alice Smith", "555-0100")

    assert is_err(result)
    assert isinstance(result, Err)
    assert isinstance(result.error, StoreFailure)
    assert result.error.kind == "store_failure"


# ---------------------------------------------------------------------------
# create_patient (Req 3.4, 3.8)
# ---------------------------------------------------------------------------


def test_create_patient_persists_and_round_trips_through_lookup() -> None:
    """Req 3.4: a created patient is subsequently returned by lookup."""
    store = MemoryPatientStore()

    created = create_patient(
        store,
        "Carla New",
        "555-0300",
        id_factory=lambda: "pnew",
        clock=_fixed_clock,
    )

    assert is_ok(created)
    assert isinstance(created, Ok)
    assert created.value.id == "pnew"
    assert created.value.name == "Carla New"
    assert created.value.callback_phone == "555-0300"
    assert created.value.created_at == _fixed_clock()

    # Round-trip: the new record is now found by lookup (Req 3.1/3.4).
    found = lookup_patient(store, "Carla New", "555-0300")
    assert is_ok(found)
    assert isinstance(found, Ok)
    assert [p.id for p in found.value] == ["pnew"]


def test_create_patient_retains_extra_identifiers() -> None:
    """Req 3.6: supplied extra identifiers are retained on the new record."""
    store = MemoryPatientStore()

    created = create_patient(
        store,
        "Dana Ray",
        "555-0400",
        extra_identifiers={"dob": "1970-07-07"},
        id_factory=lambda: "pdana",
        clock=_fixed_clock,
    )

    assert is_ok(created)
    assert isinstance(created, Ok)
    assert created.value.extra_identifiers == {"dob": "1970-07-07"}


def test_create_patient_persistence_failure_maps_to_store_failure() -> None:
    """Req 3.8: a persistence failure surfaces as a store_failure and saves nothing."""
    inner = MemoryPatientStore()
    store = wrap(inner, fail_on("create"))

    result = create_patient(
        store,
        "Eve Fail",
        "555-0500",
        id_factory=lambda: "pfail",
        clock=_fixed_clock,
    )

    assert is_err(result)
    assert isinstance(result, Err)
    assert isinstance(result.error, StoreFailure)
    assert result.error.kind == "store_failure"

    # No partial record was persisted (Req 3.8 / 16.6).
    lookup = lookup_patient(inner, "Eve Fail", "555-0500")
    assert is_ok(lookup)
    assert isinstance(lookup, Ok)
    assert lookup.value == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
