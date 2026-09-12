"""Unit tests for the fault-injection store wrapper (task 3.3).

These tests are self-contained: they wrap a tiny local stub store (not the
in-memory fakes, which are a sibling task) so they verify only the wrapper's
own behaviour:

1. With no faults configured, every call transparently delegates.
2. A configured fault makes the chosen method return ``Err(store_failure)``
   *without* delegating — the atomicity guarantee (Req 16.6).
3. ``after_calls`` lets N matching calls succeed before failing.
4. ``record_id`` scopes a fault to calls targeting a specific record.
5. :func:`wrap` selects the correct wrapper per interface and rejects
   non-store objects.

_Requirements: 16.6_
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from clinic_front_desk.data_layer.faults import (
    Fault,
    FaultInjectingPatientStore,
    fail_on,
    wrap,
)
from clinic_front_desk.data_layer.interfaces import PatientStore
from clinic_front_desk.models import (
    Err,
    Ok,
    StoreError,
    StoreErrorKind,
    StoreResult,
    is_err,
    is_ok,
)


@dataclass
class _StubPatientStore(PatientStore):
    """Minimal in-test stub that records every delegated call."""

    calls: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)

    def create(self, p: object) -> StoreResult[object]:  # type: ignore[override]
        self.calls.append(("create", (p,)))
        return Ok(p)

    def find_by_name_and_phone(self, name: str, phone: str) -> StoreResult[list[object]]:  # type: ignore[override]
        self.calls.append(("find_by_name_and_phone", (name, phone)))
        return Ok([])

    def get(self, id: str) -> StoreResult[object | None]:  # type: ignore[override]
        self.calls.append(("get", (id,)))
        return Ok(None)

    def update(self, p: object) -> StoreResult[object]:  # type: ignore[override]
        self.calls.append(("update", (p,)))
        return Ok(p)

    def find_by_code(self, code: str) -> StoreResult[list[object]]:  # type: ignore[override]
        self.calls.append(("find_by_code", (code,)))
        return Ok([])


def test_no_faults_delegates_transparently() -> None:
    stub = _StubPatientStore()
    store = wrap(stub)

    result = store.get("patient-1")

    assert is_ok(result)
    assert stub.calls == [("get", ("patient-1",))]


def test_forced_failure_returns_store_error_without_delegating() -> None:
    stub = _StubPatientStore()
    store = wrap(stub, fail_on("create"))

    result = store.create({"id": "p1"})

    assert is_err(result)
    assert isinstance(result, Err)
    error = result.error
    assert isinstance(error, StoreError)
    assert error.kind is StoreErrorKind.STORE_FAILURE
    # Atomicity: the wrapped store was never touched (Req 16.6).
    assert stub.calls == []


def test_fault_only_affects_the_named_method() -> None:
    stub = _StubPatientStore()
    store = wrap(stub, fail_on("create"))

    # A different method still delegates and succeeds.
    assert is_ok(store.get("p1"))
    assert stub.calls == [("get", ("p1",))]


def test_after_calls_lets_n_calls_through_then_fails() -> None:
    stub = _StubPatientStore()
    store = wrap(stub, fail_on("get", after_calls=2))

    assert is_ok(store.get("a"))  # 1st: pass
    assert is_ok(store.get("b"))  # 2nd: pass
    assert is_err(store.get("c"))  # 3rd: fail
    assert is_err(store.get("d"))  # subsequent: still fails
    # Only the two passing calls reached the wrapped store.
    assert stub.calls == [("get", ("a",)), ("get", ("b",))]


def test_record_id_scopes_the_fault() -> None:
    stub = _StubPatientStore()
    store = wrap(stub, Fault(method="get", record_id="target"))

    # Non-matching record delegates.
    assert is_ok(store.get("other"))
    # Matching record fails without delegating.
    assert is_err(store.get("target"))
    assert stub.calls == [("get", ("other",))]


def test_wrap_selects_the_correct_wrapper() -> None:
    store = wrap(_StubPatientStore())
    assert isinstance(store, FaultInjectingPatientStore)
    assert isinstance(store, PatientStore)


def test_wrap_rejects_non_store_objects() -> None:
    with pytest.raises(TypeError):
        wrap(object())
