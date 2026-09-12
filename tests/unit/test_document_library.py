"""Unit tests for the clinic-documents page (``components/document_library.py``)."""

from __future__ import annotations

from clinic_front_desk.dashboard.components.document_library import (
    DOCUMENTS_ENDPOINT,
    DocumentLibraryViewModel,
    build_document_library_view_model,
    render_document_library,
    render_document_library_page,
)
from clinic_front_desk.data_layer.faults import fail_on, wrap
from clinic_front_desk.data_layer.memory import MemoryClinicDocumentStore
from clinic_front_desk.models import ClinicDocument, DocumentChunk


def _document(
    doc_id: str = "doc1",
    *,
    filename: str = "practice-info.pdf",
    byte_size: int = 2048,
    page_count: int | None = 3,
    uploaded_at: str = "2026-01-01T09:00:00+00:00",
) -> ClinicDocument:
    return ClinicDocument(
        id=doc_id,
        filename=filename,
        content_type="application/pdf",
        uploaded_at=uploaded_at,
        byte_size=byte_size,
        chunk_count=0,
        page_count=page_count,
    )


def _store_with(*documents: tuple[ClinicDocument, int]) -> MemoryClinicDocumentStore:
    store = MemoryClinicDocumentStore()
    for document, chunk_count in documents:
        chunks = [
            DocumentChunk(
                document_id=document.id, index=i, text=f"passage {i}", page=None,
                embedding=(1.0,),
            )
            for i in range(chunk_count)
        ]
        store.put(document, original=b"x" * document.byte_size, chunks=chunks)
    return store


# ---------------------------------------------------------------------------
# View model
# ---------------------------------------------------------------------------


def test_an_absent_store_reports_the_feature_as_disabled() -> None:
    view = build_document_library_view_model(None)

    assert not view.enabled
    assert not view.retrieval_enabled
    assert view.empty


def test_an_empty_store_is_enabled_but_empty() -> None:
    view = build_document_library_view_model(MemoryClinicDocumentStore())

    assert view.enabled
    assert view.empty
    assert view.store_error is None


def test_documents_are_presented_with_readable_metadata() -> None:
    store = _store_with((_document(byte_size=2048), 7))

    view = build_document_library_view_model(store)

    row = view.documents[0]
    assert row.filename == "practice-info.pdf"
    assert row.size_label == "2 KB"
    assert row.page_label == "3 pages"
    assert row.chunk_count == 7
    assert row.searchable


def test_a_single_page_is_labelled_singular() -> None:
    view = build_document_library_view_model(_store_with((_document(page_count=1), 1)))

    assert view.documents[0].page_label == "1 page"


def test_a_pageless_document_shows_a_dash_rather_than_a_page_count() -> None:
    store = _store_with((_document(filename="notes.txt", page_count=None), 1))

    view = build_document_library_view_model(store)

    assert view.documents[0].page_label == "\u2014"


def test_a_document_with_no_chunks_is_marked_unsearchable() -> None:
    # A stored document that answers nothing must not look like one that works.
    view = build_document_library_view_model(_store_with((_document(), 0)))

    assert not view.documents[0].searchable


def test_sizes_scale_to_kb_and_mb() -> None:
    store = _store_with(
        (_document("small", byte_size=512), 1),
        (_document("large", byte_size=3 * 1024 * 1024), 1),
    )

    labels = {row.id: row.size_label for row in
              build_document_library_view_model(store).documents}

    assert labels["small"] == "512 B"
    assert labels["large"] == "3.0 MB"


def test_documents_are_listed_most_recent_first() -> None:
    store = _store_with(
        (_document("older", uploaded_at="2026-01-01T09:00:00+00:00"), 1),
        (_document("newer", uploaded_at="2026-06-01T09:00:00+00:00"), 1),
    )

    view = build_document_library_view_model(store)

    assert [row.id for row in view.documents] == ["newer", "older"]


def test_a_store_read_failure_is_carried_rather_than_raised() -> None:
    faulty = wrap(_store_with((_document(), 1)), fail_on("list_documents"))

    view = build_document_library_view_model(faulty)

    assert view.store_error is not None
    assert view.empty


def test_messages_and_errors_are_carried_through() -> None:
    view = build_document_library_view_model(
        MemoryClinicDocumentStore(), message="Uploaded x.", error="Nope."
    )

    assert view.message == "Uploaded x."
    assert view.error == "Nope."


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_the_upload_form_posts_multipart_to_the_documents_endpoint() -> None:
    html = render_document_library(build_document_library_view_model(MemoryClinicDocumentStore()))

    assert f'action="{DOCUMENTS_ENDPOINT}"' in html
    assert 'method="post"' in html
    # Without this the file never reaches the server.
    assert 'enctype="multipart/form-data"' in html
    assert 'type="file"' in html
    assert 'name="document"' in html


def test_the_empty_state_says_where_answers_currently_come_from() -> None:
    html = render_document_library(build_document_library_view_model(MemoryClinicDocumentStore()))

    assert "documents-empty" in html
    assert "setup form" in html


def test_each_document_offers_download_extract_and_delete() -> None:
    view = build_document_library_view_model(_store_with((_document("abc"), 2)))

    html = render_document_library(view)

    assert f'href="{DOCUMENTS_ENDPOINT}/abc/download"' in html
    assert f'action="{DOCUMENTS_ENDPOINT}/abc/extract"' in html
    assert f'action="{DOCUMENTS_ENDPOINT}/abc/delete"' in html


def test_the_viewers_role_is_carried_into_every_url() -> None:
    # Locally the role arrives only as a query parameter, so a rendered URL that
    # drops it is a button that answers 403.
    view = build_document_library_view_model(
        _store_with((_document("abc"), 2)), role="doctor"
    )

    html = render_document_library(view)

    assert 'action="/documents?role=doctor"' in html  # upload
    assert 'href="/documents/abc/download?role=doctor"' in html
    assert 'action="/documents/abc/extract?role=doctor"' in html
    assert 'action="/documents/abc/delete?role=doctor"' in html
    assert 'href="/?role=doctor"' in html


def test_urls_stay_bare_when_there_is_no_role() -> None:
    # In a deployment the role comes from a header set by the proxy, so appending
    # an empty parameter would be noise.
    view = build_document_library_view_model(_store_with((_document("abc"), 2)))

    html = render_document_library(view)

    assert 'href="/documents/abc/download"' in html
    assert "role=" not in html


def test_a_role_with_url_special_characters_is_encoded() -> None:
    view = build_document_library_view_model(
        _store_with((_document("abc"), 1)), role="a b&c"
    )

    html = render_document_library(view)

    assert "a%20b%26c" in html
    assert "a b&c" not in html


def test_a_document_id_with_url_special_characters_is_encoded() -> None:
    view = build_document_library_view_model(
        _store_with((_document("a b/c"), 1)), role="doctor"
    )

    html = render_document_library(view)

    assert "/documents/a%20b%2Fc/download?role=doctor" in html


def test_destructive_and_state_changing_actions_are_posts_not_links() -> None:
    view = build_document_library_view_model(_store_with((_document("abc"), 2)))

    html = render_document_library(view)

    # A GET must never delete: crawlers and prefetchers follow links.
    assert f'href="{DOCUMENTS_ENDPOINT}/abc/delete"' not in html
    delete_at = html.find(f'action="{DOCUMENTS_ENDPOINT}/abc/delete"')
    assert 'method="post"' in html[max(0, delete_at - 80):delete_at]


def test_an_unsearchable_document_is_flagged_in_the_markup() -> None:
    html = render_document_library(build_document_library_view_model(_store_with((_document(), 0))))

    assert "documents-row__status--inert" in html
    assert "not searchable" in html


def test_a_searchable_document_reports_its_passage_count() -> None:
    html = render_document_library(build_document_library_view_model(_store_with((_document(), 4))))

    assert "4 passages" in html
    assert "documents-row__status--inert" not in html


def test_one_passage_is_reported_in_the_singular() -> None:
    html = render_document_library(build_document_library_view_model(_store_with((_document(), 1))))

    assert "1 passage" in html
    assert "1 passages" not in html


def test_the_disabled_state_explains_how_to_switch_uploads_on() -> None:
    html = render_document_library(build_document_library_view_model(None))

    assert "documents-disabled" in html
    assert "CLINIC_DOCUMENTS_BUCKET" in html
    # No upload form, so the page never offers a control that cannot work.
    assert 'type="file"' not in html


def test_without_an_embedder_the_page_says_answers_are_not_possible_yet() -> None:
    view = build_document_library_view_model(
        MemoryClinicDocumentStore(), retrieval_enabled=False
    )

    html = render_document_library(view)

    assert "cannot answer from them yet" in html


def test_with_an_embedder_no_such_warning_appears() -> None:
    html = render_document_library(
        build_document_library_view_model(MemoryClinicDocumentStore())
    )

    assert "cannot answer from them yet" not in html


def test_a_store_error_is_shown_instead_of_a_silently_empty_list() -> None:
    faulty = wrap(_store_with((_document(), 1)), fail_on("list_documents"))

    html = render_document_library(build_document_library_view_model(faulty))

    assert "Could not read the uploaded documents" in html
    assert "documents-empty" not in html


def test_a_filename_cannot_inject_markup() -> None:
    nasty = _document(filename='<script>alert("x")</script>.pdf')
    view = build_document_library_view_model(_store_with((nasty, 1)))

    html = render_document_library(view)

    # The filename is doctor-supplied and echoed back, so it has to be escaped.
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_a_message_cannot_inject_markup() -> None:
    view = build_document_library_view_model(
        MemoryClinicDocumentStore(), message="<img src=x onerror=1>"
    )

    html = render_document_library(view)

    assert "<img" not in html


def test_the_page_is_a_complete_html_document_with_the_stylesheet_hook() -> None:
    page = render_document_library_page(
        build_document_library_view_model(MemoryClinicDocumentStore())
    )

    assert page.startswith("<!DOCTYPE html>")
    assert "</html>" in page
    assert "<title>Clinic documents</title>" in page
    # dashboard_app injects the stylesheet by replacing </head>.
    assert "</head>" in page


def test_the_page_links_back_to_setup_and_the_dashboard() -> None:
    page = render_document_library_page(
        build_document_library_view_model(MemoryClinicDocumentStore())
    )

    assert 'href="/onboarding"' in page
    assert 'href="/"' in page


def test_the_intro_explains_both_uses_of_an_upload() -> None:
    html = render_document_library(
        build_document_library_view_model(MemoryClinicDocumentStore())
    )

    assert "answers callers" in html
    assert "setup form" in html


def test_the_hint_warns_that_scans_will_not_work() -> None:
    html = render_document_library(
        build_document_library_view_model(MemoryClinicDocumentStore())
    )

    # The single most likely upload failure, so it is said before the attempt.
    assert "scanned PDF will not work" in html


def test_an_empty_view_model_renders_without_a_store() -> None:
    # The dataclass has to stand on its own for tests and error paths.
    html = render_document_library(DocumentLibraryViewModel())

    assert "Clinic documents" in html
