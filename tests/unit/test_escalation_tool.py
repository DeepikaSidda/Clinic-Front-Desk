"""Unit tests for the ``flag_for_human`` Strands tool (task 6.13).

Covers ``flag_for_human``:

- Records an escalation carrying the reason, call-session context, and patient
  identity when known (Req 9.4).
- Records an escalation with no patient identity when the patient is unknown
  (Req 9.4 — "when known").
- Accepts each of the four escalation-reason categories (Req 9.4).
- Generates an id and timestamp when not supplied.
- Surfaces a store-failure result path as a ToolError with no partial
  escalation retained (Req 9.9).

These are focused example/edge tests; the escalation property test lives in
task 7.13.
"""

from __future__ import annotations

import pytest

from clinic_front_desk.data_layer.faults import fail_on, wrap
from clinic_front_desk.data_layer.memory import MemoryEscalationStore
from clinic_front_desk.models import (
    EscalationReason,
    PatientRef,
    StoreFailure,
    is_err,
    is_ok,
)
from clinic_front_desk.tools.escalation import flag_for_human


def test_flag_for_human_records_reason_context_and_patient_identity() -> None:
    """Req 9.4: a successful escalation persists the reason, call-session
    context, and the patient identity when known."""
    store = MemoryEscalationStore()

    result = flag_for_human(
        store,
        reason=EscalationReason.CLINICAL_CONTENT,
        call_session_id="cs1",
        context="patient asked for a diagnosis",
        patient_ref=PatientRef(patient_id="p1", name="Jane Doe", callback_phone="555-0100"),
        escalation_id="e1",
        created_at="2025-06-01T09:00:00Z",
    )

    assert is_ok(result)
    esc = result.value
    assert esc.id == "e1"
    assert esc.reason == EscalationReason.CLINICAL_CONTENT
    assert esc.call_session_id == "cs1"
    assert esc.context == "patient asked for a diagnosis"
    assert esc.patient_ref is not None
    assert esc.patient_ref.patient_id == "p1"
    assert esc.patient_ref.name == "Jane Doe"
    assert esc.created_at == "2025-06-01T09:00:00Z"
    # Persisted and retrievable through the store (surfaces in the activity log).
    recent = store.list_recent(10).unwrap()
    assert [e.id for e in recent] == ["e1"]


def test_flag_for_human_records_escalation_without_known_patient() -> None:
    """Req 9.4: patient identity is recorded only "when known"; an escalation
    with no identified patient is still recorded."""
    store = MemoryEscalationStore()

    result = flag_for_human(
        store,
        reason=EscalationReason.OUTSIDE_ADMIN_RULES,
        call_session_id="cs2",
        context="request outside administrative rules",
        escalation_id="e2",
        created_at="2025-06-01T10:00:00Z",
    )

    assert is_ok(result)
    assert result.value.patient_ref is None
    assert store.list_recent(10).unwrap()[0].id == "e2"


@pytest.mark.parametrize(
    "reason",
    [
        EscalationReason.CLINICAL_CONTENT,
        EscalationReason.OUTSIDE_ADMIN_RULES,
        EscalationReason.PATIENT_DISTRESS,
        EscalationReason.PATIENT_REQUEST,
    ],
)
def test_flag_for_human_accepts_each_reason_category(reason: EscalationReason) -> None:
    """Req 9.4: each of the four escalation-reason categories is recorded."""
    store = MemoryEscalationStore()

    result = flag_for_human(
        store,
        reason=reason,
        call_session_id="cs3",
        context="ctx",
        escalation_id="e3",
    )

    assert is_ok(result)
    assert result.value.reason == reason


def test_flag_for_human_generates_id_and_timestamp_when_absent() -> None:
    store = MemoryEscalationStore()

    result = flag_for_human(
        store,
        reason=EscalationReason.PATIENT_REQUEST,
        call_session_id="cs4",
        context="please get me a human",
        clock=lambda: "2025-06-02T11:00:00Z",
        id_gen=lambda: "generated-id",
    )

    assert is_ok(result)
    assert result.value.id == "generated-id"
    assert result.value.created_at == "2025-06-02T11:00:00Z"


def test_flag_for_human_store_failure_leaves_no_partial_escalation() -> None:
    """Req 9.9: when the create write fails, the failure surfaces as a
    StoreFailure ToolError and no partial escalation is retained."""
    base = MemoryEscalationStore()
    store = wrap(base, fail_on("create"))

    result = flag_for_human(
        store,
        reason=EscalationReason.PATIENT_DISTRESS,
        call_session_id="cs5",
        context="patient is upset",
        escalation_id="e5",
    )

    assert is_err(result)
    assert isinstance(result.error, StoreFailure)
    assert result.error.store == "EscalationStore"
    # Nothing persisted.
    assert base.list_recent(10).unwrap() == []
