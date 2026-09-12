"""Chunk embedding and similarity search over a clinic's documents.

Why no vector database
----------------------
A single clinic's descriptive corpus is a practice information sheet and maybe a
holiday list: a few hundred chunks at most. Scoring a few hundred 512-dimension
dot products takes well under a millisecond in plain Python. A managed vector store
would add a service to operate, an index that can drift out of sync with the text,
and — for OpenSearch Serverless — a monthly floor larger than everything else this
system costs put together.

So embeddings are computed once at upload, stored beside the text they describe, and
searched in process. If this ever became multi-tenant with thousands of documents,
that decision should be revisited; it is written down here so the tradeoff is
visible rather than looking like an oversight.

Embeddings are **normalized to unit length** at generation (Titan's ``normalize``
option), which makes cosine similarity a plain dot product and puts scores on a
comparable 0–1 scale. That matters because the relevance floor is expressed as an
absolute threshold, and a threshold is meaningless if score magnitudes drift with
passage length.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from clinic_front_desk.models import DocumentChunk, RetrievedChunk

#: Amazon Titan Text Embeddings V2.
DEFAULT_EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"

#: Output dimensions. Titan V2 supports 256/512/1024; 512 is the middle option and
#: is ample for a corpus this size, at half the storage of 1024.
DEFAULT_DIMENSIONS = 512

#: Titan's input limit is generous, but a chunk far past this is a chunking bug;
#: truncating is safer than a runtime error mid-upload.
MAX_EMBED_CHARS = 8000


class Embedder(Protocol):
    """Turns text into a vector. Narrow on purpose, so tests can fake it."""

    def embed(self, text: str) -> tuple[float, ...]:
        """Return the embedding of a single passage."""
        ...

    def embed_all(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """Return embeddings for many passages, in order."""
        ...


class BedrockEmbedder:
    """Embeds text with Amazon Titan on Bedrock.

    Args:
        client: A ``bedrock-runtime`` client. Injected so no AWS construction
            happens here and tests never reach the network.
        model_id: Titan embedding model id.
        dimensions: Output dimensionality.
    """

    def __init__(
        self,
        client: Any,
        *,
        model_id: str = DEFAULT_EMBEDDING_MODEL_ID,
        dimensions: int = DEFAULT_DIMENSIONS,
    ) -> None:
        self._client = client
        self._model_id = model_id
        self._dimensions = dimensions

    def embed(self, text: str) -> tuple[float, ...]:
        import json

        payload = {
            "inputText": text[:MAX_EMBED_CHARS],
            "dimensions": self._dimensions,
            # Unit-length vectors, so cosine similarity is a dot product and the
            # relevance floor means the same thing for every passage.
            "normalize": True,
        }
        response = self._client.invoke_model(
            modelId=self._model_id, body=json.dumps(payload)
        )
        body = json.loads(response["body"].read())
        return tuple(float(value) for value in body["embedding"])

    def embed_all(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """Embed passages one call at a time.

        Titan's text-embedding API takes a single input per request, so this is a
        loop by necessity rather than by choice. At a few hundred chunks per upload
        that is seconds, paid once at upload rather than per question.
        """
        return [self.embed(text) for text in texts]


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity of two vectors, or ``0.0`` if either is empty/degenerate.

    Vectors from :class:`BedrockEmbedder` are already unit length, so this reduces
    to a dot product — but the norms are computed anyway so the function stays
    correct for un-normalized input rather than silently returning wrong scores.
    """
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right, strict=True):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return dot / math.sqrt(left_norm * right_norm)


@dataclass(frozen=True)
class SearchResult:
    """Ranked chunks for one question."""

    query: str
    matches: list[RetrievedChunk]

    @property
    def best_score(self) -> float:
        return self.matches[0].score if self.matches else 0.0


def search_chunks(
    query: str,
    chunks: Sequence[DocumentChunk],
    embedder: Embedder,
    *,
    limit: int = 4,
    min_score: float = 0.0,
) -> SearchResult:
    """Rank ``chunks`` against ``query`` by cosine similarity.

    Chunks with no embedding are skipped rather than scored as ``0``: an
    un-embedded chunk means embedding failed for it, and silently treating that as
    "irrelevant" would hide the failure.

    Args:
        query: The caller's question.
        chunks: Every chunk across the clinic's documents.
        embedder: Used to embed the query.
        limit: Maximum matches to return.
        min_score: Discard matches below this similarity.

    Returns:
        A :class:`SearchResult` with matches ordered best-first.
    """
    embeddable = [chunk for chunk in chunks if chunk.embedding]
    if not query.strip() or not embeddable:
        return SearchResult(query=query, matches=[])

    query_vector = embedder.embed(query)
    scored = [
        RetrievedChunk(chunk=chunk, score=cosine_similarity(query_vector, chunk.embedding))
        for chunk in embeddable
    ]
    scored = [match for match in scored if match.score >= min_score]
    # Document id and chunk index break score ties, so ordering is deterministic
    # for identical passages rather than depending on store iteration order.
    scored.sort(key=lambda m: (-m.score, m.chunk.document_id, m.chunk.index))
    return SearchResult(query=query, matches=scored[:limit])


def create_embedder(
    *, region: str | None = None, model_id: str = DEFAULT_EMBEDDING_MODEL_ID
) -> BedrockEmbedder:
    """Build a :class:`BedrockEmbedder`, creating a boto3 client lazily."""
    import boto3  # type: ignore[import-untyped]

    return BedrockEmbedder(
        boto3.client("bedrock-runtime", region_name=region), model_id=model_id
    )


__all__ = [
    "DEFAULT_EMBEDDING_MODEL_ID",
    "DEFAULT_DIMENSIONS",
    "MAX_EMBED_CHARS",
    "Embedder",
    "BedrockEmbedder",
    "SearchResult",
    "cosine_similarity",
    "search_chunks",
    "create_embedder",
]
