"""Property-based tests for the clinic-configuration domain (tasks 5.2, 5.3).

These validate two design correctness properties against the real
``save_clinic_config`` / ``validate_config`` logic (task 5.1) backed by the
in-memory ``MemoryClinicKnowledgeBaseStore`` fake:

- **Property 1** — clinic-config validation respects every field bound (Req 1.2).
- **Property 2** — missing-required-field rejection is complete and
  non-destructive (Req 1.5).

Both are implemented as a single Hypothesis test running >= 100 iterations. Per
the design's "Generators" note, the generators intentionally straddle every
bound: services count 0-120 (bound 1-100), prep-instruction length around 2,000,
price around the 0.01 / 999,999.99 bounds, providers count 0-60 (bound 1-50),
and provider-name length around 1 / 100 chars.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from clinic_front_desk.config import (
    PREP_INSTRUCTIONS_MAX,
    PROVIDER_NAME_MAX,
    PROVIDER_NAME_MIN,
    PROVIDERS_MAX,
    PROVIDERS_MIN,
    SERVICES_MAX,
    SERVICES_MIN,
    save_clinic_config,
)
from clinic_front_desk.config.validation import REQUIRED_FIELDS
from clinic_front_desk.data_layer.memory.clinic_knowledge_base_store import (
    MemoryClinicKnowledgeBaseStore,
)
from clinic_front_desk.models import (
    MONEY_MAX,
    MONEY_MIN,
    ClinicKnowledgeBase,
    DayHours,
    Provider,
    ScheduleRule,
    ServiceConfig,
    is_ok,
)

pytestmark = pytest.mark.property

_FIXED_CLOCK = "2025-06-01T00:00:00+00:00"


def _clock() -> str:
    return _FIXED_CLOCK


def _fresh_hours() -> dict[int, DayHours | None]:
    """A minimal present ``hours`` map (one open weekday)."""
    return {1: DayHours(open="09:00", close="17:00")}


def _fresh_schedule() -> list[ScheduleRule]:
    return [ScheduleRule(day_of_week=1, start="09:00", end="17:00")]


# ---------------------------------------------------------------------------
# Property 1 generators — straddle every field bound (design "Generators").
# ---------------------------------------------------------------------------

# Prices straddling the 0.01 / 999,999.99 bounds, plus a spread of random values
# above and below.
_price = st.one_of(
    st.none(),
    st.sampled_from(
        [
            -1.0,
            0.0,
            0.009,
            MONEY_MIN,  # 0.01, inclusive lower bound
            100.0,
            MONEY_MAX,  # 999,999.99, inclusive upper bound
            1_000_000.0,
            1_000_000.01,
        ]
    ),
    st.floats(
        min_value=0.0, max_value=1_000_001.0, allow_nan=False, allow_infinity=False
    ),
)

# Prep-instruction length straddling the 2,000-char bound. Built by repetition so
# large strings are cheap to generate.
_prep = st.one_of(
    st.none(),
    st.integers(min_value=0, max_value=PREP_INSTRUCTIONS_MAX + 5).map(lambda n: "x" * n),
)

_service = st.builds(
    ServiceConfig,
    name=st.sampled_from(["Hearing Test", "Cleaning", "Consult", "Fitting"]),
    prep_instructions=_prep,
    price=_price,
)

# Provider-name length straddling the 1 / 100-char bounds (0 => empty name is out
# of bounds; > 100 is out of bounds).
_provider = st.builds(
    Provider,
    id=st.integers(min_value=0, max_value=1_000_000).map(lambda n: f"prov-{n}"),
    name=st.integers(min_value=0, max_value=PROVIDER_NAME_MAX + 2).map(lambda n: "x" * n),
    specialty=st.just("ENT"),
    schedule=st.builds(_fresh_schedule),
)

# A config whose required fields (hours, location) are always present, so the
# only thing that varies acceptance is whether the bounded fields are in range.
_bounds_config = st.builds(
    ClinicKnowledgeBase,
    location=st.just("123 Main St"),
    hours=st.builds(_fresh_hours),
    services=st.lists(_service, min_size=0, max_size=120),
    accepted_insurance=st.just([]),
    providers=st.lists(_provider, min_size=0, max_size=60),
)


def _within_all_bounds(kb: ClinicKnowledgeBase) -> bool:
    """Independent oracle: True iff every bounded field is in range.

    Because the generated config always has clinic hours and a location, this
    predicate (which also encodes the 1..N lower bounds as required-field
    presence) is exactly the acceptance condition for Property 1.
    """
    if not (SERVICES_MIN <= len(kb.services) <= SERVICES_MAX):
        return False
    if not (PROVIDERS_MIN <= len(kb.providers) <= PROVIDERS_MAX):
        return False
    for service in kb.services:
        if (
            service.prep_instructions is not None
            and len(service.prep_instructions) > PREP_INSTRUCTIONS_MAX
        ):
            return False
        if service.price is not None and not (MONEY_MIN <= service.price <= MONEY_MAX):
            return False
    for provider in kb.providers:
        if not (PROVIDER_NAME_MIN <= len(provider.name) <= PROVIDER_NAME_MAX):
            return False
    return True


# Feature: clinic-front-desk-agent, Property 1: Clinic config validation respects field bounds
@settings(max_examples=150, deadline=None)
@given(kb=_bounds_config)
def test_property_1_config_validation_respects_field_bounds(
    kb: ClinicKnowledgeBase,
) -> None:
    """The save is accepted iff every field lies within its bound (Req 1.2)."""
    store = MemoryClinicKnowledgeBaseStore()
    result = save_clinic_config(store, kb, clock=_clock)

    expected_ok = _within_all_bounds(kb)
    assert result.ok is expected_ok

    # The accept/reject decision is faithfully reflected in persistence: an
    # accepted config is stored, a rejected one leaves the store empty.
    stored = store.get()
    assert is_ok(stored)
    assert (stored.value is not None) is expected_ok


# ---------------------------------------------------------------------------
# Property 2 generators — a fully in-bounds base config whose required fields we
# then selectively clear.
# ---------------------------------------------------------------------------

_valid_service = st.builds(
    ServiceConfig,
    name=st.sampled_from(["Hearing Test", "Cleaning", "Consult", "Fitting"]),
    prep_instructions=st.one_of(
        st.none(),
        st.integers(min_value=0, max_value=PREP_INSTRUCTIONS_MAX).map(lambda n: "x" * n),
    ),
    price=st.one_of(
        st.none(),
        st.floats(
            min_value=MONEY_MIN,
            max_value=MONEY_MAX,
            allow_nan=False,
            allow_infinity=False,
        ),
    ),
)

_valid_provider = st.builds(
    Provider,
    id=st.integers(min_value=0, max_value=1_000_000).map(lambda n: f"prov-{n}"),
    name=st.integers(min_value=PROVIDER_NAME_MIN, max_value=PROVIDER_NAME_MAX).map(
        lambda n: "x" * n
    ),
    specialty=st.just("ENT"),
    schedule=st.builds(_fresh_schedule),
)

_valid_config = st.builds(
    ClinicKnowledgeBase,
    location=st.just("123 Main St"),
    hours=st.builds(_fresh_hours),
    services=st.lists(_valid_service, min_size=1, max_size=5),
    accepted_insurance=st.just([]),
    providers=st.lists(_valid_provider, min_size=1, max_size=5),
)

# A non-empty subset of the four required fields to remove.
_missing_subset = st.sets(st.sampled_from(REQUIRED_FIELDS), min_size=1)


def _clear_field(kb: ClinicKnowledgeBase, field: str) -> ClinicKnowledgeBase:
    """Return a copy of ``kb`` with the named required field made absent."""
    if field == "hours":
        return replace(kb, hours={})
    if field == "location":
        return replace(kb, location="")
    if field == "services":
        return replace(kb, services=[])
    if field == "providers":
        return replace(kb, providers=[])
    raise AssertionError(f"unknown required field {field!r}")


# Feature: clinic-front-desk-agent, Property 2: Missing-required-field rejection is complete and non-destructive
@settings(max_examples=200, deadline=None)
@given(base=_valid_config, missing=_missing_subset)
def test_property_2_missing_required_field_rejection_complete_and_nondestructive(
    base: ClinicKnowledgeBase, missing: set[str]
) -> None:
    """Rejection names exactly the missing required fields; values are retained (Req 1.5)."""
    kb = base
    for field in missing:
        kb = _clear_field(kb, field)

    store = MemoryClinicKnowledgeBaseStore()
    result = save_clinic_config(store, kb, clock=_clock)

    # The save is rejected because required fields are absent.
    assert result.ok is False
    assert result.failed_validation is True

    # The rejection names exactly the missing required fields — no more, no less.
    assert set(result.validation.missing_required_fields) == missing

    # The base was fully in-bounds, so there are no spurious bound violations:
    # every reported problem is a missing-required-field one.
    assert result.validation.out_of_bounds_fields == ()

    # The previously entered values are retained unchanged (same object, so
    # nothing was mutated or dropped).
    assert result.retained_config is kb

    # Nothing was persisted (non-destructive; the store was never touched).
    stored = store.get()
    assert is_ok(stored)
    assert stored.value is None
