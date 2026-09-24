"""Unit tests for the ``answer_faq`` Strands tool (task 6.8, Req 6.1–6.6).

These are focused example/edge-case tests. The exhaustive property test for FAQ
pricing and information availability (Property 9) lives in task 6.9; the
clarifying-behaviour unit tests (single-answer, ambiguity, decline) live in
task 6.15.
"""

from __future__ import annotations

import pytest

from clinic_front_desk.data_layer.faults import fail_on, wrap
from clinic_front_desk.data_layer.memory import MemoryClinicKnowledgeBaseStore
from clinic_front_desk.models import (
    ClinicKnowledgeBase,
    DayHours,
    Provider,
    ServiceConfig,
    is_err,
    is_ok,
)
from clinic_front_desk.tools.faq import VALID_TOPICS, answer_faq


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _kb() -> ClinicKnowledgeBase:
    """A fully-populated clinic configuration for the happy-path tests."""
    return ClinicKnowledgeBase(
        location="123 Main St, Springfield",
        hours={
            0: None,  # closed Sunday
            1: DayHours(open="09:00", close="17:00"),
            2: DayHours(open="09:00", close="17:00"),
            6: None,  # closed Saturday
        },
        services=[
            ServiceConfig(
                name="Hearing Test",
                prep_instructions="Avoid loud noise for 24 hours beforehand.",
                price=150.0,
            ),
            ServiceConfig(name="Consultation", prep_instructions=None, price=None),
        ],
        accepted_insurance=["Aetna", "Blue Cross"],
        providers=[Provider(id="p1", name="Dr. ENT", specialty="ENT")],
        configured=True,
    )


def _store_with(kb: ClinicKnowledgeBase) -> MemoryClinicKnowledgeBaseStore:
    store = MemoryClinicKnowledgeBaseStore()
    saved = store.save(kb)
    assert is_ok(saved)
    return store


# ---------------------------------------------------------------------------
# Topic validation
# ---------------------------------------------------------------------------


def test_unknown_topic_is_validation_error() -> None:
    store = _store_with(_kb())
    result = answer_faq(store, "menu")
    assert is_err(result)
    assert result.error.kind == "validation"
    assert result.error.field == "topic"


def test_valid_topics_set_matches_the_structured_fields() -> None:
    """``contact`` is structured for the same reason ``pricing`` is: a phone
    number is an instruction the caller acts on, and the wrong one sends them
    to a stranger."""
    assert VALID_TOPICS == {
        "hours",
        "location",
        "what_to_bring",
        "prep",
        "insurance",
        "pricing",
        "contact",
    }


# ---------------------------------------------------------------------------
# Happy-path answers (Req 6.1, 6.2)
# ---------------------------------------------------------------------------


def test_hours_answer_lists_open_days() -> None:
    result = answer_faq(_store_with(_kb()), "hours")
    assert is_ok(result)
    assert "Monday" in result.value
    assert "Tuesday" in result.value
    assert "09:00" in result.value
    # Closed days are not fabricated into hours.
    assert "Sunday" not in result.value


def test_location_answer() -> None:
    result = answer_faq(_store_with(_kb()), "location")
    assert is_ok(result)
    assert "123 Main St, Springfield" in result.value


def test_insurance_answer_lists_accepted_plans() -> None:
    result = answer_faq(_store_with(_kb()), "insurance")
    assert is_ok(result)
    assert "Aetna" in result.value
    assert "Blue Cross" in result.value


def test_prep_scoped_to_named_service() -> None:
    result = answer_faq(_store_with(_kb()), "prep", service="Hearing Test")
    assert is_ok(result)
    assert result.value == "Avoid loud noise for 24 hours beforehand."


def test_what_to_bring_without_service_returns_the_single_prep() -> None:
    result = answer_faq(_store_with(_kb()), "what_to_bring")
    assert is_ok(result)
    assert "Avoid loud noise" in result.value


def test_service_matching_is_case_insensitive() -> None:
    result = answer_faq(_store_with(_kb()), "pricing", service="hearing test")
    assert is_ok(result)
    assert "150 rupees" in result.value


# ---------------------------------------------------------------------------
# Pricing (Req 6.4, 6.5)
# ---------------------------------------------------------------------------


def test_pricing_returns_configured_price() -> None:
    result = answer_faq(_store_with(_kb()), "pricing", service="Hearing Test")
    assert is_ok(result)
    assert "Hearing Test" in result.value
    assert "150 rupees" in result.value


def test_pricing_without_service_is_validation_error() -> None:
    result = answer_faq(_store_with(_kb()), "pricing")
    assert is_err(result)
    assert result.error.kind == "validation"
    assert result.error.field == "service"


@pytest.mark.parametrize("service", ["", "   "])
def test_pricing_with_blank_service_is_validation_error(service: str) -> None:
    result = answer_faq(_store_with(_kb()), "pricing", service=service)
    assert is_err(result)
    assert result.error.kind == "validation"


def test_pricing_for_unoffered_service_is_unavailable() -> None:
    # Not offered -> unavailable (Req 6.5), never fabricated.
    result = answer_faq(_store_with(_kb()), "pricing", service="Rhinoplasty")
    assert is_err(result)
    assert result.error.kind == "not_offered"


def test_pricing_for_offered_service_without_price_is_unavailable() -> None:
    # Offered but no configured price -> unavailable (Req 6.5).
    result = answer_faq(_store_with(_kb()), "pricing", service="Consultation")
    assert is_err(result)
    assert result.error.kind == "not_found"


# ---------------------------------------------------------------------------
# Absent information -> unavailable, never fabricated (Req 6.3)
# ---------------------------------------------------------------------------


def test_hours_unavailable_when_all_days_closed() -> None:
    kb = _kb()
    kb.hours = {d: None for d in range(7)}
    result = answer_faq(_store_with(kb), "hours")
    assert is_err(result)
    assert result.error.kind == "not_found"


def test_insurance_unavailable_when_none_configured() -> None:
    kb = _kb()
    kb.accepted_insurance = []
    result = answer_faq(_store_with(kb), "insurance")
    assert is_err(result)
    assert result.error.kind == "not_found"


def test_prep_unavailable_for_service_without_instructions() -> None:
    result = answer_faq(_store_with(_kb()), "prep", service="Consultation")
    assert is_err(result)
    assert result.error.kind == "not_found"


def test_prep_unavailable_for_unoffered_service() -> None:
    result = answer_faq(_store_with(_kb()), "what_to_bring", service="Rhinoplasty")
    assert is_err(result)
    assert result.error.kind == "not_offered"


def test_topic_unavailable_when_no_config_exists() -> None:
    # Empty store (nothing onboarded yet) -> unavailable for any topic (Req 6.3).
    empty = MemoryClinicKnowledgeBaseStore()
    result = answer_faq(empty, "location")
    assert is_err(result)
    assert result.error.kind == "not_found"


# ---------------------------------------------------------------------------
# Store/tool failure -> store_failure (Req 6.6)
# ---------------------------------------------------------------------------


def test_store_read_failure_surfaces_store_failure() -> None:
    store = _store_with(_kb())
    faulty = wrap(store, fail_on("get"))
    result = answer_faq(faulty, "location")
    assert is_err(result)
    assert result.error.kind == "store_failure"
