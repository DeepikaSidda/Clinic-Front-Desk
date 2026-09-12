"""``ClinicDocumentStore`` — uploaded clinic-document data access (Req 16.1, 16.5).

The *descriptive* half of the Clinic_Knowledge_Base. The structured half —
hours, offered services, prices, providers — stays in
:class:`~clinic_front_desk.data_layer.interfaces.ClinicKnowledgeBaseStore`, because
those values drive behaviour: the offered-service list is what
``match_offered_service`` compares against by exact name, and prices are money.
Documents cover what that schema has no field for: detailed directions, parking,
which floor, holiday closures, accessibility, policies.

Like call recordings, documents do not fit the DynamoDB single table (a PDF is
megabytes against a 400 KB item limit), so the interface is backed by object
storage. Unlike recordings, the derived **chunks** are what gets read on every
lookup, so they are stored separately from the original file and can be fetched
without pulling the PDF back.

Contract:
    - **Replace-by-id.** ``put`` for an existing document id overwrites, so a
      re-upload leaves one copy rather than two.
    - **Atomicity (Req 16.6).** A failed ``put`` returns an ``Err`` and stores
      nothing; the doctor is told the upload failed rather than the agent quietly
      answering from a half-written document.
    - **Empty initialization (Req 16.4).** Before any upload, ``list_documents``
      returns ``Ok([])`` and ``list_chunks`` returns ``Ok([])`` — never an error,
      because "no documents uploaded yet" is the normal starting state.
    - **Chunks are read whole.** ``list_chunks`` returns every chunk across every
      document. A single clinic's corpus is a few hundred passages, so retrieval
      scores them in process; this interface deliberately does not pretend to be a
      vector database.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from clinic_front_desk.models import ClinicDocument, DocumentChunk, StoreResult


class ClinicDocumentStore(ABC):
    """Read/write interface for uploaded clinic documents and their chunks."""

    @abstractmethod
    def put(
        self,
        document: ClinicDocument,
        *,
        original: bytes,
        chunks: list[DocumentChunk],
    ) -> StoreResult[ClinicDocument]:
        """Store a document, its original bytes, and its retrievable chunks.

        Args:
            document: Metadata for the upload. ``uri`` is filled in by the store.
            original: The uploaded file as received, kept so the doctor can
                download exactly what they provided and so chunking can be redone
                if the strategy changes.
            chunks: The passages retrieval will score, with embeddings already
                attached.

        Returns:
            ``Ok(ClinicDocument)`` with ``uri`` and ``chunk_count`` set, or
            ``Err(StoreError)`` having stored nothing.
        """
        raise NotImplementedError

    @abstractmethod
    def list_documents(self) -> StoreResult[list[ClinicDocument]]:
        """Return the uploaded documents, most recently uploaded first."""
        raise NotImplementedError

    @abstractmethod
    def list_chunks(self) -> StoreResult[list[DocumentChunk]]:
        """Return every chunk across every document, for retrieval to score."""
        raise NotImplementedError

    @abstractmethod
    def get_original(self, document_id: str) -> StoreResult[bytes | None]:
        """Return the original uploaded bytes, or ``Ok(None)`` if unknown."""
        raise NotImplementedError

    @abstractmethod
    def delete(self, document_id: str) -> StoreResult[None]:
        """Remove a document, its original, and its chunks.

        Deleting must remove the chunks too: a document the doctor has withdrawn
        must stop being a source the agent answers from.
        """
        raise NotImplementedError


__all__ = ["ClinicDocumentStore"]
