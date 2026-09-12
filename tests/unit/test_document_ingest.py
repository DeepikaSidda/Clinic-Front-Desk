"""Unit tests for the upload → store path (``documents/ingest.py``)."""

from __future__ import annotations

from typing import Any

import pytest

from clinic_front_desk.data_layer.faults import fail_on, wrap
from clinic_front_desk.data_layer.memory import MemoryClinicDocumentStore
from clinic_front_desk.documents.ingest import ingest_document
from clinic_front_desk.models import is_ok

SHEET = b"""Springfield Hearing Clinic

Parking
Patient parking is free in the surface lot behind the building, off Elm Street.

Holiday closures
We are closed on New Year's Day and Thanksgiving, and from December 24th
through January 1st inclusive.
"""


class CountingEmbedder:
    """Embeds everything with a fixed vector and counts the passages seen."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def embed(self, text: str) -> tuple[float, ...]:
        self.seen.append(text)
        return (1.0, 0.0, 0.0)

    def embed_all(self, texts: Any) -> list[tuple[float, ...]]:
        return [self.embed(t) for t in texts]


class BrokenEmbedder:
    def embed(self, text: str) -> tuple[float, ...]:
        raise RuntimeError("bedrock is unavailable")

    def embed_all(self, texts: Any) -> list[tuple[float, ...]]:
        raise RuntimeError("bedrock is unavailable")


@pytest.fixture
def store() -> MemoryClinicDocumentStore:
    return MemoryClinicDocumentStore()


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_a_readable_upload_is_chunked_embedded_and_stored(
    store: MemoryClinicDocumentStore,
) -> None:
    embedder = CountingEmbedder()

    result = ingest_document(
        store,
        SHEET,
        filename="info.txt",
        content_type="text/plain",
        embedder=embedder,
    )

    assert result.ok
    assert result.document is not None
    assert result.document.filename == "info.txt"
    assert result.document.byte_size == len(SHEET)
    assert result.chunks_stored > 0
    assert result.chunks_embedded == result.chunks_stored
    assert not result.partially_embedded
    assert len(embedder.seen) == result.chunks_stored

    stored = store.list_documents()
    assert is_ok(stored)
    assert [d.filename for d in stored.value] == ["info.txt"]


def test_stored_chunks_carry_their_embeddings(
    store: MemoryClinicDocumentStore,
) -> None:
    ingest_document(store, SHEET, filename="info.txt", embedder=CountingEmbedder())

    chunks = store.list_chunks()

    assert is_ok(chunks)
    assert chunks.value
    assert all(chunk.embedding for chunk in chunks.value)


def test_an_explicit_id_and_timestamp_are_honoured(
    store: MemoryClinicDocumentStore,
) -> None:
    result = ingest_document(
        store,
        SHEET,
        filename="info.txt",
        document_id="fixed-id",
        uploaded_at="2026-03-04T05:06:07+00:00",
        embedder=CountingEmbedder(),
    )

    assert result.document is not None
    assert result.document.id == "fixed-id"
    assert result.document.uploaded_at == "2026-03-04T05:06:07+00:00"


def test_a_generated_id_is_assigned_when_none_is_given(
    store: MemoryClinicDocumentStore,
) -> None:
    first = ingest_document(store, SHEET, filename="a.txt")
    second = ingest_document(store, SHEET, filename="b.txt")

    assert first.document is not None and second.document is not None
    assert first.document.id != second.document.id


def test_re_ingesting_the_same_id_replaces_the_document(
    store: MemoryClinicDocumentStore,
) -> None:
    ingest_document(store, SHEET, filename="info.txt", document_id="same")
    ingest_document(store, b"Parking\nFree in the lot behind the building always.",
                    filename="info.txt", document_id="same")

    documents = store.list_documents()

    assert len(documents.value) == 1


# ---------------------------------------------------------------------------
# Unreadable uploads store nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "filename"),
    [
        (b"", "empty.txt"),
        (b"   \n  ", "blank.txt"),
        (b"%PDF-1.4 not actually a pdf", "broken.pdf"),
        (b"\x89PNG\r\n\x1a\n", "photo.png"),
    ],
)
def test_an_unreadable_upload_stores_nothing_and_explains_why(
    store: MemoryClinicDocumentStore, data: bytes, filename: str
) -> None:
    result = ingest_document(store, data, filename=filename)

    assert not result.ok
    assert result.error
    assert result.document is None
    # Nothing stored: a document that contributes no text would sit in the portal
    # looking ingested while answering nothing.
    assert store.list_documents().value == []
    assert store.list_chunks().value == []


def test_the_failure_message_is_written_for_a_person(
    store: MemoryClinicDocumentStore,
) -> None:
    result = ingest_document(store, b"%PDF-1.4 broken", filename="scan.pdf")

    assert result.error is not None
    assert "Traceback" not in result.error
    assert result.error[0].islower() or result.error[0].isalpha()


def test_text_too_short_to_chunk_is_reported_rather_than_stored_empty(
    store: MemoryClinicDocumentStore,
) -> None:
    # Readable, but yields no passage long enough to answer anything.
    result = ingest_document(store, b"Hi.", filename="tiny.txt")

    assert not result.ok
    assert result.error is not None
    assert store.list_documents().value == []


# ---------------------------------------------------------------------------
# Degraded paths
# ---------------------------------------------------------------------------


def test_no_embedder_still_stores_the_document_but_nothing_is_searchable(
    store: MemoryClinicDocumentStore,
) -> None:
    result = ingest_document(store, SHEET, filename="info.txt", embedder=None)

    assert result.ok
    assert result.chunks_stored > 0
    assert result.chunks_embedded == 0
    assert result.partially_embedded
    assert all(chunk.embedding == () for chunk in store.list_chunks().value)


def test_a_failing_embedder_still_stores_the_text(
    store: MemoryClinicDocumentStore,
) -> None:
    # A partly-usable upload beats losing it: the doctor keeps the file, can
    # download it, and can re-upload to fix retrieval.
    result = ingest_document(
        store, SHEET, filename="info.txt", embedder=BrokenEmbedder()
    )

    assert result.ok
    assert result.chunks_stored > 0
    assert result.chunks_embedded == 0
    assert result.partially_embedded


def test_a_store_failure_is_surfaced_and_nothing_is_reported_as_stored(
    store: MemoryClinicDocumentStore,
) -> None:
    faulty = wrap(store, fail_on("put"))

    result = ingest_document(faulty, SHEET, filename="info.txt")

    assert not result.ok
    assert result.store_error is not None
    assert result.error is not None
    assert "saved" in result.error
    assert result.chunks_stored == 0


def test_partially_embedded_is_false_for_a_failed_ingest(
    store: MemoryClinicDocumentStore,
) -> None:
    # The flag means "stored but incomplete", so it must not fire for something
    # that was never stored.
    result = ingest_document(store, b"", filename="empty.txt")

    assert not result.ok
    assert not result.partially_embedded
