"""Unit tests for clinic-config save orchestration (task 5.4, Req 1.4, 1.6).

Covers :func:`clinic_front_desk.config.save.save_clinic_config` on two paths the
property tests do not pin down with concrete values:

- **Req 1.4** — a valid configuration is persisted and the result signals
  success (``configured=True``, a fresh ``updated_at``, and the stored value is
  readable through the store).
- **Req 1.6** — a persistence failure is rejected with an error indication, the
  entered values are retained, and no partial update is applied. The failure is
  produced with the fault-injection store wrapper (task 3.3).
"""

from __future__ import annotations

from clinic_front_desk.config import save_clinic_config
from clinic_front_desk.data_layer.faults import (
    FaultController,
    FaultInjectingClinicKnowledgeBaseStore,
    fail_on,
)
from clinic_front_desk.data_layer.memory.clinic_knowledge_base_store import (
    MemoryClinicKnowledgeBaseStore,
)
from clinic_front_desk.models import (
    ClinicKnowledgeBase,
    DayHours,
    Provider,
    ScheduleRule,
    ServiceConfig,
    StoreErrorKind,
    is_ok,
)

_FIXED_CLOCK = "2025-06-01T00:00:00+00:00"


def _clock() -> str:
    return _FIXED_CLOCK


def _valid_kb() -> ClinicKnowledgeBase:
    """A complete, fully in-bounds clinic configuration."""
    return ClinicKnowledgeBase(
        location="123 Main St, Springfield",
        hours={
            1: DayHours(open="09:00", close="17:00"),
            3: DayHours(open="09:00", close="17:00"),
        },
        services=[
            ServiceConfig(
                name="Hearing Test",
                prep_instructions="Arrive 10 minutes early.",
                price=120.00,
            )
        ],
        accepted_insurance=["Aetna"],
        providers=[
            Provider(
                id="prov-1",
                name="Dr. Alice Smith",
                specialty="ENT",
                schedule=[ScheduleRule(day_of_week=1, start="09:00", end="17:00")],
            )
        ],
    )


# ---------------------------------------------------------------------------
# Req 1.4 — successful save is persisted and reported successful.
# ---------------------------------------------------------------------------


def test_valid_save_succeeds_and_indicates_success() -> None:
    store = MemoryClinicKnowledgeBaseStore()

    result = save_clinic_config(store, _valid_kb(), clock=_clock)

    # Success is signalled explicitly (Req 1.4).
    assert result.ok is True
    assert result.failed_validation is False
    assert result.failed_persistence is False
    assert result.store_error is None

    # The saved config is marked configured and stamped with the save time.
    assert result.saved_config is not None
    assert result.saved_config.configured is True
    assert result.saved_config.updated_at == _FIXED_CLOCK
    assert result.retained_config is None

    # The configuration is durably readable through the store.
    stored = store.get()
    assert is_ok(stored)
    assert stored.value is not None
    assert stored.value.configured is True
    assert stored.value.location == "123 Main St, Springfield"


def test_valid_save_does_not_mutate_the_caller_config() -> None:
    store = MemoryClinicKnowledgeBaseStore()
    kb = _valid_kb()

    save_clinic_config(store, kb, clock=_clock)

    # The doctor's in-hand object is untouched; only the persisted copy carries
    # the configured flag / timestamp.
    assert kb.configured is False
    assert kb.updated_at == ""


# ---------------------------------------------------------------------------
# Req 1.6 — persistence failure rejected, values retained, no partial update.
# ---------------------------------------------------------------------------


def test_persistence_failure_rejects_and_retains_values_without_partial_update() -> None:
    controller = FaultController([fail_on("save", detail="disk offline")])
    store = FaultInjectingClinicKnowledgeBaseStore(
        MemoryClinicKnowledgeBaseStore(), controller
    )
    kb = _valid_kb()

    result = save_clinic_config(store, kb, clock=_clock)

    # Rejected because persistence failed (validation had passed).
    assert result.ok is False
    assert result.failed_persistence is True
    assert result.failed_validation is False

    # The failure is surfaced with an error indication (Req 1.6).
    assert result.store_error is not None
    assert result.store_error.kind is StoreErrorKind.STORE_FAILURE
    assert result.store_error.detail == "disk offline"

    # The entered values are retained unchanged so the doctor keeps their work.
    assert result.retained_config is kb
    assert result.saved_config is None

    # No partial update was applied: the underlying store is still empty.
    stored = store.get()
    assert is_ok(stored)
    assert stored.value is None
