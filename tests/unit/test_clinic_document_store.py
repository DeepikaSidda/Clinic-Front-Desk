"""Store-contract tests for both :class:`ClinicDocumentStore` implementations.

Every behavioural test runs against the in-memory fake *and* the S3 store on a
moto-mocked bucket, from one parametrized fixture. That is the point: retrieval and
the portal are written against the interface, so the two backends have to be
observably interchangeable (Req 16.5) — including the parts that are easy to get
wrong in only one of them, like ordering and what a missing document returns.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from clinic_front_desk.data_layer.memory import MemoryClinicDocumentStore
from clinic_front_desk.data_layer.s3 import S3ClinicDocumentStore
from clinic_front_desk.data_layer.s3.clinic_document_store import MANIFEST_NAME
from clinic_front_desk.models import (
    ClinicDocument,
    DocumentChunk,
    is_err,
    is_ok,
)

REGION = "us-east-1"
BUCKET = "clinic-documents-test"
PREFIX = "clinic-documents"


@pytest.fixture
def memory_store() -> MemoryClinicDocumentStore:
    return MemoryClinicDocumentStore()


@pytest.fixture
def s3_client() -> Iterator[Any]:
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture
def s3_store(s3_client: Any) -> S3ClinicDocumentStore:
    return S3ClinicDocumentStore(BUCKET, s3_client, prefix=PREFIX)


@pytest.fixture(params=["memory", "s3"])
def store(request: pytest.FixtureRequest) -> Any:
    """Both backends, so every contract test runs twice."""
    return request.getfixturevalue(f"{request.param}_store")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _document(doc_id: str = "doc1", *, uploaded_at: str = "2026-01-01T09:00:00+00:00") -> ClinicDocument:
    return ClinicDocument(
        id=doc_id,
        filename=f"{doc_id}.pdf",
        content_type="application/pdf",
        uploaded_at=uploaded_at,
        byte_size=11,
        chunk_count=0,
        page_count=2,
    )


def _chunks(doc_id: str = "doc1", count: int = 2) -> list[DocumentChunk]:
    return [
        DocumentChunk(
            document_id=doc_id,
            index=i,
            text=f"passage {i} of {doc_id}",
            page=i + 1,
            embedding=(0.1 * (i + 1), 0.2, 0.3),
        )
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------


def test_a_fresh_store_lists_nothing(store: Any) -> None:
    documents = store.list_documents()
    chunks = store.list_chunks()

    assert is_ok(documents) and documents.value == []
    assert is_ok(chunks) and chunks.value == []


def test_an_unknown_document_has_no_original(store: Any) -> None:
    result = store.get_original("never-uploaded")

    # Absence is Ok(None), not an error (Req 16.4): "not there" is a normal answer.
    assert is_ok(result)
    assert result.value is None


def test_deleting_an_unknown_document_succeeds(store: Any) -> None:
    # Idempotent delete: the portal's delete button must not error because the
    # doctor double-clicked it.
    assert is_ok(store.delete("never-uploaded"))


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_put_round_trips_the_document_and_its_chunks(store: Any) -> None:
    result = store.put(_document(), original=b"hello world", chunks=_chunks())

    assert is_ok(result)
    stored = result.value
    assert stored.id == "doc1"
    assert stored.filename == "doc1.pdf"
    assert stored.page_count == 2
    # chunk_count is derived by the store from what was actually stored, not
    # trusted from the caller's metadata.
    assert stored.chunk_count == 2
    assert stored.uri

    listed = store.list_documents()
    assert is_ok(listed)
    assert [d.id for d in listed.value] == ["doc1"]

    chunks = store.list_chunks()
    assert is_ok(chunks)
    assert [c.text for c in chunks.value] == ["passage 0 of doc1", "passage 1 of doc1"]


def test_the_original_bytes_round_trip_exactly(store: Any) -> None:
    payload = b"%PDF-1.4 \x00\x01\x02 binary \xff\xfe content"
    store.put(_document(), original=payload, chunks=_chunks())

    result = store.get_original("doc1")

    # Byte-exact: the doctor downloads what they uploaded, not a re-encoding.
    assert is_ok(result)
    assert result.value == payload


def test_embeddings_survive_the_round_trip(store: Any) -> None:
    store.put(_document(), original=b"x", chunks=_chunks())

    chunks = store.list_chunks()

    assert is_ok(chunks)
    first = chunks.value[0]
    assert first.embedding == pytest.approx((0.1, 0.2, 0.3))
    # A tuple, not a list: chunks are treated as immutable values downstream.
    assert isinstance(first.embedding, tuple)


def test_page_numbers_survive_the_round_trip(store: Any) -> None:
    store.put(_document(), original=b"x", chunks=_chunks())

    chunks = store.list_chunks()

    assert [c.page for c in chunks.value] == [1, 2]


def test_a_chunk_without_an_embedding_round_trips_as_empty(store: Any) -> None:
    # Partial embedding is a real state: the document stored, some passages did
    # not embed. It must not come back as a zero vector, which would score.
    unembedded = [
        DocumentChunk(document_id="doc1", index=0, text="a passage", page=None)
    ]
    store.put(_document(), original=b"x", chunks=unembedded)

    chunks = store.list_chunks()

    assert is_ok(chunks)
    assert chunks.value[0].embedding == ()


def test_a_pageless_document_round_trips_with_no_page_count(store: Any) -> None:
    document = ClinicDocument(
        id="txt1",
        filename="notes.txt",
        content_type="text/plain",
        uploaded_at="2026-01-01T09:00:00+00:00",
        byte_size=5,
        chunk_count=0,
        page_count=None,
    )
    store.put(document, original=b"hello", chunks=[])

    listed = store.list_documents()

    assert is_ok(listed)
    assert listed.value[0].page_count is None


# ---------------------------------------------------------------------------
# Ordering, replacement, deletion
# ---------------------------------------------------------------------------


def test_documents_are_listed_most_recent_first(store: Any) -> None:
    store.put(
        _document("older", uploaded_at="2026-01-01T09:00:00+00:00"),
        original=b"a",
        chunks=_chunks("older", 1),
    )
    store.put(
        _document("newer", uploaded_at="2026-06-01T09:00:00+00:00"),
        original=b"b",
        chunks=_chunks("newer", 1),
    )

    listed = store.list_documents()

    assert is_ok(listed)
    assert [d.id for d in listed.value] == ["newer", "older"]


def test_re_uploading_the_same_id_replaces_rather_than_duplicates(store: Any) -> None:
    store.put(_document(), original=b"first", chunks=_chunks("doc1", 3))
    store.put(_document(), original=b"second", chunks=_chunks("doc1", 1))

    listed = store.list_documents()
    chunks = store.list_chunks()
    original = store.get_original("doc1")

    assert [d.id for d in listed.value] == ["doc1"]
    assert listed.value[0].chunk_count == 1
    # The stale chunks must be gone, or the agent answers from withdrawn text.
    assert len(chunks.value) == 1
    assert original.value == b"second"


def test_delete_removes_the_document_its_original_and_its_chunks(store: Any) -> None:
    store.put(_document(), original=b"x", chunks=_chunks())

    assert is_ok(store.delete("doc1"))

    assert store.list_documents().value == []
    # The chunks are the part that matters: a withdrawn document must stop being a
    # source the agent answers from.
    assert store.list_chunks().value == []
    assert store.get_original("doc1").value is None


def test_delete_leaves_other_documents_alone(store: Any) -> None:
    store.put(_document("keep"), original=b"a", chunks=_chunks("keep", 1))
    store.put(_document("drop"), original=b"b", chunks=_chunks("drop", 1))

    store.delete("drop")

    assert [d.id for d in store.list_documents().value] == ["keep"]
    assert [c.document_id for c in store.list_chunks().value] == ["keep"]


def test_list_chunks_spans_every_document(store: Any) -> None:
    store.put(_document("a"), original=b"a", chunks=_chunks("a", 2))
    store.put(_document("b"), original=b"b", chunks=_chunks("b", 3))

    chunks = store.list_chunks()

    assert is_ok(chunks)
    assert len(chunks.value) == 5
    assert {c.document_id for c in chunks.value} == {"a", "b"}


# ---------------------------------------------------------------------------
# S3-specific: key layout, encryption, and failure surfacing
# ---------------------------------------------------------------------------


def test_s3_lays_documents_out_one_folder_each(
    s3_store: S3ClinicDocumentStore, s3_client: Any
) -> None:
    s3_store.put(_document(), original=b"hello", chunks=_chunks())

    keys = {
        item["Key"]
        for item in s3_client.list_objects_v2(Bucket=BUCKET)["Contents"]
    }

    assert keys == {
        f"{PREFIX}/doc1/original.pdf",
        f"{PREFIX}/doc1/{MANIFEST_NAME}",
    }


def test_s3_uri_points_at_the_document_folder(s3_store: S3ClinicDocumentStore) -> None:
    result = s3_store.put(_document(), original=b"x", chunks=[])

    assert result.value.uri == f"s3://{BUCKET}/{PREFIX}/doc1/"


def test_s3_extension_follows_the_uploaded_filename(
    s3_store: S3ClinicDocumentStore, s3_client: Any
) -> None:
    document = ClinicDocument(
        id="t1",
        filename="practice-info.txt",
        content_type="text/plain",
        uploaded_at="2026-01-01T00:00:00+00:00",
        byte_size=1,
        chunk_count=0,
    )
    s3_store.put(document, original=b"x", chunks=[])

    keys = {i["Key"] for i in s3_client.list_objects_v2(Bucket=BUCKET)["Contents"]}

    assert f"{PREFIX}/t1/original.txt" in keys


def test_s3_requests_server_side_encryption(
    s3_store: S3ClinicDocumentStore, s3_client: Any
) -> None:
    s3_store.put(_document(), original=b"x", chunks=_chunks())

    head = s3_client.head_object(Bucket=BUCKET, Key=f"{PREFIX}/doc1/{MANIFEST_NAME}")

    # Clinic documents carry practice detail; they are encrypted at rest by
    # request, not merely by whatever the bucket default happens to be.
    assert head["ServerSideEncryption"] == "AES256"


def test_s3_shares_a_bucket_with_recordings_without_colliding(
    s3_client: Any
) -> None:
    # The deployment reuses the recordings bucket by default, so the prefixes have
    # to keep the two apart.
    documents = S3ClinicDocumentStore(BUCKET, s3_client, prefix="clinic-documents")
    s3_client.put_object(
        Bucket=BUCKET, Key="call-recordings/2026-01-01/call-1.wav", Body=b"audio"
    )
    documents.put(_document(), original=b"x", chunks=_chunks())

    listed = documents.list_documents()

    assert is_ok(listed)
    assert [d.id for d in listed.value] == ["doc1"]


def test_s3_reports_a_write_failure_as_a_store_error(s3_client: Any) -> None:
    # A bucket that does not exist stands in for any AWS-side write refusal.
    store = S3ClinicDocumentStore("no-such-bucket", s3_client)

    result = store.put(_document(), original=b"x", chunks=_chunks())

    assert is_err(result)
    assert "doc1" in result.error.detail


def test_s3_reports_a_list_failure_as_a_store_error(s3_client: Any) -> None:
    store = S3ClinicDocumentStore("no-such-bucket", s3_client)

    documents = store.list_documents()
    chunks = store.list_chunks()

    assert is_err(documents)
    assert is_err(chunks)


def test_s3_ignores_unrelated_objects_under_the_prefix(
    s3_store: S3ClinicDocumentStore, s3_client: Any
) -> None:
    # Something else wrote into the prefix. Only manifests define a document, so
    # the listing must not invent one from a stray key.
    s3_client.put_object(Bucket=BUCKET, Key=f"{PREFIX}/stray/notes.txt", Body=b"x")

    listed = s3_store.list_documents()

    assert is_ok(listed)
    assert listed.value == []
