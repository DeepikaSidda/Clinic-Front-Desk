"""Unit tests for embedding and similarity search (``documents/embeddings.py``)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from clinic_front_desk.documents.embeddings import (
    DEFAULT_DIMENSIONS,
    MAX_EMBED_CHARS,
    BedrockEmbedder,
    cosine_similarity,
    search_chunks,
)
from clinic_front_desk.models import DocumentChunk


class ScriptedEmbedder:
    """Returns a chosen vector per query, so scores are exact and assertable."""

    def __init__(self, vectors: dict[str, tuple[float, ...]]) -> None:
        self.vectors = vectors
        self.calls: list[str] = []

    def embed(self, text: str) -> tuple[float, ...]:
        self.calls.append(text)
        return self.vectors.get(text, (0.0, 0.0, 1.0))

    def embed_all(self, texts: Any) -> list[tuple[float, ...]]:
        return [self.embed(t) for t in texts]


def _chunk(
    index: int,
    text: str,
    embedding: tuple[float, ...],
    *,
    document_id: str = "doc",
    page: int | None = None,
) -> DocumentChunk:
    return DocumentChunk(
        document_id=document_id,
        index=index,
        text=text,
        page=page,
        embedding=embedding,
    )


# ---------------------------------------------------------------------------
# cosine_similarity
# ---------------------------------------------------------------------------


def test_identical_unit_vectors_score_one() -> None:
    assert cosine_similarity((1.0, 0.0), (1.0, 0.0)) == pytest.approx(1.0)


def test_orthogonal_vectors_score_zero() -> None:
    assert cosine_similarity((1.0, 0.0), (0.0, 1.0)) == pytest.approx(0.0)


def test_opposite_vectors_score_minus_one() -> None:
    assert cosine_similarity((1.0, 0.0), (-1.0, 0.0)) == pytest.approx(-1.0)


def test_magnitude_does_not_change_the_score() -> None:
    # Titan returns unit vectors, but the function must stay correct for
    # un-normalized input rather than silently returning wrong scores.
    assert cosine_similarity((3.0, 0.0), (0.5, 0.0)) == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ((), (1.0, 0.0)),
        ((1.0, 0.0), ()),
        ((), ()),
        ((0.0, 0.0), (1.0, 0.0)),  # degenerate zero vector
        ((1.0, 0.0, 0.0), (1.0, 0.0)),  # mismatched dimensions
    ],
)
def test_unusable_vectors_score_zero_rather_than_raising(
    left: tuple[float, ...], right: tuple[float, ...]
) -> None:
    assert cosine_similarity(left, right) == 0.0


# ---------------------------------------------------------------------------
# search_chunks
# ---------------------------------------------------------------------------


def test_matches_are_ordered_best_first() -> None:
    chunks = [
        _chunk(0, "unrelated", (0.0, 1.0)),
        _chunk(1, "exact", (1.0, 0.0)),
        _chunk(2, "partial", (0.7071, 0.7071)),
    ]
    embedder = ScriptedEmbedder({"q": (1.0, 0.0)})

    found = search_chunks("q", chunks, embedder)

    assert [m.chunk.text for m in found.matches] == ["exact", "partial", "unrelated"]
    assert found.best_score == pytest.approx(1.0)


def test_limit_caps_the_number_of_matches() -> None:
    chunks = [_chunk(i, f"c{i}", (1.0, 0.0)) for i in range(10)]

    found = search_chunks("q", chunks, ScriptedEmbedder({"q": (1.0, 0.0)}), limit=3)

    assert len(found.matches) == 3


def test_min_score_discards_weak_matches() -> None:
    chunks = [
        _chunk(0, "strong", (1.0, 0.0)),
        _chunk(1, "weak", (0.1, 0.995)),
    ]

    found = search_chunks(
        "q", chunks, ScriptedEmbedder({"q": (1.0, 0.0)}), min_score=0.5
    )

    assert [m.chunk.text for m in found.matches] == ["strong"]


def test_chunks_without_an_embedding_are_skipped_not_scored_as_zero() -> None:
    # An un-embedded chunk means embedding *failed* for it. Scoring it as 0 would
    # quietly present that failure as "irrelevant".
    chunks = [_chunk(0, "no embedding", ()), _chunk(1, "embedded", (1.0, 0.0))]

    found = search_chunks("q", chunks, ScriptedEmbedder({"q": (1.0, 0.0)}))

    assert [m.chunk.text for m in found.matches] == ["embedded"]


def test_a_corpus_with_no_embeddings_returns_nothing_without_calling_the_model() -> None:
    embedder = ScriptedEmbedder({})

    found = search_chunks("q", [_chunk(0, "text", ())], embedder)

    assert found.matches == []
    # No embedding call: paying Bedrock to embed a question with nothing to
    # compare it against is waste on every unanswerable turn.
    assert embedder.calls == []


@pytest.mark.parametrize("query", ["", "   ", "\n"])
def test_a_blank_query_returns_nothing(query: str) -> None:
    embedder = ScriptedEmbedder({})

    found = search_chunks(query, [_chunk(0, "t", (1.0, 0.0))], embedder)

    assert found.matches == []
    assert embedder.calls == []


def test_an_empty_corpus_returns_nothing() -> None:
    found = search_chunks("q", [], ScriptedEmbedder({}))

    assert found.matches == []
    assert found.best_score == 0.0


def test_ties_break_deterministically_by_document_then_index() -> None:
    # Identical passages must not reorder between calls just because the store
    # iterated differently.
    chunks = [
        _chunk(2, "b2", (1.0, 0.0), document_id="b"),
        _chunk(1, "a1", (1.0, 0.0), document_id="a"),
        _chunk(0, "b0", (1.0, 0.0), document_id="b"),
    ]
    embedder = ScriptedEmbedder({"q": (1.0, 0.0)})

    first = [m.chunk.text for m in search_chunks("q", chunks, embedder).matches]
    second = [
        m.chunk.text
        for m in search_chunks("q", list(reversed(chunks)), embedder).matches
    ]

    assert first == ["a1", "b0", "b2"]
    assert first == second


def test_the_query_is_embedded_once_regardless_of_corpus_size() -> None:
    chunks = [_chunk(i, f"c{i}", (1.0, 0.0)) for i in range(50)]
    embedder = ScriptedEmbedder({"q": (1.0, 0.0)})

    search_chunks("q", chunks, embedder)

    assert embedder.calls == ["q"]


def test_the_search_result_carries_the_query_back() -> None:
    found = search_chunks("where is parking", [], ScriptedEmbedder({}))

    assert found.query == "where is parking"


def test_matches_carry_the_page_for_citation() -> None:
    chunks = [_chunk(0, "on page 3", (1.0, 0.0), page=3)]

    found = search_chunks("q", chunks, ScriptedEmbedder({"q": (1.0, 0.0)}))

    assert found.matches[0].chunk.page == 3


# ---------------------------------------------------------------------------
# BedrockEmbedder: the request it actually sends
# ---------------------------------------------------------------------------


class _FakeBody:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


class RecordingBedrockClient:
    """Captures invoke_model calls and returns a canned embedding."""

    def __init__(self, dimensions: int = 3) -> None:
        self.calls: list[dict[str, Any]] = []
        self.dimensions = dimensions

    def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"body": _FakeBody({"embedding": [0.1] * self.dimensions})}


def test_bedrock_embedder_requests_normalized_vectors() -> None:
    client = RecordingBedrockClient()

    BedrockEmbedder(client).embed("some passage")

    body = json.loads(client.calls[0]["body"])
    # Unit-length vectors are what make cosine a dot product and let the relevance
    # floor mean the same thing for every passage.
    assert body["normalize"] is True
    assert body["dimensions"] == DEFAULT_DIMENSIONS
    assert body["inputText"] == "some passage"


def test_bedrock_embedder_uses_the_configured_model_and_dimensions() -> None:
    client = RecordingBedrockClient()

    BedrockEmbedder(client, model_id="custom-model", dimensions=256).embed("x")

    assert client.calls[0]["modelId"] == "custom-model"
    assert json.loads(client.calls[0]["body"])["dimensions"] == 256


def test_bedrock_embedder_truncates_an_over_long_passage() -> None:
    client = RecordingBedrockClient()

    BedrockEmbedder(client).embed("x" * (MAX_EMBED_CHARS + 500))

    # Truncating beats a runtime error part-way through an upload.
    assert len(json.loads(client.calls[0]["body"])["inputText"]) == MAX_EMBED_CHARS


def test_bedrock_embedder_returns_a_tuple_of_floats() -> None:
    result = BedrockEmbedder(RecordingBedrockClient()).embed("x")

    assert isinstance(result, tuple)
    assert result == pytest.approx((0.1, 0.1, 0.1))


def test_embed_all_preserves_order_and_calls_once_per_passage() -> None:
    client = RecordingBedrockClient()

    vectors = BedrockEmbedder(client).embed_all(["one", "two", "three"])

    assert len(vectors) == 3
    assert [json.loads(c["body"])["inputText"] for c in client.calls] == [
        "one",
        "two",
        "three",
    ]


def test_embed_all_of_nothing_calls_nothing() -> None:
    client = RecordingBedrockClient()

    assert BedrockEmbedder(client).embed_all([]) == []
    assert client.calls == []
