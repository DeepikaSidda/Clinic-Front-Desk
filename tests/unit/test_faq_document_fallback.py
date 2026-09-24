"""Tests for the document fallback inside ``answer_faq``.

The ordering rules here are the whole safety argument for letting a document reach
a caller at all: configuration is authoritative, documents only fill genuine gaps,
and the failures that mean "do not answer this" are never overridden. Each of those
gets a test, because each is a different way the feature could quietly go wrong.
"""

from __future__ import annotations

from typing import Any

import pytest

from clinic_front_desk.data_layer.faults import fail_on, wrap
from clinic_front_desk.data_layer.memory import (
    MemoryClinicDocumentStore,
    MemoryClinicKnowledgeBaseStore,
)
from clinic_front_desk.documents.retrieval import DocumentKnowledge
from clinic_front_desk.models import (
    ClinicDocument,
    ClinicKnowledgeBase,
    DayHours,
    DocumentChunk,
    Provider,
    ServiceConfig,
    is_err,
    is_ok,
)
from clinic_front_desk.tools.faq import (
    ANSWERABLE_TOPICS,
    DOCUMENT_FALLBACK_TOPICS,
    DOCUMENT_TOPIC,
    VALID_TOPICS,
    answer_faq,
)

PARKING = "Parking Patient parking is free in the surface lot behind the building."
HOLIDAYS = "Holiday closures We are closed on New Year's Day and Thanksgiving."
FEES = "Fees A skin graft consultation is billed at four hundred dollars."


class FixedEmbedder:
    """Everything embeds to the same vector, so every passage matches perfectly."""

    def embed(self, text: str) -> tuple[float, ...]:
        return (1.0, 0.0)

    def embed_all(self, texts: Any) -> list[tuple[float, ...]]:
        return [self.embed(t) for t in texts]


def _documents(
    *texts: str, embedding: tuple[float, ...] = (1.0, 0.0)
) -> DocumentKnowledge:
    """A document corpus whose passages score ``embedding`` against every query."""
    store = MemoryClinicDocumentStore()
    store.put(
        ClinicDocument(
            id="doc",
            filename="info.txt",
            content_type="text/plain",
            uploaded_at="2026-01-01T00:00:00+00:00",
            byte_size=1,
            chunk_count=len(texts),
        ),
        original=b"x",
        chunks=[
            DocumentChunk(
                document_id="doc", index=i, text=text, page=None, embedding=embedding
            )
            for i, text in enumerate(texts)
        ],
    )
    return DocumentKnowledge(store=store, embedder=FixedEmbedder())


def _kb(**overrides: Any) -> ClinicKnowledgeBase:
    kb = ClinicKnowledgeBase(
        location="123 Configured Street, Springfield",
        hours={1: DayHours(open="09:00", close="17:00")},
        services=[
            ServiceConfig(name="Hearing Test", price=150.0),
            ServiceConfig(name="Consultation", price=None),
        ],
        accepted_insurance=["Aetna"],
        providers=[Provider(id="p1", name="Dr. ENT", specialty="ENT")],
        configured=True,
    )
    for key, value in overrides.items():
        setattr(kb, key, value)
    return kb


def _store(kb: ClinicKnowledgeBase | None) -> MemoryClinicKnowledgeBaseStore:
    store = MemoryClinicKnowledgeBaseStore()
    if kb is not None:
        store.save(kb)
    return store


# ---------------------------------------------------------------------------
# Topic surface
# ---------------------------------------------------------------------------


def test_the_structured_topics_mirror_the_configured_fields() -> None:
    # The structured topics mirror the wizard's fields; the document topic is
    # additional, not a replacement.
    assert VALID_TOPICS == {
        "hours",
        "location",
        "what_to_bring",
        "prep",
        "insurance",
        "pricing",
        "contact",
    }


def test_the_document_topic_is_answerable_but_not_a_structured_topic() -> None:
    assert DOCUMENT_TOPIC not in VALID_TOPICS
    assert DOCUMENT_TOPIC in ANSWERABLE_TOPICS


def test_pricing_is_excluded_from_the_fallback() -> None:
    # A price lifted from a document could be last year's, another plan's, or from
    # a comparison sheet — quoted in a voice the caller treats as the clinic's.
    assert "pricing" not in DOCUMENT_FALLBACK_TOPICS
    assert DOCUMENT_FALLBACK_TOPICS == VALID_TOPICS - {"pricing", "contact"}


def test_an_unrecognised_topic_is_still_a_validation_error() -> None:
    result = answer_faq(
        _store(_kb()), "general_info", question="anything", documents=_documents(PARKING)
    )

    assert is_err(result)
    assert result.error.kind == "validation"


# ---------------------------------------------------------------------------
# Configuration is authoritative
# ---------------------------------------------------------------------------


def test_a_configured_answer_wins_over_a_matching_document() -> None:
    result = answer_faq(
        _store(_kb()),
        "location",
        question="where exactly are you",
        documents=_documents("The clinic is at 999 Document Drive, Shelbyville."),
    )

    assert is_ok(result)
    assert "123 Configured Street" in result.value
    assert "Document Drive" not in result.value


def test_configured_hours_win_over_a_document() -> None:
    result = answer_faq(
        _store(_kb()), "hours", question="when are you open", documents=_documents(HOLIDAYS)
    )

    assert is_ok(result)
    assert "Monday" in result.value


def test_configured_insurance_wins_over_a_document() -> None:
    result = answer_faq(
        _store(_kb()),
        "insurance",
        question="what insurance do you take",
        documents=_documents("We accept every plan under the sun."),
    )

    assert is_ok(result)
    assert "Aetna" in result.value


# ---------------------------------------------------------------------------
# Documents fill genuine gaps
# ---------------------------------------------------------------------------


def test_an_unconfigured_location_falls_back_to_the_document() -> None:
    result = answer_faq(
        _store(_kb(location="")),
        "location",
        question="where is the clinic",
        documents=_documents("The clinic is on the third floor of 123 Main Street."),
    )

    assert is_ok(result)
    assert "third floor" in result.value


def test_unconfigured_hours_fall_back_to_the_document() -> None:
    result = answer_faq(
        _store(_kb(hours={d: None for d in range(7)})),
        "hours",
        question="what days are you closed",
        documents=_documents(HOLIDAYS),
    )

    assert is_ok(result)
    assert "Thanksgiving" in result.value


def test_an_unconfigured_clinic_can_still_answer_from_documents() -> None:
    # A doctor may upload the practice sheet before finishing the wizard.
    result = answer_faq(
        _store(None), "location", question="where are you", documents=_documents(PARKING)
    )

    assert is_ok(result)


def test_the_fallback_works_without_a_question_using_a_topic_description() -> None:
    # Passing the caller's words retrieves far better, but the tool must still be
    # usable when the model omits them.
    result = answer_faq(
        _store(_kb(location="")), "location", documents=_documents(PARKING)
    )

    assert is_ok(result)


# ---------------------------------------------------------------------------
# The document topic
# ---------------------------------------------------------------------------


def test_the_document_topic_answers_the_descriptive_long_tail() -> None:
    result = answer_faq(
        _store(_kb()),
        DOCUMENT_TOPIC,
        question="is there parking at the clinic",
        documents=_documents(PARKING),
    )

    assert is_ok(result)
    assert "surface lot" in result.value


def test_the_document_topic_needs_the_callers_question() -> None:
    result = answer_faq(_store(_kb()), DOCUMENT_TOPIC, documents=_documents(PARKING))

    assert is_err(result)
    assert result.error.kind == "validation"
    assert result.error.field == "question"


@pytest.mark.parametrize("question", ["", "   "])
def test_the_document_topic_rejects_a_blank_question(question: str) -> None:
    result = answer_faq(
        _store(_kb()), DOCUMENT_TOPIC, question=question, documents=_documents(PARKING)
    )

    assert is_err(result)
    assert result.error.kind == "validation"


def test_the_document_topic_reports_nothing_found_when_no_documents_exist() -> None:
    result = answer_faq(_store(_kb()), DOCUMENT_TOPIC, question="is there parking")

    assert is_err(result)
    assert result.error.kind == "not_found"


def test_the_document_topic_does_not_need_a_configured_clinic() -> None:
    result = answer_faq(
        _store(None), DOCUMENT_TOPIC, question="is there parking",
        documents=_documents(PARKING),
    )

    assert is_ok(result)


# ---------------------------------------------------------------------------
# Failures that must never fall back
# ---------------------------------------------------------------------------


def test_pricing_never_falls_back_even_when_a_document_names_a_figure() -> None:
    result = answer_faq(
        _store(_kb()),
        "pricing",
        service="Skin Graft Consultation",
        question="how much is a skin graft consultation",
        documents=_documents(FEES),
    )

    assert is_err(result)
    assert result.error.kind == "not_offered"


def test_an_offered_service_with_no_configured_price_stays_unavailable() -> None:
    result = answer_faq(
        _store(_kb()),
        "pricing",
        service="Consultation",
        question="how much is a consultation",
        documents=_documents("A consultation costs two hundred dollars."),
    )

    assert is_err(result)
    assert result.error.kind == "not_found"
    # The configuration's more specific reason survives, rather than being replaced
    # by a vague retrieval miss.
    assert "Consultation" in result.error.detail


def test_a_service_that_is_not_offered_never_falls_back() -> None:
    # This is the guardrail that stops the agent discussing a service the clinic
    # does not provide; a document mentioning one must not re-open it.
    result = answer_faq(
        _store(_kb()),
        "prep",
        service="Rhinoplasty",
        question="how do I prepare for rhinoplasty",
        documents=_documents("Before rhinoplasty, stop taking aspirin."),
    )

    assert is_err(result)
    assert result.error.kind == "not_offered"


def test_a_knowledge_base_read_failure_is_not_papered_over_with_a_document() -> None:
    # A Data_Layer that cannot be read should produce "let me take a message", not
    # an answer that may contradict the configuration.
    faulty = wrap(_store(_kb()), fail_on("get"))

    result = answer_faq(
        faulty, "location", question="where are you", documents=_documents(PARKING)
    )

    assert is_err(result)
    assert result.error.kind == "store_failure"


def test_a_clinical_document_passage_is_not_returned_as_an_faq_answer() -> None:
    result = answer_faq(
        _store(_kb(location="")),
        "location",
        question="where are you",
        documents=_documents("Dosing Take 25 mg twice a day with food."),
    )

    assert is_err(result)


# ---------------------------------------------------------------------------
# Backwards compatibility
# ---------------------------------------------------------------------------


def test_without_documents_the_tool_behaves_exactly_as_before() -> None:
    configured = answer_faq(_store(_kb()), "location")
    missing = answer_faq(_store(_kb(location="")), "location")

    assert is_ok(configured)
    assert "123 Configured Street" in configured.value
    assert is_err(missing)
    assert missing.error.kind == "not_found"


def test_passing_a_question_alone_changes_nothing_without_documents() -> None:
    with_question = answer_faq(_store(_kb()), "location", question="where are you")
    without = answer_faq(_store(_kb()), "location")

    assert is_ok(with_question) and is_ok(without)
    assert with_question.value == without.value


def test_a_retrieval_miss_leaves_the_original_reason_intact() -> None:
    # The corpus has content, but nothing that clears the relevance floor: ~0.20
    # cosine against the query vector, below the 0.30 default.
    documents = _documents(PARKING, embedding=(0.2, 0.9798))

    result = answer_faq(
        _store(_kb(accepted_insurance=[])),
        "insurance",
        question="what insurance do you take",
        documents=documents,
    )

    assert is_err(result)
    assert result.error.kind == "not_found"
    assert "insurance" in result.error.detail
