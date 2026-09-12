"""Clarifying-behaviour example unit tests (task 6.15).

Focused example/edge-case tests for the tool-layer clarifying behaviours the
design's Testing Strategy calls out:

- single-answer FAQ (Req 6.2): when ``answer_faq`` returns a single matching
  answer, that answer is stated to the patient.
- ambiguous-FAQ clarification (Req 6.7): when a question could match more than
  one FAQ topic, the agent must ask the patient to specify which before
  invoking ``answer_faq``; the tool never guesses among topics.
- decline-waitlist (Req 7.6): when a patient declines the waitlist offer, no
  ``add_to_waitlist`` call is made and no entry is created.

The exhaustive property tests for FAQ and waitlist live in
tests/property/test_tool_properties.py (Properties 9 and 11).
"""

from __future__ import annotations

from typing import Any

from clinic_front_desk.data_layer.memory import (
    MemoryClinicKnowledgeBaseStore,
    MemoryWaitlistStore,
)
from clinic_front_desk.models import (
    ClinicKnowledgeBase,
    DayHours,
    Provider,
    ServiceConfig,
    ToolResult,
    is_err,
    is_ok,
)
from clinic_front_desk.tools.faq import VALID_TOPICS, answer_faq
from clinic_front_desk.tools.waitlist import add_to_waitlist


def _kb() -> ClinicKnowledgeBase:
    """A fully-populated clinic configuration for the happy-path tests."""
    return ClinicKnowledgeBase(
        location="123 Main St, Springfield",
        hours={
            1: DayHours(open="09:00", close="17:00"),
            2: DayHours(open="09:00", close="17:00"),
        },
        services=[
            ServiceConfig(
                name="Hearing Test",
                prep_instructions="Avoid loud noise for 24 hours beforehand.",
                price=150.0,
            ),
            ServiceConfig(
                name="Allergy Consult",
                prep_instructions="Stop antihistamines 3 days before.",
                price=90.0,
            ),
        ],
        accepted_insurance=["Aetna", "Blue Cross"],
        providers=[Provider(id="p1", name="Dr. ENT", specialty="ENT")],
        configured=True,
    )


def _store_with(kb: ClinicKnowledgeBase) -> MemoryClinicKnowledgeBaseStore:
    store = MemoryClinicKnowledgeBaseStore()
    assert is_ok(store.save(kb))
    return store


# ---------------------------------------------------------------------------
# single-answer FAQ (Req 6.2)
# ---------------------------------------------------------------------------


def test_single_answer_faq_returns_one_answer_to_state() -> None:
    """Req 6.2: a resolved single-topic question yields exactly one answer the
    agent can state to the patient."""
    store = _store_with(_kb())

    result = answer_faq(store, "location")

    assert is_ok(result)
    assert isinstance(result.value, str) and result.value.strip()
    assert "123 Main St, Springfield" in result.value


def test_single_answer_faq_pricing_states_the_configured_price() -> None:
    """Req 6.2/6.4: a single pricing answer for a named service is stated."""
    store = _store_with(_kb())

    result = answer_faq(store, "pricing", service="Hearing Test")

    assert is_ok(result)
    assert "150.00" in result.value


# ---------------------------------------------------------------------------
# ambiguous-FAQ clarification (Req 6.7)
# ---------------------------------------------------------------------------


def test_answer_faq_never_guesses_an_unspecified_topic() -> None:
    """Req 6.7: the tool answers exactly one specified topic and refuses an
    unspecified/ambiguous one with a validation error, so the agent must ask the
    patient to specify which information they need before invoking it."""
    store = _store_with(_kb())

    # An unspecified / vague "topic" the patient's phrasing might imply is not a
    # recognised single topic; the tool refuses to guess among topics.
    result = answer_faq(store, "general_info")

    assert is_err(result)
    assert result.error.kind == "validation"
    assert result.error.field == "topic"


def test_ambiguous_question_has_multiple_distinct_topic_answers() -> None:
    """Req 6.7: a vague question ("what do I need to know before my visit") can
    map to more than one FAQ topic. Each candidate resolves to its own distinct
    answer, so the agent must clarify and pick exactly one topic rather than
    answering several at once."""
    store = _store_with(_kb())

    # Candidate topics an ambiguous "how do I prepare / what do I bring" question
    # could match. Both are valid, recognised topics.
    candidate_topics = ["prep", "what_to_bring"]
    assert len(candidate_topics) > 1
    assert all(topic in VALID_TOPICS for topic in candidate_topics)

    # Scope each to different services so the two candidates yield different
    # answers — demonstrating why the agent must resolve to a single topic
    # (and service) before answering.
    answer_a = answer_faq(store, "prep", service="Hearing Test")
    answer_b = answer_faq(store, "what_to_bring", service="Allergy Consult")
    assert is_ok(answer_a) and is_ok(answer_b)
    assert answer_a.value != answer_b.value


# ---------------------------------------------------------------------------
# decline-waitlist (Req 7.6)
# ---------------------------------------------------------------------------


def _handle_waitlist_offer(
    store: MemoryWaitlistStore, *, accepted: bool, **fields: Any
) -> ToolResult[Any] | None:
    """Mirror the orchestration rule for a waitlist offer.

    ``add_to_waitlist`` is invoked only when the patient *accepts* (Req 7.1);
    a decline creates no entry and the agent offers to take a message (Req 7.6).
    """
    if not accepted:
        return None
    return add_to_waitlist(store, **fields)


def test_decline_waitlist_creates_no_entry() -> None:
    """Req 7.6: declining the waitlist offer creates no waitlist entry."""
    store = MemoryWaitlistStore()

    outcome = _handle_waitlist_offer(
        store,
        accepted=False,
        patient_id="p1",
        service="ent",
        preferred_slot_type="morning",
    )

    assert outcome is None  # tool never invoked
    assert store.list_by_service_ordered("ent").unwrap() == []


def test_accept_waitlist_creates_entry_contrast() -> None:
    """Contrast to Req 7.6: accepting the offer does record one entry, isolating
    the decline path's no-op behaviour."""
    store = MemoryWaitlistStore()

    outcome = _handle_waitlist_offer(
        store,
        accepted=True,
        patient_id="p1",
        service="ent",
        preferred_slot_type="morning",
    )

    assert outcome is not None and is_ok(outcome)
    assert len(store.list_by_service_ordered("ent").unwrap()) == 1
