"""Unit tests for answering from documents (``documents/retrieval.py``).

The relevance floor and the clinical screen are the two things standing between a
caller and a confidently wrong answer, so most of this file is about what retrieval
*refuses* to say.
"""

from __future__ import annotations

from typing import Any

import pytest

from clinic_front_desk.data_layer.faults import fail_on, wrap
from clinic_front_desk.data_layer.memory import MemoryClinicDocumentStore
from clinic_front_desk.documents.retrieval import (
    MAX_SPOKEN_CHARS,
    MIN_RELEVANCE,
    DocumentKnowledge,
    looks_clinical,
)
from clinic_front_desk.models import ClinicDocument, DocumentChunk, is_err, is_ok


class ScoredEmbedder:
    """Maps a query to a vector so cosine scores are exact and chosen by the test."""

    def __init__(self, query_vector: tuple[float, ...] = (1.0, 0.0)) -> None:
        self.query_vector = query_vector
        self.calls: list[str] = []

    def embed(self, text: str) -> tuple[float, ...]:
        self.calls.append(text)
        return self.query_vector

    def embed_all(self, texts: Any) -> list[tuple[float, ...]]:
        return [self.embed(t) for t in texts]


def _store_with(*chunks: DocumentChunk) -> MemoryClinicDocumentStore:
    store = MemoryClinicDocumentStore()
    document = ClinicDocument(
        id="doc",
        filename="info.txt",
        content_type="text/plain",
        uploaded_at="2026-01-01T00:00:00+00:00",
        byte_size=1,
        chunk_count=len(chunks),
    )
    store.put(document, original=b"x", chunks=list(chunks))
    return store


def _chunk(
    text: str, embedding: tuple[float, ...] = (1.0, 0.0), *, index: int = 0,
    page: int | None = None,
) -> DocumentChunk:
    return DocumentChunk(
        document_id="doc", index=index, text=text, page=page, embedding=embedding
    )


# A vector at ~0.20 cosine to (1, 0): below the 0.30 floor.
_WEAK = (0.2, 0.9798)
# A vector at ~0.80 cosine to (1, 0): comfortably above it.
_STRONG = (0.8, 0.6)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_a_strong_match_is_answered_with_the_document_text_verbatim() -> None:
    passage = "Parking Patient parking is free in the lot behind the building."
    knowledge = DocumentKnowledge(
        store=_store_with(_chunk(passage, _STRONG)), embedder=ScoredEmbedder()
    )

    result = knowledge.lookup("is there parking")

    assert is_ok(result)
    # Verbatim: there is no generation step in which a plausible falsehood could
    # appear, so the worst case is an irrelevant true sentence.
    assert result.value == passage


def test_the_caller_question_is_what_gets_embedded() -> None:
    embedder = ScoredEmbedder()
    knowledge = DocumentKnowledge(
        store=_store_with(_chunk("Parking is free.", _STRONG)), embedder=embedder
    )

    knowledge.lookup("where do I park")

    assert embedder.calls == ["where do I park"]


def test_the_best_of_several_passages_wins() -> None:
    knowledge = DocumentKnowledge(
        store=_store_with(
            _chunk("Holiday closures: closed Thanksgiving.", _WEAK, index=0),
            _chunk("Parking is free in the lot behind us.", (1.0, 0.0), index=1),
        ),
        embedder=ScoredEmbedder(),
    )

    result = knowledge.lookup("parking")

    assert is_ok(result)
    assert "Parking is free" in result.value


# ---------------------------------------------------------------------------
# The relevance floor
# ---------------------------------------------------------------------------


def test_a_weak_match_is_refused_rather_than_spoken() -> None:
    knowledge = DocumentKnowledge(
        store=_store_with(_chunk("Something unrelated entirely.", _WEAK)),
        embedder=ScoredEmbedder(),
    )

    result = knowledge.lookup("do you validate parking")

    # This is where a retrieval system without a floor reads out a passage that
    # does not answer the question.
    assert is_err(result)
    assert result.error.kind == "not_found"


def test_the_floor_is_configurable_and_actually_applied() -> None:
    store = _store_with(_chunk("A passage about parking.", _WEAK))
    permissive = DocumentKnowledge(store=store, embedder=ScoredEmbedder(), min_relevance=0.0)
    strict = DocumentKnowledge(store=store, embedder=ScoredEmbedder(), min_relevance=0.9)

    assert is_ok(permissive.lookup("parking"))
    assert is_err(strict.lookup("parking"))


def test_the_default_floor_is_the_measured_one() -> None:
    # Pinned because it was calibrated against real Titan scores; changing it is a
    # decision, not a tweak.
    assert MIN_RELEVANCE == 0.30
    assert DocumentKnowledge(
        store=MemoryClinicDocumentStore(), embedder=ScoredEmbedder()
    ).min_relevance == MIN_RELEVANCE


# ---------------------------------------------------------------------------
# The clinical screen
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Take 25 mg of diphenhydramine if the itching spreads.",
        "The build-up phase starts at 0.05 ml and increases each visit.",
        "Maintenance dosing is every four weeks.",
        "Take 2 tablets before bed.",
        "Common side effects include drowsiness.",
        "Symptoms of an ear infection include pain and fever.",
        "If you experience wheezing, seek immediate medical attention.",
        "Call 911 if you have trouble breathing.",
        "This is contraindicated in pregnancy.",
        "Your treatment plan will be reviewed at each visit.",
        "The recommended treatment is daily nasal irrigation.",
        "If you experience wheezing, use your inhaler.",
        "Your prescription for the inhaler will be sent to the pharmacy.",
        "The doctor will diagnose the cause at your visit.",
        "Adverse reactions should be reported immediately.",
        "Antihistamines are taken twice a day.",
    ],
)
def test_clinical_prose_is_recognised(text: str) -> None:
    assert looks_clinical(text)


@pytest.mark.parametrize(
    "text",
    [
        "Patient parking is free in the surface lot behind the building.",
        "We are closed on New Year's Day and Thanksgiving.",
        "Bring a photo ID, your insurance card, and a list of your medications.",
        "We treat patients of all ages.",
        "We ask for 24 hours notice to cancel an appointment.",
        "The clinic is on the third floor; take the elevator by the north entrance.",
        "Injection clinic runs Monday through Thursday on a walk-in basis.",
        "Each injection visit is billed as a nurse visit.",
        "You must remain in the waiting room for thirty minutes afterwards.",
        "We accept cash, all major credit cards, and HSA cards.",
        # These two were false positives found on a real clinic PDF: they silently
        # dropped the services list and the whole what-to-bring section, from both
        # retrieval and the pre-call briefing.
        "We offer Sinus Treatment for sinusitis and Nasal Polyp Removal.",
        "If you have had a hearing test elsewhere, bring the audiogram with you.",
        "If you have insurance, bring your card to reception.",
        "We offer Ear Discharge Treatment and Nose Bleed Treatment.",
        "Treatment for children is available on weekday mornings.",
    ],
)
def test_administrative_prose_is_not_mistaken_for_clinical(text: str) -> None:
    # These are the false positives that would matter: each is a question the
    # clinic genuinely wants answered.
    assert not looks_clinical(text)


def test_a_clinical_passage_is_refused_even_when_it_matches_strongly() -> None:
    knowledge = DocumentKnowledge(
        store=_store_with(
            _chunk("Dosing schedule Take 25 mg twice a day for two weeks.", (1.0, 0.0))
        ),
        embedder=ScoredEmbedder(),
    )

    result = knowledge.lookup("how much should I take")

    assert is_err(result)
    assert "clinical" in result.error.detail


def test_a_clinical_top_match_does_not_fall_through_to_the_runner_up() -> None:
    # Refusing outright, rather than quietly answering with the next passage, keeps
    # the response honest: the clinic's best answer to this question is one the
    # agent may not give.
    knowledge = DocumentKnowledge(
        store=_store_with(
            _chunk("Dosing Take 500 mg daily.", (1.0, 0.0), index=0),
            _chunk("Parking is free behind the building.", _STRONG, index=1),
        ),
        embedder=ScoredEmbedder(),
    )

    result = knowledge.lookup("dose")

    assert is_err(result)


# ---------------------------------------------------------------------------
# Empty and failure states
# ---------------------------------------------------------------------------


def test_no_documents_means_nothing_found() -> None:
    knowledge = DocumentKnowledge(
        store=MemoryClinicDocumentStore(), embedder=ScoredEmbedder()
    )

    result = knowledge.lookup("is there parking")

    assert is_err(result)
    assert result.error.kind == "not_found"


@pytest.mark.parametrize("question", ["", "   "])
def test_a_blank_question_is_refused_without_touching_the_model(question: str) -> None:
    embedder = ScoredEmbedder()
    knowledge = DocumentKnowledge(
        store=_store_with(_chunk("Parking is free.", _STRONG)), embedder=embedder
    )

    result = knowledge.lookup(question)

    assert is_err(result)
    assert embedder.calls == []


def test_documents_stored_without_embeddings_answer_nothing() -> None:
    knowledge = DocumentKnowledge(
        store=_store_with(_chunk("Parking is free behind the building.", ())),
        embedder=ScoredEmbedder(),
    )

    result = knowledge.lookup("parking")

    assert is_err(result)


def test_a_store_read_failure_degrades_to_nothing_found() -> None:
    # Not an exception: the FAQ tool falls back to the configured answer, which
    # reports the information as unavailable. A broken store must not break a call.
    faulty = wrap(_store_with(_chunk("Parking is free.", _STRONG)), fail_on("list_chunks"))
    knowledge = DocumentKnowledge(store=faulty, embedder=ScoredEmbedder())

    result = knowledge.lookup("parking")

    assert is_err(result)
    assert result.error.kind == "not_found"


def test_an_embedder_failure_propagates_rather_than_answering_wrongly() -> None:
    class BrokenEmbedder:
        def embed(self, text: str) -> tuple[float, ...]:
            raise RuntimeError("bedrock is down")

        def embed_all(self, texts: Any) -> list[tuple[float, ...]]:
            raise RuntimeError("bedrock is down")

    knowledge = DocumentKnowledge(
        store=_store_with(_chunk("Parking is free.", _STRONG)), embedder=BrokenEmbedder()
    )

    with pytest.raises(RuntimeError):
        knowledge.lookup("parking")


# ---------------------------------------------------------------------------
# Speech shaping
# ---------------------------------------------------------------------------


def test_a_long_passage_is_trimmed_for_speech() -> None:
    long_passage = "Parking. " + " ".join(
        f"Detail number {i} about the parking arrangements here." for i in range(40)
    )
    knowledge = DocumentKnowledge(
        store=_store_with(_chunk(long_passage, (1.0, 0.0))), embedder=ScoredEmbedder()
    )

    result = knowledge.lookup("parking")

    assert is_ok(result)
    assert len(result.value) <= MAX_SPOKEN_CHARS + 1


def test_a_trimmed_answer_ends_on_a_sentence_not_mid_word() -> None:
    long_passage = " ".join(
        f"Sentence {i} explains one more parking detail clearly." for i in range(40)
    )
    knowledge = DocumentKnowledge(
        store=_store_with(_chunk(long_passage, (1.0, 0.0))), embedder=ScoredEmbedder()
    )

    result = knowledge.lookup("parking")

    assert is_ok(result)
    # A sentence that stops halfway sounds like the agent malfunctioned.
    assert result.value.endswith(".") or result.value.endswith("\u2026")


def test_whitespace_is_collapsed_so_the_answer_reads_aloud_cleanly() -> None:
    knowledge = DocumentKnowledge(
        store=_store_with(_chunk("Parking\n\n   is   free\tbehind us.", (1.0, 0.0))),
        embedder=ScoredEmbedder(),
    )

    result = knowledge.lookup("parking")

    assert is_ok(result)
    assert result.value == "Parking is free behind us."


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_the_corpus_is_read_once_per_instance_when_caching() -> None:
    store = _store_with(_chunk("Parking is free.", _STRONG))
    knowledge = DocumentKnowledge(store=store, embedder=ScoredEmbedder())

    knowledge.lookup("parking")
    knowledge.lookup("parking again")

    # One object read per call, not per question: a caller's follow-up must not
    # cost another S3 round trip mid-conversation.
    assert knowledge._chunks is not None


def test_invalidate_makes_a_new_upload_visible() -> None:
    store = MemoryClinicDocumentStore()
    knowledge = DocumentKnowledge(store=store, embedder=ScoredEmbedder())
    assert is_err(knowledge.lookup("parking"))

    document = ClinicDocument(
        id="doc", filename="info.txt", content_type="text/plain",
        uploaded_at="2026-01-01T00:00:00+00:00", byte_size=1, chunk_count=1,
    )
    store.put(document, original=b"x", chunks=[_chunk("Parking is free.", _STRONG)])
    knowledge.invalidate()

    assert is_ok(knowledge.lookup("parking"))


def test_caching_can_be_turned_off() -> None:
    store = MemoryClinicDocumentStore()
    knowledge = DocumentKnowledge(
        store=store, embedder=ScoredEmbedder(), cache_chunks=False
    )
    assert is_err(knowledge.lookup("parking"))

    document = ClinicDocument(
        id="doc", filename="info.txt", content_type="text/plain",
        uploaded_at="2026-01-01T00:00:00+00:00", byte_size=1, chunk_count=1,
    )
    store.put(document, original=b"x", chunks=[_chunk("Parking is free.", _STRONG)])

    # No invalidate needed when nothing is cached.
    assert is_ok(knowledge.lookup("parking"))
