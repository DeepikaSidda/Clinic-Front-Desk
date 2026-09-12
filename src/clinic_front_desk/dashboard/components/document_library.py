"""The clinic-documents page: what the doctor has uploaded, and what it does.

Renders a full page rather than a dashboard region. Uploading is clinic setup, in
the same family as onboarding, not something to monitor during the day — so it gets
its own address instead of a panel competing with the schedule.

The page is assembled in Python rather than from a template in ``web/``, which is a
departure from the wizard. The reason is that almost all of this page *is* the
document list: a template would be a shell around one substitution token, so the
indirection would buy nothing and split the markup across two files.

Everything here is a plain ``<form>`` — upload, delete, and "use this to fill
onboarding" are all POSTs, with no JavaScript. Two reasons: the destructive action
(delete) then works identically with scripting unavailable, and there is no client
state worth managing. The cost is a full page reload per action, which for a page
touched a handful of times per clinic is not worth a controller script.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from urllib.parse import quote

from clinic_front_desk.data_layer.interfaces import ClinicDocumentStore
from clinic_front_desk.models import ClinicDocument, is_err

#: Where the page and its actions live.
DOCUMENTS_ENDPOINT = "/documents"

#: Upload types offered. Kept in step with what
#: :func:`~clinic_front_desk.documents.text.extract_text` can actually read, so the
#: file picker does not invite a file the server will reject.
ACCEPTED_UPLOAD_TYPES = ".pdf,.txt,.md,.markdown,text/plain,application/pdf"


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


@dataclass
class DocumentRow:
    """One uploaded document as the page presents it."""

    id: str
    filename: str
    uploaded_at: str
    size_label: str
    page_label: str
    chunk_count: int

    @property
    def searchable(self) -> bool:
        """Whether this document can actually answer anything.

        A document with no chunks is stored but contributes nothing to an answer.
        Showing that plainly matters: otherwise a doctor sees a file listed, assumes
        the agent is using it, and never finds out it is inert.
        """
        return self.chunk_count > 0


@dataclass
class DocumentLibraryViewModel:
    """Everything the documents page needs.

    Attributes:
        documents: Uploaded documents, most recent first.
        enabled: Whether a document store is configured at all. When ``False`` the
            page explains how to turn it on instead of offering a dead upload form.
        retrieval_enabled: Whether an embedder is configured. Without one, uploads
            are stored and downloadable but answer no questions — a distinction the
            doctor has to be told, not left to discover.
        message: A confirmation to show (e.g. after an upload).
        error: A failure to show.
        store_error: Set when the document list itself could not be read.
    """

    documents: list[DocumentRow] = field(default_factory=list)
    enabled: bool = True
    retrieval_enabled: bool = True
    message: str | None = None
    error: str | None = None
    store_error: str | None = None
    #: The viewer's role, carried into every link and form on the page.
    #:
    #: Every action here is role-gated, and in a local run the role arrives only as
    #: a ``?role=`` query parameter — there is no authenticating proxy setting the
    #: header. Omitting it from the rendered URLs meant the role was lost the moment
    #: the doctor clicked anything, so Download, Delete, "Fill setup form", and even
    #: Upload all answered 403 from a browser while working fine when the parameter
    #: was passed by hand.
    role: str | None = None

    @property
    def empty(self) -> bool:
        return not self.documents

    def url(self, path: str) -> str:
        """``path`` with the viewer's role preserved, when there is one."""
        if not self.role:
            return path
        separator = "&" if "?" in path else "?"
        return f"{path}{separator}role={quote(self.role, safe='')}"


def _size_label(byte_size: int) -> str:
    """Human-readable file size."""
    if byte_size < 1024:
        return f"{byte_size} B"
    if byte_size < 1024 * 1024:
        return f"{byte_size / 1024:.0f} KB"
    return f"{byte_size / (1024 * 1024):.1f} MB"


def _page_label(document: ClinicDocument) -> str:
    """Page count, or a dash for formats that have no pages."""
    if document.page_count is None:
        return "—"
    return f"{document.page_count} page" + ("" if document.page_count == 1 else "s")


def _row(document: ClinicDocument) -> DocumentRow:
    return DocumentRow(
        id=document.id,
        filename=document.filename,
        uploaded_at=document.uploaded_at,
        size_label=_size_label(document.byte_size),
        page_label=_page_label(document),
        chunk_count=document.chunk_count,
    )


def build_document_library_view_model(
    store: ClinicDocumentStore | None,
    *,
    retrieval_enabled: bool = True,
    message: str | None = None,
    error: str | None = None,
    role: str | None = None,
) -> DocumentLibraryViewModel:
    """Build the documents page view-model by reading ``store``."""
    if store is None:
        return DocumentLibraryViewModel(
            enabled=False,
            retrieval_enabled=False,
            message=message,
            error=error,
            role=role,
        )

    result = store.list_documents()
    if is_err(result):
        return DocumentLibraryViewModel(
            retrieval_enabled=retrieval_enabled,
            message=message,
            error=error,
            role=role,
            store_error=result.error.detail,
        )
    return DocumentLibraryViewModel(
        documents=[_row(document) for document in result.value],
        retrieval_enabled=retrieval_enabled,
        message=message,
        error=error,
        role=role,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_INTRO = (
    "Upload the documents you already have — a practice information sheet, your "
    "holiday list, a new-patient handout. They are used two ways: the agent answers "
    "callers' questions about the practice from them, and you can pull clinic "
    "details out of one to fill in your setup form."
)

_LIMITS = (
    "PDF or plain text, up to 10 MB. A scanned PDF will not work: the text has to "
    "be text, not a picture of text."
)


def _render_disabled() -> str:
    """The state when no document store is configured."""
    return (
        '<div class="documents-disabled" role="status">'
        "<p>Document uploads are not enabled for this deployment.</p>"
        "<p>Set <code>CLINIC_DOCUMENTS_BUCKET</code> (or "
        "<code>CLINIC_RECORDINGS_BUCKET</code>, which is reused with a separate "
        "prefix) and restart to turn them on.</p>"
        "</div>"
    )


def _render_upload_form(view: DocumentLibraryViewModel) -> str:
    return (
        f'<form class="documents-upload" method="post" '
        f'action="{_esc(view.url(DOCUMENTS_ENDPOINT))}" '
        'enctype="multipart/form-data">'
        '<label for="document-file">Choose a document</label>'
        f'<input type="file" id="document-file" name="document" '
        f'accept="{_esc(ACCEPTED_UPLOAD_TYPES)}" required>'
        f'<p class="documents-hint">{_esc(_LIMITS)}</p>'
        '<button type="submit" class="documents-upload__submit">Upload</button>'
        "</form>"
    )


def _render_row(view: DocumentLibraryViewModel, row: DocumentRow) -> str:
    """One document with its actions."""
    doc = _esc(row.id)
    base = f"{DOCUMENTS_ENDPOINT}/{quote(row.id, safe='')}"
    status = (
        f"{row.chunk_count} passage" + ("" if row.chunk_count == 1 else "s")
        if row.searchable
        else "not searchable"
    )
    status_class = "documents-row__status" + (
        "" if row.searchable else " documents-row__status--inert"
    )
    return (
        f'<li class="documents-row" data-document-id="{doc}">'
        '<div class="documents-row__detail">'
        f'<span class="documents-row__name">{_esc(row.filename)}</span>'
        '<span class="documents-row__meta">'
        f"{_esc(row.uploaded_at)} · {_esc(row.size_label)} · "
        f"{_esc(row.page_label)} · "
        f'<span class="{status_class}">{_esc(status)}</span>'
        "</span>"
        "</div>"
        '<div class="documents-row__actions">'
        f'<a class="documents-action" href="{_esc(view.url(base + "/download"))}">'
        "Download</a>"
        f'<form method="post" action="{_esc(view.url(base + "/extract"))}">'
        '<button type="submit" class="documents-action">'
        "Fill setup form from this</button></form>"
        f'<form method="post" action="{_esc(view.url(base + "/delete"))}">'
        '<button type="submit" class="documents-action documents-action--danger">'
        "Delete</button></form>"
        "</div>"
        "</li>"
    )


def render_document_library(view: DocumentLibraryViewModel) -> str:
    """Render the documents page body."""
    parts: list[str] = ['<h1 class="documents-title">Clinic documents</h1>']

    if view.message:
        parts.append(
            f'<p class="wizard-success" role="status">{_esc(view.message)}</p>'
        )
    if view.error:
        parts.append(f'<p class="wizard-error" role="alert">{_esc(view.error)}</p>')

    if not view.enabled:
        parts.append(_render_disabled())
        return "\n".join(parts)

    parts.append(f'<p class="documents-intro">{_esc(_INTRO)}</p>')

    if not view.retrieval_enabled:
        parts.append(
            '<p class="wizard-notice" role="status">'
            "Uploads are being stored, but the agent cannot answer from them yet: "
            "no embedding model is configured, so there is nothing to search. "
            "Documents you upload now will work once one is set."
            "</p>"
        )

    parts.append(_render_upload_form(view))

    parts.append('<h2 class="documents-subtitle">Uploaded</h2>')
    if view.store_error:
        parts.append(
            '<p class="wizard-error" role="alert">'
            f"Could not read the uploaded documents: {_esc(view.store_error)}</p>"
        )
    elif view.empty:
        parts.append(
            '<p class="documents-empty">Nothing uploaded yet. The agent is '
            "answering only from your setup form.</p>"
        )
    else:
        rows = "".join(_render_row(view, row) for row in view.documents)
        parts.append(f'<ul class="documents-list">{rows}</ul>')

    # /onboarding is ungated, so it needs no role; the dashboard does.
    parts.append(
        '<p class="documents-links">'
        '<a href="/onboarding">Clinic setup form</a> · '
        f'<a href="{_esc(view.url("/"))}">Back to dashboard</a>'
        "</p>"
    )
    return "\n".join(parts)


def render_document_library_page(view: DocumentLibraryViewModel) -> str:
    """Render the documents page as a complete HTML document."""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "  <head>\n"
        '    <meta charset="utf-8" />\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        '    <meta name="color-scheme" content="light dark" />\n'
        "    <title>Clinic documents</title>\n"
        "  </head>\n"
        "  <body>\n"
        '    <main class="documents-page">\n'
        f"      {render_document_library(view)}\n"
        "    </main>\n"
        "  </body>\n"
        "</html>\n"
    )


__all__ = [
    "DOCUMENTS_ENDPOINT",
    "ACCEPTED_UPLOAD_TYPES",
    "DocumentRow",
    "DocumentLibraryViewModel",
    "build_document_library_view_model",
    "render_document_library",
    "render_document_library_page",
]
