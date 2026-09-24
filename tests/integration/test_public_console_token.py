"""Publishing the live console on a public host, without publishing the records.

Why this exists: live calls are tracked in memory, per process. A caller on the
public URL is registered inside that container, so a console running on a laptop is
looking at an empty list however much it is permitted to see — the call simply is not
there. Taking a real call means the console must be served by the process holding it.

That is a genuine problem, because the surface it publishes is the most sensitive one
here: speech from a call in progress, and the ability to talk to a caller as the
clinic. So it is opt-in via ``CLINIC_CONSOLE_TOKEN``, gated on a shared secret rather
than ``?role=``, and it publishes the live console *only* — stored patient records,
the calendar, documents and onboarding stay unrouted even with a valid token.

These tests hold that shape down. The valuable ones are the negatives.
"""

from __future__ import annotations

from typing import Any

import pytest

from clinic_front_desk.deployment.server import build_asgi_app_from_env

pytestmark = pytest.mark.integration

starlette_testclient = pytest.importorskip("starlette.testclient")
TestClient = starlette_testclient.TestClient

TOKEN = "s3cret-token-long-enough-to-pass-the-length-check"

#: Live-call paths the token is meant to open, and which render on their own.
CONSOLE = (
    "/live",
    "/dashboard/live",
)

#: Opened by the token too, but answers 404 for a call that is not in progress. Kept
#: separate so "404 because no such call" is never mistaken for "404 because
#: unrouted" — the whole lockdown rests on telling those apart.
CONSOLE_NEEDING_A_CALL = ("/dashboard/live/abc/transcript",)

#: Stored-record paths. These must stay shut *with* a valid token — the token buys
#: access to calls in progress, never to the clinic's history.
RECORDS = (
    "/",
    "/slots",
    "/slots/patient/abc",
    "/documents",
    "/onboarding",
    "/dashboard/schedule",
    "/dashboard/metrics",
    "/dashboard/decisions",
    "/dashboard/events",
    "/dashboard/calls/abc",
)


def client(token: str | None) -> Any:
    env = {"CLINIC_BACKEND": "memory", "CLINIC_VOICE_ONLY": "1"}
    if token is not None:
        env["CLINIC_CONSOLE_TOKEN"] = token
    return TestClient(build_asgi_app_from_env(env))


@pytest.fixture
def gated() -> Any:
    return client(TOKEN)


@pytest.mark.parametrize("path", CONSOLE)
def test_the_console_opens_with_the_right_token(gated: Any, path: str) -> None:
    assert gated.get(f"{path}?role=doctor&k={TOKEN}").status_code == 200, path


@pytest.mark.parametrize("path", CONSOLE + CONSOLE_NEEDING_A_CALL)
def test_the_console_is_shut_without_a_token(gated: Any, path: str) -> None:
    """The point of the gate. A judge with the bare link gets nothing."""
    assert gated.get(f"{path}?role=doctor").status_code == 403, path


@pytest.mark.parametrize("path", CONSOLE + CONSOLE_NEEDING_A_CALL)
def test_the_console_is_shut_with_a_wrong_token(gated: Any, path: str) -> None:
    assert gated.get(f"{path}?role=doctor&k=wrong").status_code == 403, path


@pytest.mark.parametrize("path", CONSOLE_NEEDING_A_CALL)
def test_the_token_reaches_the_handler_not_just_the_router(
    gated: Any, path: str
) -> None:
    """403 without the token, 404 with it: the route exists and the secret was read.

    Distinguishes an authorised miss from an unrouted path, which is the distinction
    the whole lockdown depends on.
    """
    assert gated.get(f"{path}?role=doctor").status_code == 403
    assert gated.get(f"{path}?role=doctor&k={TOKEN}").status_code == 404


@pytest.mark.parametrize("path", RECORDS)
def test_stored_records_stay_unrouted_even_with_a_valid_token(
    gated: Any, path: str
) -> None:
    """The one that limits the damage: the secret buys live calls, not history."""
    response = gated.get(f"{path}?role=doctor&k={TOKEN}")

    assert response.status_code == 404, path


def test_the_tool_surface_stays_unrouted_with_a_valid_token(gated: Any) -> None:
    """/invocations books, cancels and looks patients up without any speech."""
    assert (
        gated.post("/invocations", json={"prompt": "list patients"}).status_code == 404
    )


def test_a_caller_is_unaffected_by_the_gate(gated: Any) -> None:
    """The agent has to keep answering the phone for everyone, token or not."""
    assert gated.get("/voice").status_code == 200
    assert gated.get("/ping").status_code == 200


def test_taking_a_call_over_needs_the_token(gated: Any) -> None:
    """Writes, not just reads: this speaks to a caller as the clinic."""
    assert gated.post("/dashboard/live/abc/takeover?role=doctor").status_code == 403
    assert (
        gated.post(
            "/dashboard/live/abc/say?role=doctor", json={"text": "hello"}
        ).status_code
        == 403
    )


def test_the_doctor_talk_socket_needs_the_token(gated: Any) -> None:
    """Live patient audio outbound, and the doctor's voice inbound."""
    with pytest.raises(Exception):  # noqa: B017 - any refusal passes; accept fails
        with gated.websocket_connect("/dashboard/live/abc/talk?role=doctor") as socket:
            socket.receive_json()


def test_the_talk_socket_opens_with_the_token(gated: Any) -> None:
    """Otherwise the negative above would pass with the route simply deleted."""
    with gated.websocket_connect(
        f"/dashboard/live/abc/talk?role=doctor&k={TOKEN}"
    ) as socket:
        assert socket.receive_json()["message_type"] == "error"


def test_no_token_means_no_console_at_all() -> None:
    """The default is unchanged: not routed, which is stronger than guarded."""
    plain = client(None)

    assert plain.get("/live?role=doctor").status_code == 404
    assert plain.get("/dashboard/live?role=doctor").status_code == 404
    assert plain.get("/voice").status_code == 200


def test_a_short_token_is_refused_at_startup() -> None:
    """Fail loudly rather than publish live patient calls behind "abc".

    A typo'd or placeholder secret would otherwise look exactly like a working
    deployment, which is the worst possible outcome for this particular gate.
    """
    with pytest.raises(ValueError, match="at least 24 characters"):
        build_asgi_app_from_env(
            {
                "CLINIC_BACKEND": "memory",
                "CLINIC_VOICE_ONLY": "1",
                "CLINIC_CONSOLE_TOKEN": "abc",
            }
        )


def test_the_console_page_carries_the_token_into_its_own_requests(gated: Any) -> None:
    """A page that loads but whose polling 403s is worse than one that refuses.

    The console reads ``k`` from its own URL and appends it to every call it makes.
    Without that the page renders and then silently shows nothing.
    """
    body = gated.get(f"/live?role=doctor&k={TOKEN}").text

    assert 'params.get("k")' in body
    assert '"&k=" + encodeURIComponent(key)' in body
