"""In-memory :class:`ClinicDocumentStore` fake (Req 16.2, 16.4, 16.5).

Holds uploads and their chunks in dicts so the whole document path — upload,
extract, chunk, embed, retrieve, answer — runs in tests and local demos with no S3
bucket, observably identically to the real store.
"""

from __future__ import annotations

from clinic_front_desk.data_layer.events import ChangeEmitter
from clinic_front_desk.data_layer.interfaces import ClinicDocumentStore
from clinic_front_desk.models import ClinicDocument, DocumentChunk, Ok, StoreResult

from ._support import MemoryStoreBase


def memory_document_uri(document_id: str) -> str:
    """The pointer stored on an in-memory document."""
    return f"memory://clinic-documents/{document_id}"


class MemoryClinicDocumentStore(ClinicDocumentStore, MemoryStoreBase):
    """A dict-backed :class:`ClinicDocumentStore` honouring the full contract."""

    def __init__(self, emitter: ChangeEmitter | None = None) -> None:
        MemoryStoreBase.__init__(self, emitter)
        self._documents: dict[str, ClinicDocument] = {}
        self._originals: dict[str, bytes] = {}
        self._chunks: dict[str, list[DocumentChunk]] = {}
        self._order: list[str] = []

    def put(
        self,
        document: ClinicDocument,
        *,
        original: bytes,
        chunks: list[DocumentChunk],
    ) -> StoreResult[ClinicDocument]:
        stored = ClinicDocument(
            id=document.id,
            filename=document.filename,
            content_type=document.content_type,
            uploaded_at=document.uploaded_at,
            byte_size=document.byte_size,
            chunk_count=len(chunks),
            page_count=document.page_count,
            uri=memory_document_uri(document.id),
            extraction_error=document.extraction_error,
        )
        # Replace-by-id, so a re-upload leaves one copy.
        self._documents[document.id] = stored
        self._originals[document.id] = bytes(original)
        self._chunks[document.id] = list(chunks)
        if document.id in self._order:
            self._order.remove(document.id)
        self._order.append(document.id)
        return Ok(stored)

    def list_documents(self) -> StoreResult[list[ClinicDocument]]:
        # Most recently uploaded first.
        return Ok([self._documents[doc_id] for doc_id in reversed(self._order)])

    def list_chunks(self) -> StoreResult[list[DocumentChunk]]:
        chunks: list[DocumentChunk] = []
        for doc_id in self._order:
            chunks.extend(self._chunks.get(doc_id, []))
        return Ok(chunks)

    def get_original(self, document_id: str) -> StoreResult[bytes | None]:
        return Ok(self._originals.get(document_id))

    def delete(self, document_id: str) -> StoreResult[None]:
        self._documents.pop(document_id, None)
        self._originals.pop(document_id, None)
        self._chunks.pop(document_id, None)
        if document_id in self._order:
            self._order.remove(document_id)
        return Ok(None)


__all__ = ["MemoryClinicDocumentStore", "memory_document_uri"]
