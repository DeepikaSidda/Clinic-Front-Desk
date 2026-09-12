"""Turn an upload into a stored, retrievable clinic document.

One function, :func:`ingest_document`, owns the whole path: read the file, split it
into passages, embed them, and persist. It is the only place that sequence lives, so
the portal route and any batch/CLI path cannot drift apart.

Failure handling is deliberate. Three things can go wrong and they are not the same:

- **The file is unreadable** (a scan, a password, an unsupported type). The doctor
  needs to know now, so this returns a failed result carrying the reason for display.
  Nothing is stored: a document that contributes no text would sit in the portal
  looking ingested while answering nothing.
- **Embedding fails** for some chunks. The document is still stored with the chunks
  that did embed, because a partly-searchable document beats none — but the count is
  reported so the doctor can re-upload.
- **The store write fails.** Surfaced as-is; nothing is half-written.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from clinic_front_desk.data_layer.interfaces import ClinicDocumentStore
from clinic_front_desk.models import (
    ClinicDocument,
    DocumentChunk,
    StoreError,
    is_err,
)

from .embeddings import Embedder
from .text import DocumentExtractionError, chunk_text, extract_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestResult:
    """The outcome of ingesting one upload.

    Attributes:
        ok: Whether the document was stored and is now answerable from.
        document: The stored document, when successful.
        error: A message written for the doctor, not a stack trace.
        chunks_stored: Passages persisted.
        chunks_embedded: Passages that got an embedding. Fewer than
            ``chunks_stored`` means some embedding calls failed and those passages
            are stored but not retrievable.
        store_error: The underlying Data_Layer failure, when that was the cause.
    """

    ok: bool
    document: ClinicDocument | None = None
    error: str | None = None
    chunks_stored: int = 0
    chunks_embedded: int = 0
    store_error: StoreError | None = None

    @property
    def partially_embedded(self) -> bool:
        """True when the document stored but some passages are not searchable."""
        return self.ok and self.chunks_embedded < self.chunks_stored


def ingest_document(
    store: ClinicDocumentStore,
    data: bytes,
    *,
    filename: str,
    content_type: str = "",
    embedder: Embedder | None = None,
    document_id: str | None = None,
    uploaded_at: str | None = None,
) -> IngestResult:
    """Extract, chunk, embed, and store an uploaded clinic document.

    Args:
        store: Where the document and its chunks are persisted.
        data: The uploaded bytes.
        filename: Original filename, used to pick the reader and shown in the portal.
        content_type: Browser-reported type; only a hint, since browsers disagree.
        embedder: Used to embed the chunks. ``None`` stores the text without
            embeddings, which makes the document visible in the portal but *not*
            retrievable — used by tests and by a deployment with no Bedrock access.
        document_id: Explicit id, for a deterministic re-upload.
        uploaded_at: Explicit timestamp; defaults to now.

    Returns:
        An :class:`IngestResult`. Never raises for a bad upload — the reason is
        returned for display.
    """
    doc_id = document_id or uuid4().hex
    stamp = uploaded_at or datetime.now(UTC).isoformat()

    try:
        extracted = extract_text(data, filename=filename, content_type=content_type)
    except DocumentExtractionError as exc:
        # Nothing is stored: an unreadable upload that appeared in the portal would
        # look ingested while contributing no answers.
        return IngestResult(ok=False, error=str(exc))

    passages = chunk_text(extracted)
    if not passages:
        return IngestResult(
            ok=False,
            error="the file was read but contained no usable text passages",
        )

    embeddings: list[tuple[float, ...]] = [() for _ in passages]
    if embedder is not None:
        try:
            embeddings = list(embedder.embed_all([text for text, _ in passages]))
        except Exception as exc:  # noqa: BLE001 - degrade rather than lose the upload
            logger.warning("embedding failed for document %s: %s", doc_id, exc)
            embeddings = [() for _ in passages]

    chunks = [
        DocumentChunk(
            document_id=doc_id,
            index=index,
            text=text,
            page=page,
            embedding=embeddings[index] if index < len(embeddings) else (),
        )
        for index, (text, page) in enumerate(passages)
    ]

    document = ClinicDocument(
        id=doc_id,
        filename=filename,
        content_type=content_type,
        uploaded_at=stamp,
        byte_size=len(data),
        chunk_count=len(chunks),
        page_count=extracted.page_count,
    )

    result = store.put(document, original=data, chunks=chunks)
    if is_err(result):
        return IngestResult(
            ok=False,
            error=f"the document could not be saved: {result.error.detail}",
            store_error=result.error,
        )

    embedded = sum(1 for chunk in chunks if chunk.embedding)
    if embedded < len(chunks):
        logger.warning(
            "document %s stored with %d/%d passages embedded", doc_id, embedded, len(chunks)
        )
    return IngestResult(
        ok=True,
        document=result.value,
        chunks_stored=len(chunks),
        chunks_embedded=embedded,
    )


__all__ = ["IngestResult", "ingest_document"]
