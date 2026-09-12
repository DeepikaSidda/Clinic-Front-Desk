"""Clinic-configuration save orchestration (task 5.1, Req 1.4, 1.5, 1.6).

:func:`save_clinic_config` is the single entry point the onboarding wizard
calls. It ties validation (:mod:`clinic_front_desk.config.validation`) to atomic
persistence through the :class:`~clinic_front_desk.data_layer.interfaces.ClinicKnowledgeBaseStore`
and returns a structured :class:`ConfigSaveResult` describing exactly what
happened, so the wizard can either confirm success (Req 1.4) or re-render the
form with per-field errors and the values the doctor already entered
(Req 1.5, 1.6).

Ordering and atomicity guarantees:

1. **Validate first.** If any required field is missing (Req 1.5) or any field
   is out of bounds (Req 1.2), the save is rejected *before* the store is
   touched. Nothing is persisted and the entered configuration is returned
   unchanged in :attr:`ConfigSaveResult.retained_config`.
2. **Mark configured, then persist.** On a clean validation the
   :attr:`~clinic_front_desk.models.ClinicKnowledgeBase.configured` flag is set
   (all required fields are present) and ``updated_at`` is stamped, on a *copy*
   of the input — the caller's object is never mutated.
3. **Atomic persistence (Req 1.6).** The store's ``save`` is all-or-nothing. If
   it returns a failure, no partial update is applied; the save is reported as
   failed and the entered values are retained for the wizard to re-render.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from clinic_front_desk.data_layer.interfaces import ClinicKnowledgeBaseStore
from clinic_front_desk.models import ClinicKnowledgeBase, StoreError, is_err

from .validation import ConfigValidationResult, validate_config

#: A clock returning the current time as an ISO-8601 UTC string. Injectable so
#: tests can pin ``updated_at`` deterministically.
Clock = Callable[[], str]


def _default_clock() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class ConfigSaveResult:
    """The outcome of a :func:`save_clinic_config` call.

    Exactly one of the success / failure shapes is populated:

    - **Success** (:attr:`ok` is ``True``): :attr:`saved_config` holds the
      persisted configuration (with ``configured=True`` and a fresh
      ``updated_at``). This is the "indicate save success" signal of Req 1.4.
    - **Validation failure** (:attr:`failed_validation`): the configuration
      broke a required-field (Req 1.5) or bound (Req 1.2) rule; the store was
      never touched and :attr:`retained_config` holds the entered values.
    - **Persistence failure** (:attr:`failed_persistence`): validation passed
      but the store rejected the write (Req 1.6); no partial update was applied,
      :attr:`store_error` explains why, and :attr:`retained_config` holds the
      entered values.
    """

    ok: bool
    validation: ConfigValidationResult
    saved_config: ClinicKnowledgeBase | None = None
    retained_config: ClinicKnowledgeBase | None = None
    store_error: StoreError | None = None

    @property
    def failed_validation(self) -> bool:
        """True when the save was rejected because validation failed (Req 1.5)."""
        return not self.ok and not self.validation.ok

    @property
    def failed_persistence(self) -> bool:
        """True when validation passed but persistence failed (Req 1.6)."""
        return not self.ok and self.validation.ok and self.store_error is not None


def save_clinic_config(
    store: ClinicKnowledgeBaseStore,
    kb: ClinicKnowledgeBase,
    *,
    clock: Clock = _default_clock,
) -> ConfigSaveResult:
    """Validate ``kb`` and, if valid, atomically persist it through ``store``.

    Args:
        store: The knowledge-base store to persist through.
        kb: The clinic configuration the doctor is trying to save.
        clock: Injectable clock for the ``updated_at`` stamp (defaults to now).

    Returns:
        A :class:`ConfigSaveResult`. On success ``ok`` is ``True`` and
        ``saved_config`` carries the persisted, ``configured=True``
        configuration. On any failure ``ok`` is ``False``, nothing (or nothing
        further) is persisted, and ``retained_config`` carries the entered
        values so the wizard can re-render them (Req 1.5, 1.6).
    """
    validation = validate_config(kb)
    if not validation.ok:
        # Reject before touching the store; retain the entered values (Req 1.5).
        return ConfigSaveResult(
            ok=False,
            validation=validation,
            retained_config=kb,
        )

    # All required fields are present, so the configuration is "configured"
    # (Req 1.7). Stamp on a copy — never mutate the caller's object.
    to_persist = replace(kb, configured=True, updated_at=clock())

    store_result = store.save(to_persist)
    if is_err(store_result):
        # Atomic store: no partial update was applied. Retain entered values so
        # the doctor does not lose their work (Req 1.6).
        return ConfigSaveResult(
            ok=False,
            validation=validation,
            retained_config=kb,
            store_error=store_result.error,
        )

    # Persisted successfully; signal success to the doctor (Req 1.4).
    return ConfigSaveResult(
        ok=True,
        validation=validation,
        saved_config=store_result.value,
    )


__all__ = [
    "Clock",
    "ConfigSaveResult",
    "save_clinic_config",
]
