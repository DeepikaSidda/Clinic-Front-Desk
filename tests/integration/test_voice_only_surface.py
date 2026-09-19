"""The public surface when only the voice agent is exposed.

For putting the agent on a URL judges can click. The doctor's dashboard decides
access from a ``?role=`` query parameter, which ``resolve_role`` itself documents
as not a security control — it exists for local runs with no auth layer in front.
On a public host that means anyone holding the link is the doctor, reading patient
names, mobile numbers and blood groups.

Not routing those paths is a stronger guarantee than guarding them, and less code:
there is no handler to reach, so no role check to get wrong.
"""

from __future__ import annotations

from typing import Any

import pytest

from clinic_front_desk.deployment.app import build_memory_application
from clinic_front_desk.deployment.server import build_asgi_app_from_env, create_asgi_app

pytestmark = pytest.mark.integration

starlette_testclient = pytest.importorskip("starlette.testclient")
TestClient = starlette_testclient.TestClient

#: Everything a caller needs.
PUBLIC = ("/ping", "/voice", "/static/dashboard.css", "/static/voice_client.js")

#: Everything a doctor does. None of it may be reachable.
PRIVATE = (
    "/",
    "/slots?role=doctor",
    "/slots/patient/abc?role=doctor",
    "/documents?role=doctor",
    "/onboarding",
    "/dashboard/schedule?role=doctor",
    "/dashboard/activity?role=doctor",
    "/dashboard/metrics?role=doctor",
    "/dashboard/decisions?role=doctor",
    "/dashboard/calls/abc?role=doctor",
    "/dashboard/events?role=doctor",
    # The live-takeover console and its routes. These carry patient speech from a
    # call that is still in progress — the most sensitive thing this system serves —
    # and they let whoever holds the URL speak to a caller as the clinic.
    "/live?role=doctor",
    "/dashboard/live?role=doctor",
    "/dashboard/live/abc/transcript?role=doctor",
    "/dashboard/live/abc/takeover?role=doctor",
    "/dashboard/live/abc/release?role=doctor",
    "/dashboard/live/abc/say?role=doctor",
    "/invocations",
)


@pytest.fixture
def voice_client() -> Any:
    return TestClient(create_asgi_app(build_memory_application(), voice_only=True))


@pytest.fixture
def full_client() -> Any:
    return TestClient(create_asgi_app(build_memory_application()))


@pytest.mark.parametrize("path", PUBLIC)
def test_a_caller_can_reach_everything_they_need(voice_client: Any, path: str) -> None:
    assert voice_client.get(path).status_code == 200, path


@pytest.mark.parametrize("path", PRIVATE)
def test_no_dashboard_path_is_routed_at_all(voice_client: Any, path: str) -> None:
    """404, not 403. There is no handler, so there is no role check to bypass."""
    response = voice_client.get(path)

    assert response.status_code == 404, path


def test_patient_records_are_unreachable_even_with_a_doctor_role(
    voice_client: Any,
) -> None:
    """The one that matters: health data behind a guessable query parameter."""
    assert voice_client.get("/slots/patient/anything?role=doctor").status_code == 404


def test_posting_to_a_dashboard_path_is_also_unrouted(voice_client: Any) -> None:
    """A missing GET route with a live POST would be worse than useless."""
    for path in ("/slots?role=doctor", "/documents?role=doctor", "/onboarding"):
        assert voice_client.post(path, data={}).status_code == 404, path


def test_the_full_app_still_serves_the_dashboard(full_client: Any) -> None:
    """The default is unchanged: a local run keeps every portal."""
    assert full_client.get("/slots?role=doctor").status_code == 200
    assert full_client.get("/voice").status_code == 200


def test_the_env_flag_selects_the_voice_only_surface() -> None:
    """What the container actually reads, so a typo cannot silently expose it."""
    env = {"CLINIC_BACKEND": "memory", "CLINIC_VOICE_ONLY": "1"}
    client = TestClient(build_asgi_app_from_env(env))

    assert client.get("/voice").status_code == 200
    assert client.get("/slots?role=doctor").status_code == 404


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_anything_but_an_affirmative_serves_the_whole_app(value: str) -> None:
    """Fail safe toward the local default rather than half-exposing the dashboard."""
    env = {"CLINIC_BACKEND": "memory", "CLINIC_VOICE_ONLY": value}
    client = TestClient(build_asgi_app_from_env(env))

    assert client.get("/slots?role=doctor").status_code == 200, value


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " 1 "])
def test_the_flag_accepts_the_obvious_affirmatives(value: str) -> None:
    env = {"CLINIC_BACKEND": "memory", "CLINIC_VOICE_ONLY": value}
    client = TestClient(build_asgi_app_from_env(env))

    assert client.get("/slots?role=doctor").status_code == 404, value
