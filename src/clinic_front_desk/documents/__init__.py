"""Document-backed clinic knowledge: uploads the doctor makes in the portal.

The Clinic_Knowledge_Base has two halves, and they are stored and used differently
on purpose.

**Structured** — hours, offered services, prices, providers, location — lives in
:class:`~clinic_front_desk.models.ClinicKnowledgeBase` and is entered/confirmed in
the onboarding wizard. Those values *drive behaviour*: the offered-service list is
what ``match_offered_service`` compares against by exact name, which is the same
check that stops the agent inferring a service from a symptom, and prices are money.
They must not be inferred from a document at answer time.

**Descriptive** — detailed directions, parking, which floor, holiday closures,
accessibility, cancellation policy — has no field in that schema, and the six fixed
FAQ topics cannot answer it. This package covers that half: the doctor uploads what
they already have, and the text becomes a retrieval corpus the FAQ tool falls back
to.

Modules:

- :mod:`~clinic_front_desk.documents.text` — read PDFs and text files into
  page-tagged passages. Pure; no AWS.
- :mod:`~clinic_front_desk.documents.embeddings` — embed passages with Amazon Titan
  and rank them by cosine similarity, in process. See that module for why there is
  no vector database.
- :mod:`~clinic_front_desk.documents.ingest` — the upload → extract → chunk → embed
  → store path, in one place.
"""

from __future__ import annotations

from .embeddings import (
    DEFAULT_DIMENSIONS,
    DEFAULT_EMBEDDING_MODEL_ID,
    BedrockEmbedder,
    Embedder,
    SearchResult,
    cosine_similarity,
    create_embedder,
    search_chunks,
)
from .extraction import (
    DEFAULT_EXTRACTION_MODEL_ID,
    BedrockConfigExtractor,
    ConfigExtractor,
    ExtractedConfig,
    create_config_extractor,
    extract_clinic_config,
    normalize_time,
)
from .ingest import IngestResult, ingest_document
from .retrieval import (
    MAX_SPOKEN_CHARS,
    MIN_RELEVANCE,
    SEARCH_LIMIT,
    DocumentKnowledge,
    looks_clinical,
)
from .text import (
    DEFAULT_CHUNK_CHARS,
    DEFAULT_OVERLAP_CHARS,
    MAX_UPLOAD_BYTES,
    DocumentExtractionError,
    ExtractedPage,
    ExtractedText,
    chunk_text,
    extract_text,
)

__all__ = [
    # text extraction + chunking
    "extract_text",
    "chunk_text",
    "ExtractedText",
    "ExtractedPage",
    "DocumentExtractionError",
    "DEFAULT_CHUNK_CHARS",
    "DEFAULT_OVERLAP_CHARS",
    "MAX_UPLOAD_BYTES",
    # embeddings + retrieval
    "Embedder",
    "BedrockEmbedder",
    "create_embedder",
    "search_chunks",
    "cosine_similarity",
    "SearchResult",
    "DEFAULT_EMBEDDING_MODEL_ID",
    "DEFAULT_DIMENSIONS",
    # ingestion
    "ingest_document",
    "IngestResult",
    # structured extraction for the onboarding wizard
    "extract_clinic_config",
    "ExtractedConfig",
    "ConfigExtractor",
    "BedrockConfigExtractor",
    "create_config_extractor",
    "normalize_time",
    "DEFAULT_EXTRACTION_MODEL_ID",
    # answering from the corpus
    "DocumentKnowledge",
    "looks_clinical",
    "MIN_RELEVANCE",
    "SEARCH_LIMIT",
    "MAX_SPOKEN_CHARS",
]
