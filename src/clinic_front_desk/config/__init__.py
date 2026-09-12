"""Clinic configuration / onboarding domain logic (task 5.1, Req 1.2, 1.4, 1.5, 1.6).

This package validates onboarded clinic configuration and orchestrates its
atomic save through the ``ClinicKnowledgeBaseStore``:

- :mod:`~clinic_front_desk.config.validation` — exhaustive required-field
  (Req 1.5) and field-bound (Req 1.2) checks, returning a structured
  ``ConfigValidationResult`` that lists every violation.
- :mod:`~clinic_front_desk.config.save` — the ``save_clinic_config`` entry point
  that validates, sets the ``configured`` flag, and atomically persists with
  success/failure signalling and no partial update (Req 1.4, 1.6).

All public names are re-exported here so callers can
``from clinic_front_desk.config import save_clinic_config, validate_config``.
"""

from __future__ import annotations

from .save import Clock, ConfigSaveResult, save_clinic_config
from .validation import (
    FIELD_HOURS,
    FIELD_LOCATION,
    FIELD_PROVIDERS,
    FIELD_SERVICES,
    PREP_INSTRUCTIONS_MAX,
    PROVIDER_NAME_MAX,
    PROVIDER_NAME_MIN,
    PROVIDERS_MAX,
    PROVIDERS_MIN,
    REQUIRED_FIELDS,
    SERVICES_MAX,
    SERVICES_MIN,
    ConfigValidationResult,
    ConfigViolation,
    ViolationKind,
    validate_config,
)

__all__ = [
    # save orchestration
    "Clock",
    "ConfigSaveResult",
    "save_clinic_config",
    # validation
    "validate_config",
    "ConfigValidationResult",
    "ConfigViolation",
    "ViolationKind",
    "REQUIRED_FIELDS",
    "FIELD_HOURS",
    "FIELD_LOCATION",
    "FIELD_SERVICES",
    "FIELD_PROVIDERS",
    "SERVICES_MIN",
    "SERVICES_MAX",
    "PREP_INSTRUCTIONS_MAX",
    "PROVIDERS_MIN",
    "PROVIDERS_MAX",
    "PROVIDER_NAME_MIN",
    "PROVIDER_NAME_MAX",
]
