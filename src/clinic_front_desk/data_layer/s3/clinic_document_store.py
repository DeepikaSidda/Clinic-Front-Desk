"""Amazon S3 :class:`ClinicDocumentStore` (Req 16.2, 16.5, 16.6).

Layout, per document, under one prefix:

    <prefix>/<document_id>/original.<ext>   the file the doctor uploaded
    <prefix>/<document_id>/document.json    metadata + chunks + embeddings

Two objects rather than one because the access patterns differ sharply: the
metadata/chunks JSON is read on **every** retrieval, while the original is only
read when the doctor downloads it. Keeping the PDF out of that hot path means a
lookup fetches kilobytes of JSON instead of megabytes of PDF.

Embeddings are stored inside the JSON as plain float lists. At a few hundred chunks
per clinic that is a small object, and it means no vector database and no separate
index to keep in sync — the embeddings cannot drift from the text they describe
because they are written in the same object, in the same request.

Encryption is requested on every write. Clinic documents are less sensitive than
call audio but can still name staff and describe premises, so they get the same
treatment.
"""

from __future__ import annotations

import json
import posixpath
from typing import Any

from clinic_front_desk.data_layer.interfaces import ClinicDocumentStore
from clinic_front_desk.models import (
    ClinicDocument,
    DocumentChunk,
    Err,
    Ok,
    StoreError,
    StoreErrorKind,
    StoreResult,
)

_STORE = "S3ClinicDocumentStore"

#: Default key prefix inside the bucket.
DEFAULT_PREFIX = "clinic-documents"

#: Name of the per-document metadata object.
MANIFEST_NAME = "document.json"


def _extension(filename: str, content_type: str) -> str:
    lowered = (filename or "").lower()
    for suffix in (".pdf", ".txt", ".md", ".markdown"):
        if lowered.endswith(suffix):
            return suffix
    if "pdf" in (content_type or ""):
        return ".pdf"
    return ".bin"


class S3ClinicDocumentStore(ClinicDocumentStore):
    """Stores uploaded clinic documents and their chunks as S3 objects.

    Args:
        bucket: The bucket documents are written to. May be the same bucket used
            for recordings — the prefixes are distinct, and so are their lifecycle
            rules (recordings expire; documents should not).
        client: A boto3 S3 client, injected so this class performs no AWS
            construction and tests can pass a moto-mocked client.
        prefix: Key prefix inside the bucket.
        sse: Server-side encryption mode, or ``None`` to rely on bucket defaults.
        kms_key_id: Required when ``sse`` is ``"aws:kms"``.
    """

    def __init__(
        self,
        bucket: str,
        client: Any,
        *,
        prefix: str = DEFAULT_PREFIX,
        sse: str | None = "AES256",
        kms_key_id: str | None = None,
    ) -> None:
        self._bucket = bucket
        self._client = client
        self._prefix = prefix.strip("/")
        self._sse = sse
        self._kms_key_id = kms_key_id

    # -- keys ---------------------------------------------------------------

    def _manifest_key(self, document_id: str) -> str:
        return posixpath.join(self._prefix, document_id, MANIFEST_NAME)

    def _document_prefix(self, document_id: str) -> str:
        return posixpath.join(self._prefix, document_id) + "/"

    def uri_for(self, document_id: str) -> str:
        """The ``s3://`` URI of a document's folder."""
        return f"s3://{self._bucket}/{self._document_prefix(document_id)}"

    def _encryption_args(self) -> dict[str, Any]:
        args: dict[str, Any] = {}
        if self._sse:
            args["ServerSideEncryption"] = self._sse
            if self._sse == "aws:kms" and self._kms_key_id:
                args["SSEKMSKeyId"] = self._kms_key_id
        return args

    # -- serialization ------------------------------------------------------

    @staticmethod
    def _to_manifest(
        document: ClinicDocument, chunks: list[DocumentChunk], original_key: str
    ) -> dict[str, Any]:
        return {
            "id": document.id,
            "filename": document.filename,
            "content_type": document.content_type,
            "uploaded_at": document.uploaded_at,
            "byte_size": document.byte_size,
            "page_count": document.page_count,
            "extraction_error": document.extraction_error,
            "original_key": original_key,
            "chunks": [
                {
                    "index": chunk.index,
                    "text": chunk.text,
                    "page": chunk.page,
                    "embedding": list(chunk.embedding),
                }
                for chunk in chunks
            ],
        }

    def _document_from_manifest(self, manifest: dict[str, Any]) -> ClinicDocument:
        return ClinicDocument(
            id=manifest["id"],
            filename=manifest.get("filename", ""),
            content_type=manifest.get("content_type", ""),
            uploaded_at=manifest.get("uploaded_at", ""),
            byte_size=int(manifest.get("byte_size") or 0),
            chunk_count=len(manifest.get("chunks") or []),
            page_count=manifest.get("page_count"),
            uri=self.uri_for(manifest["id"]),
            extraction_error=manifest.get("extraction_error"),
        )

    @staticmethod
    def _chunks_from_manifest(manifest: dict[str, Any]) -> list[DocumentChunk]:
        document_id = manifest["id"]
        return [
            DocumentChunk(
                document_id=document_id,
                index=int(raw["index"]),
                text=raw["text"],
                page=raw.get("page"),
                embedding=tuple(float(value) for value in (raw.get("embedding") or ())),
            )
            for raw in manifest.get("chunks") or []
        ]

    # -- reads --------------------------------------------------------------

    def _list_manifest_keys(self) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "Bucket": self._bucket,
                "Prefix": self._prefix + "/",
            }
            if token:
                kwargs["ContinuationToken"] = token
            response = self._client.list_objects_v2(**kwargs)
            for item in response.get("Contents", []) or []:
                key = str(item.get("Key", ""))
                if key.endswith("/" + MANIFEST_NAME):
                    keys.append(key)
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
        return keys

    def _read_manifest(self, key: str) -> dict[str, Any]:
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        parsed: dict[str, Any] = json.loads(response["Body"].read())
        return parsed

    def _all_manifests(self) -> list[dict[str, Any]]:
        manifests = [self._read_manifest(key) for key in self._list_manifest_keys()]
        # Most recently uploaded first, matching the in-memory store.
        manifests.sort(key=lambda m: str(m.get("uploaded_at") or ""), reverse=True)
        return manifests

    # -- ClinicDocumentStore ------------------------------------------------

    def put(
        self,
        document: ClinicDocument,
        *,
        original: bytes,
        chunks: list[DocumentChunk],
    ) -> StoreResult[ClinicDocument]:
        original_key = posixpath.join(
            self._prefix,
            document.id,
            "original" + _extension(document.filename, document.content_type),
        )
        manifest = self._to_manifest(document, chunks, original_key)
        try:
            # Original first, so a manifest never references an object that is not
            # there yet. A stray original without a manifest is invisible to reads.
            self._client.put_object(
                Bucket=self._bucket,
                Key=original_key,
                Body=original,
                ContentType=document.content_type or "application/octet-stream",
                **self._encryption_args(),
            )
            self._client.put_object(
                Bucket=self._bucket,
                Key=self._manifest_key(document.id),
                Body=json.dumps(manifest).encode("utf-8"),
                ContentType="application/json",
                **self._encryption_args(),
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a store failure
            return Err(
                StoreError(
                    kind=StoreErrorKind.STORE_FAILURE,
                    detail=f"failed to store document {document.id!r}: {exc}",
                    store=_STORE,
                )
            )
        return Ok(self._document_from_manifest(manifest))

    def list_documents(self) -> StoreResult[list[ClinicDocument]]:
        try:
            manifests = self._all_manifests()
        except Exception as exc:  # noqa: BLE001
            return Err(
                StoreError(
                    kind=StoreErrorKind.STORE_FAILURE,
                    detail=f"failed to list documents: {exc}",
                    store=_STORE,
                )
            )
        return Ok([self._document_from_manifest(m) for m in manifests])

    def list_chunks(self) -> StoreResult[list[DocumentChunk]]:
        try:
            manifests = self._all_manifests()
        except Exception as exc:  # noqa: BLE001
            return Err(
                StoreError(
                    kind=StoreErrorKind.STORE_FAILURE,
                    detail=f"failed to read document chunks: {exc}",
                    store=_STORE,
                )
            )
        chunks: list[DocumentChunk] = []
        for manifest in manifests:
            chunks.extend(self._chunks_from_manifest(manifest))
        return Ok(chunks)

    def get_original(self, document_id: str) -> StoreResult[bytes | None]:
        try:
            manifest = self._read_manifest(self._manifest_key(document_id))
        except Exception:
            # No manifest means no such document, which is Ok(None) rather than an
            # error (Req 16.4).
            return Ok(None)
        try:
            response = self._client.get_object(
                Bucket=self._bucket, Key=manifest["original_key"]
            )
            body: bytes = response["Body"].read()
        except Exception as exc:  # noqa: BLE001
            return Err(
                StoreError(
                    kind=StoreErrorKind.STORE_FAILURE,
                    detail=f"failed to read the original of {document_id!r}: {exc}",
                    store=_STORE,
                )
            )
        return Ok(body)

    def delete(self, document_id: str) -> StoreResult[None]:
        try:
            response = self._client.list_objects_v2(
                Bucket=self._bucket, Prefix=self._document_prefix(document_id)
            )
            keys = [
                {"Key": str(item["Key"])} for item in response.get("Contents", []) or []
            ]
            for key in keys:
                self._client.delete_object(Bucket=self._bucket, Key=key["Key"])
        except Exception as exc:  # noqa: BLE001
            return Err(
                StoreError(
                    kind=StoreErrorKind.STORE_FAILURE,
                    detail=f"failed to delete document {document_id!r}: {exc}",
                    store=_STORE,
                )
            )
        return Ok(None)


__all__ = ["S3ClinicDocumentStore", "DEFAULT_PREFIX", "MANIFEST_NAME"]
