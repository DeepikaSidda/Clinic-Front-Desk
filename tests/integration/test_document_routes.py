"""Integration tests for the document routes over the real ASGI app.

Exercises the whole portal path through HTTP — multipart parsing, the role gate,
ingestion, retrieval, and the wizard pre-fill — using fake Bedrock collaborators so
nothing reaches AWS. The end-to-end test is the one that matters most: a doctor
uploads a file and the agent answers a caller from it, which is the feature.
"""

from __future__ import annotations

import re
import zlib
from typing import Any

import pytest

from clinic_front_desk.deployment.app import build_memory_application
from clinic_front_desk.models import is_ok

pytestmark = pytest.mark.integration

starlette_testclient = pytest.importorskip("starlette.testclient")
TestClient = starlette_testclient.TestClient

SHEET = b"""Springfield Hearing Clinic

We are located at 123 Main Street, Suite 302, Springfield.

Office hours
Monday to Thursday 9:00 am to 5:00 pm.

Parking
Patient parking is free in the surface lot behind the building, off Elm Street.

Services we offer
Hearing Test - $150

Our providers
Dr. Alice Nguyen, Audiologist, sees patients Monday through Thursday.

Insurance
We accept Aetna.
"""


class TokenEmbedder:
    """A deterministic bag-of-words embedding: no AWS, real vector behaviour.

    Uses ``crc32`` rather than ``hash()``. Python salts string hashing per
    interpreter run, so a ``hash()``-based fake produces different vectors every
    time and the retrieval assertions pass or fail depending on the run — which is
    exactly how this test first failed.
    """

    DIMS = 24

    @staticmethod
    def _bucket(token: str) -> int:
        return zlib.crc32(token.encode()) % TokenEmbedder.DIMS

    def embed(self, text: str) -> tuple[float, ...]:
        vector = [0.0] * self.DIMS
        for token in text.lower().split():
            vector[self._bucket(token)] += 1.0
        norm = sum(v * v for v in vector) ** 0.5 or 1.0
        return tuple(v / norm for v in vector)

    def embed_all(self, texts: Any) -> list[tuple[float, ...]]:
        return [self.embed(t) for t in texts]


class SheetExtractor:
    """Returns a reading grounded in SHEET, as the real model does."""

    def extract(self, text: str) -> dict[str, Any]:
        return {
            "location": "123 Main Street, Suite 302, Springfield",
            "hours": [{"day": "monday", "open": "9:00 am", "close": "5:00 pm"}],
            "services": [{"name": "Hearing Test", "price": "150"}],
            "accepted_insurance": ["Aetna"],
            "providers": [
                {"name": "Dr. Alice Nguyen", "specialty": "Audiologist",
                 "days": ["monday"]}
            ],
        }


@pytest.fixture
def app() -> Any:
    return build_memory_application(
        embedder=TokenEmbedder(), config_extractor=SheetExtractor()
    )


@pytest.fixture
def client(app: Any) -> Any:
    from clinic_front_desk.deployment.server import create_asgi_app

    return TestClient(create_asgi_app(app))


def _upload(client: Any, data: bytes = SHEET, name: str = "clinic-info.txt") -> Any:
    return client.post(
        "/documents?role=doctor",
        files={"document": (name, data, "text/plain")},
    )


def _document_id(app: Any, filename: str = "clinic-info.txt") -> str:
    listed = app.stores.documents.list_documents()
    assert is_ok(listed)
    return next(d.id for d in listed.value if d.filename == filename)


# ---------------------------------------------------------------------------
# Role gating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["", "?role=assistant", "?role=nonsense"])
def test_only_the_doctor_may_see_the_documents_page(client: Any, query: str) -> None:
    # Uploads become what the agent tells callers about the practice, so this is
    # the practice owner's decision.
    assert client.get(f"/documents{query}").status_code == 403


def test_the_doctor_may_see_the_documents_page(client: Any) -> None:
    response = client.get("/documents?role=doctor")

    assert response.status_code == 200
    assert "Clinic documents" in response.text


def test_an_assistant_may_not_upload(client: Any, app: Any) -> None:
    response = client.post(
        "/documents?role=assistant",
        files={"document": ("x.txt", SHEET, "text/plain")},
    )

    assert response.status_code == 403
    assert app.stores.documents.list_documents().value == []


def test_an_assistant_may_not_download_delete_or_extract(
    client: Any, app: Any
) -> None:
    _upload(client)
    doc_id = _document_id(app)

    assert client.get(f"/documents/{doc_id}/download?role=assistant").status_code == 403
    assert client.post(f"/documents/{doc_id}/extract?role=assistant").status_code == 403
    assert client.post(f"/documents/{doc_id}/delete?role=assistant").status_code == 403
    # Nothing was removed by the denied delete.
    assert len(app.stores.documents.list_documents().value) == 1


def test_the_role_header_is_honoured_as_well_as_the_query(client: Any) -> None:
    response = client.get("/documents", headers={"X-Clinic-Role": "doctor"})

    assert response.status_code == 200


def test_every_link_and_form_on_the_page_carries_the_role(client: Any) -> None:
    """Follow the page's own URLs, adding nothing.

    This is the test that was missing. Every other test here builds its URLs by
    hand with ``?role=doctor`` appended, so all of them passed while the rendered
    page dropped the role entirely and Download, Delete, "Fill setup form" and
    Upload each answered 403 in a browser. Locally the role arrives only as a query
    parameter, so a link that omits it is a broken button.
    """
    _upload(client)
    page = client.get("/documents?role=doctor").text

    hrefs = re.findall(r'href="(/documents[^"]*)"', page)
    actions = re.findall(r'action="(/documents[^"]*)"', page)
    assert hrefs, "expected at least a download link"
    assert actions, "expected the upload and per-document forms"

    for url in hrefs:
        assert "role=" in url, f"link drops the role: {url}"
        assert client.get(url).status_code == 200, f"link is broken: {url}"

    for url in actions:
        assert "role=" in url, f"form action drops the role: {url}"

    # The upload form's own action must accept a file.
    upload_action = next(a for a in actions if a.split("?")[0] == "/documents")
    posted = client.post(
        upload_action, files={"document": ("x.txt", SHEET, "text/plain")}
    )
    assert posted.status_code == 200

    # And the two per-document POST actions must work as rendered.
    for url in actions:
        if url.split("?")[0].endswith("/extract"):
            assert client.post(url).status_code == 200, f"extract broken: {url}"
    for url in actions:
        if url.split("?")[0].endswith("/delete"):
            assert client.post(url).status_code == 200, f"delete broken: {url}"


def test_the_dashboard_link_on_the_documents_page_carries_the_role(
    client: Any,
) -> None:
    page = client.get("/documents?role=doctor").text

    assert 'href="/?role=doctor"' in page


def test_the_dashboard_offers_the_documents_link_only_to_the_doctor(
    client: Any, app: Any
) -> None:
    from clinic_front_desk.models import ClinicKnowledgeBase, DayHours, Provider, ServiceConfig

    app.stores.knowledge_base.save(
        ClinicKnowledgeBase(
            location="123 Main St",
            hours={1: DayHours(open="09:00", close="17:00")},
            services=[ServiceConfig(name="Hearing Test", price=150.0)],
            providers=[Provider(id="p1", name="Dr. A", specialty="ENT")],
            configured=True,
        )
    )

    doctor = client.get("/?role=doctor").text
    assistant = client.get("/?role=assistant").text

    assert 'href="/documents' in doctor
    # Never offer a link that answers 403.
    assert 'href="/documents' not in assistant


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


def test_a_successful_upload_confirms_what_is_now_answerable(client: Any) -> None:
    response = _upload(client)

    assert response.status_code == 200
    assert "passages the agent can now answer from" in response.text
    assert "clinic-info.txt" in response.text


def test_an_upload_is_stored_with_chunks_and_embeddings(client: Any, app: Any) -> None:
    _upload(client)

    documents = app.stores.documents.list_documents().value
    chunks = app.stores.documents.list_chunks().value

    assert len(documents) == 1
    assert documents[0].chunk_count == len(chunks)
    assert chunks
    assert all(chunk.embedding for chunk in chunks)


def test_an_unreadable_upload_is_a_page_error_not_an_http_failure(
    client: Any, app: Any
) -> None:
    # The doctor corrects this on the same page; it is their mistake, not a fault.
    response = client.post(
        "/documents?role=doctor",
        files={"document": ("scan.pdf", b"%PDF-1.4 not a real pdf", "application/pdf")},
    )

    assert response.status_code == 200
    assert "wizard-error" in response.text
    assert app.stores.documents.list_documents().value == []


def test_a_request_with_no_file_is_reported_on_the_page(client: Any) -> None:
    response = client.post("/documents?role=doctor", data={"other": "field"})

    assert response.status_code == 200
    assert "No file was received" in response.text


def test_an_empty_file_is_reported(client: Any, app: Any) -> None:
    response = client.post(
        "/documents?role=doctor", files={"document": ("empty.txt", b"", "text/plain")}
    )

    assert response.status_code == 200
    assert "wizard-error" in response.text or "No file" in response.text
    assert app.stores.documents.list_documents().value == []


def test_uploading_twice_lists_both_documents(client: Any, app: Any) -> None:
    _upload(client, name="first.txt")
    _upload(client, name="second.txt")

    listed = app.stores.documents.list_documents().value

    assert {d.filename for d in listed} == {"first.txt", "second.txt"}


def test_a_filename_with_markup_is_escaped_on_the_page(client: Any) -> None:
    response = _upload(client, name="<script>alert(1)</script>.txt")

    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def test_the_original_downloads_byte_for_byte(client: Any, app: Any) -> None:
    _upload(client)

    response = client.get(f"/documents/{_document_id(app)}/download?role=doctor")

    assert response.status_code == 200
    # The doctor gets exactly what they uploaded, not a re-encoding.
    assert response.content == SHEET


def test_the_download_is_an_attachment_and_not_cached(client: Any, app: Any) -> None:
    _upload(client)

    response = client.get(f"/documents/{_document_id(app)}/download?role=doctor")

    disposition = response.headers["content-disposition"]
    # attachment, not inline: an uploaded file must not render in the browser.
    assert disposition.startswith("attachment")
    assert "clinic-info.txt" in disposition
    assert response.headers["cache-control"] == "private, no-store"


def test_downloading_an_unknown_document_is_a_404(client: Any) -> None:
    response = client.get("/documents/never-uploaded/download?role=doctor")

    assert response.status_code == 404


def test_a_filename_cannot_inject_response_headers(client: Any, app: Any) -> None:
    # The filename is doctor-supplied and goes straight into Content-Disposition.
    _upload(client, name='evil"\r\nX-Injected: yes.txt')
    only = app.stores.documents.list_documents().value[0]

    response = client.get(f"/documents/{only.id}/download?role=doctor")

    assert response.status_code == 200
    assert "x-injected" not in {key.lower() for key in response.headers}
    assert "\r" not in response.headers["content-disposition"]
    assert response.headers["content-disposition"].count('"') == 2


# ---------------------------------------------------------------------------
# Extract to the wizard
# ---------------------------------------------------------------------------


def test_extract_renders_a_prefilled_wizard_that_has_saved_nothing(
    client: Any, app: Any
) -> None:
    _upload(client)

    response = client.post(f"/documents/{_document_id(app)}/extract?role=doctor")

    assert response.status_code == 200
    assert "onboarding-wizard" in response.text
    assert "have not been saved" in response.text
    assert "Confirm and save" in response.text
    assert "123 Main Street" in response.text
    assert "Hearing Test" in response.text
    # The gate is the doctor pressing submit; nothing is persisted before that.
    assert app.stores.knowledge_base.get().value is None


def test_extracted_times_reach_the_form_zero_padded(client: Any, app: Any) -> None:
    _upload(client)

    response = client.post(f"/documents/{_document_id(app)}/extract?role=doctor")

    # Nothing downstream format-checks HH:MM, so "9:00 am" must already be "09:00".
    assert 'value="09:00"' in response.text
    assert 'value="17:00"' in response.text


def test_extract_shows_the_review_notes(client: Any, app: Any) -> None:
    _upload(client)

    response = client.post(f"/documents/{_document_id(app)}/extract?role=doctor")

    assert "review-notes" in response.text
    assert "working hours" in response.text


def test_extract_on_an_unknown_document_is_a_404(client: Any) -> None:
    assert client.post("/documents/nope/extract?role=doctor").status_code == 404


def test_extract_without_an_extractor_configured_is_a_404() -> None:
    from clinic_front_desk.deployment.server import create_asgi_app

    app = build_memory_application(embedder=TokenEmbedder())
    client = TestClient(create_asgi_app(app))
    _upload(client)

    response = client.post(f"/documents/{_document_id(app)}/extract?role=doctor")

    assert response.status_code == 404


def test_a_document_with_no_clinic_details_reports_back_on_the_page() -> None:
    from clinic_front_desk.deployment.server import create_asgi_app

    class NothingFound:
        def extract(self, text: str) -> dict[str, Any]:
            return {"location": "", "hours": [], "services": [],
                    "accepted_insurance": [], "providers": []}

    app = build_memory_application(
        embedder=TokenEmbedder(), config_extractor=NothingFound()
    )
    client = TestClient(create_asgi_app(app))
    _upload(client, data=b"Notice\n\nThe waiting room is being repainted next week.\n")

    response = client.post(f"/documents/{_document_id(app)}/extract?role=doctor")

    # Back to the documents page with an explanation, not an empty form that looks
    # like a failed read of their data.
    assert response.status_code == 200
    assert "Clinic documents" in response.text
    assert "wizard-error" in response.text


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_removes_the_document_and_its_chunks(client: Any, app: Any) -> None:
    _upload(client)
    doc_id = _document_id(app)

    response = client.post(f"/documents/{doc_id}/delete?role=doctor")

    assert response.status_code == 200
    assert "no longer answer from it" in response.text
    assert app.stores.documents.list_documents().value == []
    assert app.stores.documents.list_chunks().value == []


def test_deleting_an_unknown_document_does_not_error(client: Any) -> None:
    # The doctor may double-click; that must not produce a failure page.
    assert client.post("/documents/never-uploaded/delete?role=doctor").status_code == 200


def test_delete_is_not_reachable_by_get(client: Any, app: Any) -> None:
    _upload(client)

    response = client.get(f"/documents/{_document_id(app)}/delete?role=doctor")

    assert response.status_code == 405
    assert len(app.stores.documents.list_documents().value) == 1


# ---------------------------------------------------------------------------
# The onboarding form can now actually be submitted
# ---------------------------------------------------------------------------


def test_posting_the_onboarding_form_saves_the_configuration(
    client: Any, app: Any
) -> None:
    # The wizard has always posted to /onboarding; only GET was routed, so this
    # path used to 405 and no configuration could be saved through the UI.
    response = client.post(
        "/onboarding",
        data={
            "location": "123 Main Street, Springfield",
            "hours[1].open": "09:00",
            "hours[1].close": "17:00",
            "services[0].name": "Hearing Test",
            "services[0].price": "150",
            "accepted_insurance": "Aetna",
            "providers[0].name": "Dr. Alice Nguyen",
            "providers[0].specialty": "Audiologist",
            "providers[0].days": "1",
            "providers[0].start": "09:00",
            "providers[0].end": "17:00",
        },
    )

    assert response.status_code == 200
    assert "configuration saved" in response.text.lower()
    kb = app.stores.knowledge_base.get().value
    assert kb is not None
    assert kb.configured
    assert kb.location == "123 Main Street, Springfield"


def test_an_invalid_onboarding_submission_is_rejected_with_the_values_kept(
    client: Any, app: Any
) -> None:
    response = client.post("/onboarding", data={"location": "123 Main Street"})

    assert response.status_code == 200
    assert "correct the highlighted fields" in response.text
    assert "123 Main Street" in response.text
    assert app.stores.knowledge_base.get().value is None


# ---------------------------------------------------------------------------
# The whole point: upload, then a caller is answered from it
# ---------------------------------------------------------------------------


def test_an_uploaded_document_answers_a_caller_question(client: Any, app: Any) -> None:
    _upload(client)

    session = app.start_voice_session("doc-routes-e2e")
    answer = session.toolset.answer_faq(
        topic="clinic_info", question="is there parking at the clinic"
    )

    assert answer.__class__.__name__ == "Ok"
    assert "surface lot" in answer.value


def test_configuration_still_wins_over_the_document(client: Any, app: Any) -> None:
    from clinic_front_desk.models import ClinicKnowledgeBase, DayHours, Provider, ServiceConfig

    app.stores.knowledge_base.save(
        ClinicKnowledgeBase(
            location="999 Configured Way",
            hours={1: DayHours(open="09:00", close="17:00")},
            services=[ServiceConfig(name="Hearing Test", price=150.0)],
            providers=[Provider(id="p1", name="Dr. A", specialty="ENT")],
            configured=True,
        )
    )
    _upload(client)

    session = app.start_voice_session("doc-routes-precedence")
    answer = session.toolset.answer_faq(topic="location", question="where are you")

    assert answer.__class__.__name__ == "Ok"
    assert "999 Configured Way" in answer.value


def test_a_deleted_document_stops_answering(client: Any, app: Any) -> None:
    _upload(client)
    session = app.start_voice_session("doc-routes-before")
    assert session.toolset.answer_faq(
        topic="clinic_info", question="is there parking at the clinic"
    ).__class__.__name__ == "Ok"

    client.post(f"/documents/{_document_id(app)}/delete?role=doctor")

    # A new session, because the corpus is cached for the length of a call.
    after = app.start_voice_session("doc-routes-after")
    answer = after.toolset.answer_faq(
        topic="clinic_info", question="is there parking at the clinic"
    )

    assert answer.__class__.__name__ == "Err"


def test_a_new_upload_is_live_on_the_next_call(client: Any, app: Any) -> None:
    before = app.start_voice_session("doc-routes-empty")
    assert before.toolset.answer_faq(
        topic="clinic_info", question="is there parking at the clinic"
    ).__class__.__name__ == "Err"

    _upload(client)

    after = app.start_voice_session("doc-routes-uploaded")
    assert after.toolset.answer_faq(
        topic="clinic_info", question="is there parking at the clinic"
    ).__class__.__name__ == "Ok"


def test_without_a_document_store_the_page_explains_rather_than_erroring() -> None:
    # A deployment with uploads switched off should still render the page.
    from clinic_front_desk.deployment.app import ApplicationStores
    from clinic_front_desk.deployment.server import create_asgi_app

    app = build_memory_application()
    stores = app.stores
    app._stores = ApplicationStores(
        appointments=stores.appointments,
        patients=stores.patients,
        waitlist=stores.waitlist,
        decisions=stores.decisions,
        knowledge_base=stores.knowledge_base,
        call_sessions=stores.call_sessions,
        escalations=stores.escalations,
        documents=None,
    )
    client = TestClient(create_asgi_app(app))

    response = client.get("/documents?role=doctor")

    assert response.status_code == 200
    assert "not enabled" in response.text
    assert "CLINIC_DOCUMENTS_BUCKET" in response.text


def test_uploading_without_a_store_configured_is_a_404() -> None:
    from clinic_front_desk.deployment.app import ApplicationStores
    from clinic_front_desk.deployment.server import create_asgi_app

    app = build_memory_application()
    stores = app.stores
    app._stores = ApplicationStores(
        appointments=stores.appointments,
        patients=stores.patients,
        waitlist=stores.waitlist,
        decisions=stores.decisions,
        knowledge_base=stores.knowledge_base,
        call_sessions=stores.call_sessions,
        escalations=stores.escalations,
        documents=None,
    )
    client = TestClient(create_asgi_app(app))

    response = _upload(client)

    assert response.status_code == 404
