"""Clinic-configuration validation (task 5.1, Req 1.2, 1.5).

This module turns a :class:`~clinic_front_desk.models.ClinicKnowledgeBase` into a
structured :class:`ConfigValidationResult` that lists **every** violation found,
so the onboarding wizard can surface all problems at once (Req 1.5) and the
property tests (tasks 5.2/5.3) can assert on the complete, deterministic set.

Two independent families of checks are performed:

- **Required-field checks (Req 1.5).** The required fields are clinic hours,
  clinic location, at least one offered service, and at least one provider. A
  missing required field is reported as a :attr:`ViolationKind.MISSING_REQUIRED`
  violation whose ``field`` is one of the top-level names in
  :data:`REQUIRED_FIELDS`. The set of names reported is *exactly* the set of
  required fields that are absent — no more, no less.
- **Field-bound checks (Req 1.2).** Offered-service count (1–100), prep
  instructions length (≤ 2,000 chars per service), price (0.01–999,999.99),
  provider count (1–50), and provider-name length (1–100). A value outside its
  bound is reported as an :attr:`ViolationKind.OUT_OF_BOUNDS` violation.

The lower bound of "at least one" for services and providers is owned by the
required-field checks (a count of zero is reported as ``MISSING_REQUIRED``, not
as an out-of-bounds violation), so the two families never double-report the same
absence. The bound checks therefore only flag *too many* services/providers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from clinic_front_desk.models import MONEY_MAX, MONEY_MIN, ClinicKnowledgeBase

# ---------------------------------------------------------------------------
# Bound constants (Req 1.2, 1.3). Exported so tests and the wizard share them.
# ---------------------------------------------------------------------------

SERVICES_MIN = 1
SERVICES_MAX = 100
PREP_INSTRUCTIONS_MAX = 2000
PROVIDERS_MIN = 1
PROVIDERS_MAX = 50
PROVIDER_NAME_MIN = 1
PROVIDER_NAME_MAX = 100

# ---------------------------------------------------------------------------
# Required-field identifiers (Req 1.5). These are the stable, top-level names
# reported for a missing required field.
# ---------------------------------------------------------------------------

FIELD_HOURS = "hours"
FIELD_LOCATION = "location"
FIELD_SERVICES = "services"
FIELD_PROVIDERS = "providers"

#: The four required fields, in a stable order (Req 1.5).
REQUIRED_FIELDS: tuple[str, ...] = (
    FIELD_HOURS,
    FIELD_LOCATION,
    FIELD_SERVICES,
    FIELD_PROVIDERS,
)


class ViolationKind(StrEnum):
    """Why a configuration value was rejected."""

    MISSING_REQUIRED = "missing_required"
    OUT_OF_BOUNDS = "out_of_bounds"


@dataclass(frozen=True)
class ConfigViolation:
    """A single validation problem.

    Attributes:
        field: The offending field. For required-field violations this is one
            of :data:`REQUIRED_FIELDS`; for bound violations on a collection
            element it is an indexed path such as ``"services[2].price"``.
        kind: Whether the field is a missing required field or an out-of-bounds
            value.
        detail: A human-readable explanation for the onboarding wizard.
    """

    field: str
    kind: ViolationKind
    detail: str


@dataclass(frozen=True)
class ConfigValidationResult:
    """The complete outcome of validating a clinic configuration.

    ``violations`` lists every problem found (Req 1.5 completeness). The result
    is :attr:`ok` only when it is empty.
    """

    violations: tuple[ConfigViolation, ...] = ()

    @property
    def ok(self) -> bool:
        """True when the configuration has no violations and may be saved."""
        return len(self.violations) == 0

    @property
    def missing_required_fields(self) -> tuple[str, ...]:
        """The required fields that are absent, in :data:`REQUIRED_FIELDS` order (Req 1.5)."""
        missing = {
            v.field for v in self.violations if v.kind is ViolationKind.MISSING_REQUIRED
        }
        return tuple(f for f in REQUIRED_FIELDS if f in missing)

    @property
    def out_of_bounds_fields(self) -> tuple[str, ...]:
        """The field paths whose values fall outside their bounds (Req 1.2)."""
        return tuple(
            v.field for v in self.violations if v.kind is ViolationKind.OUT_OF_BOUNDS
        )


def _hours_present(kb: ClinicKnowledgeBase) -> bool:
    """True when at least one weekday has concrete opening/closing hours.

    An empty ``hours`` map, or one whose every entry is ``None`` (all days
    closed), counts as *no hours configured* for the required-field check.
    """
    return any(day is not None for day in kb.hours.values())


def _location_present(kb: ClinicKnowledgeBase) -> bool:
    """True when a non-blank clinic location is configured."""
    return bool(kb.location and kb.location.strip())


def validate_config(kb: ClinicKnowledgeBase) -> ConfigValidationResult:
    """Validate ``kb`` and return every required-field and bound violation.

    The check is exhaustive and order-stable: required-field violations are
    listed first (in :data:`REQUIRED_FIELDS` order), then bound violations in
    field order. The returned result is :attr:`~ConfigValidationResult.ok` iff
    ``kb`` satisfies every required-field (Req 1.5) and field-bound (Req 1.2)
    rule.
    """
    violations: list[ConfigViolation] = []

    # --- Required-field checks (Req 1.5) -----------------------------------
    if not _hours_present(kb):
        violations.append(
            ConfigViolation(
                field=FIELD_HOURS,
                kind=ViolationKind.MISSING_REQUIRED,
                detail="clinic hours are required for at least one day",
            )
        )
    if not _location_present(kb):
        violations.append(
            ConfigViolation(
                field=FIELD_LOCATION,
                kind=ViolationKind.MISSING_REQUIRED,
                detail="clinic location is required",
            )
        )
    if len(kb.services) < SERVICES_MIN:
        violations.append(
            ConfigViolation(
                field=FIELD_SERVICES,
                kind=ViolationKind.MISSING_REQUIRED,
                detail="at least one offered service is required",
            )
        )
    if len(kb.providers) < PROVIDERS_MIN:
        violations.append(
            ConfigViolation(
                field=FIELD_PROVIDERS,
                kind=ViolationKind.MISSING_REQUIRED,
                detail="at least one provider is required",
            )
        )

    # --- Field-bound checks (Req 1.2) --------------------------------------
    # Services: only the upper bound here; a count of zero is a missing
    # required field, reported above.
    if len(kb.services) > SERVICES_MAX:
        violations.append(
            ConfigViolation(
                field=FIELD_SERVICES,
                kind=ViolationKind.OUT_OF_BOUNDS,
                detail=f"at most {SERVICES_MAX} services are allowed "
                f"(got {len(kb.services)})",
            )
        )

    for i, service in enumerate(kb.services):
        if (
            service.prep_instructions is not None
            and len(service.prep_instructions) > PREP_INSTRUCTIONS_MAX
        ):
            violations.append(
                ConfigViolation(
                    field=f"services[{i}].prep_instructions",
                    kind=ViolationKind.OUT_OF_BOUNDS,
                    detail=f"preparation instructions must be at most "
                    f"{PREP_INSTRUCTIONS_MAX} characters",
                )
            )
        if service.price is not None and not (MONEY_MIN <= service.price <= MONEY_MAX):
            violations.append(
                ConfigViolation(
                    field=f"services[{i}].price",
                    kind=ViolationKind.OUT_OF_BOUNDS,
                    detail=f"price must be between {MONEY_MIN} and {MONEY_MAX}",
                )
            )

    # Providers: only the upper bound here; a count of zero is a missing
    # required field, reported above.
    if len(kb.providers) > PROVIDERS_MAX:
        violations.append(
            ConfigViolation(
                field=FIELD_PROVIDERS,
                kind=ViolationKind.OUT_OF_BOUNDS,
                detail=f"at most {PROVIDERS_MAX} providers are allowed "
                f"(got {len(kb.providers)})",
            )
        )

    for i, provider in enumerate(kb.providers):
        if not (PROVIDER_NAME_MIN <= len(provider.name) <= PROVIDER_NAME_MAX):
            violations.append(
                ConfigViolation(
                    field=f"providers[{i}].name",
                    kind=ViolationKind.OUT_OF_BOUNDS,
                    detail=f"provider name must be between {PROVIDER_NAME_MIN} and "
                    f"{PROVIDER_NAME_MAX} characters",
                )
            )

    return ConfigValidationResult(violations=tuple(violations))


__all__ = [
    "SERVICES_MIN",
    "SERVICES_MAX",
    "PREP_INSTRUCTIONS_MAX",
    "PROVIDERS_MIN",
    "PROVIDERS_MAX",
    "PROVIDER_NAME_MIN",
    "PROVIDER_NAME_MAX",
    "FIELD_HOURS",
    "FIELD_LOCATION",
    "FIELD_SERVICES",
    "FIELD_PROVIDERS",
    "REQUIRED_FIELDS",
    "ViolationKind",
    "ConfigViolation",
    "ConfigValidationResult",
    "validate_config",
]
