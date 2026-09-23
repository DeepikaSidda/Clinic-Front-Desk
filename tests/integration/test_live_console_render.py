"""The live console actually renders its buttons.

Why this exists: the console shipped with a button row that was never populated, so
a call could not be picked up at all — and the test suite was green. A test asserting
the page *contained* ``data-talk`` passed throughout, because the function that
builds the row was present in the source and simply never ran. The bug was a
state-transition guard whose "unset" sentinel was ``""``, which is also the
not-taken-over value, so the first update saw no change and wrote nothing.

String assertions on a page of JavaScript cannot catch that. This runs the real
script under Node with a small DOM stub and looks at what comes out.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from clinic_front_desk.deployment.app import build_memory_application
from clinic_front_desk.deployment.server import create_asgi_app

pytestmark = pytest.mark.integration

starlette_testclient = pytest.importorskip("starlette.testclient")
TestClient = starlette_testclient.TestClient

HARNESS = Path(__file__).resolve().parents[1] / "js" / "check_live_console.mjs"


@pytest.fixture(scope="module")
def rendered() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; cannot execute the console script")
    if not HARNESS.exists():
        pytest.skip(f"harness missing: {HARNESS}")

    client = TestClient(create_asgi_app(build_memory_application()))
    response = client.get("/live?role=doctor")
    assert response.status_code == 200

    # tmp_path is function-scoped; this fixture is module-scoped to run node once.
    page = HARNESS.parent / "_live_page.html"
    page.write_text(response.text, encoding="utf-8")
    try:
        result = subprocess.run(
            [node, str(HARNESS), str(page)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        page.unlink(missing_ok=True)

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert "error" not in data, data["error"]
    return data


def test_a_waiting_call_offers_a_way_to_pick_it_up(rendered: dict) -> None:
    """The regression. An empty row here means the page cannot answer a call."""
    waiting = rendered["waiting"]

    assert waiting["html"].strip(), (
        "the button row rendered empty for a call needing a person — "
        "there is no way to pick the call up"
    )
    assert waiting["hasTalk"], waiting["html"]
    assert waiting["hasTake"], waiting["html"]


def test_a_call_in_hand_keeps_voice_typing_and_a_way_out(rendered: dict) -> None:
    live = rendered["live"]

    assert live["hasTalk"], live["html"]
    assert live["hasSay"], live["html"]
    assert live["hasRelease"], live["html"]


def test_the_controls_survive_a_second_poll(rendered: dict) -> None:
    """The harness calls updateCard twice; the row must not be wiped.

    The console polls every two seconds. An earlier version rebuilt the row on every
    poll, which destroyed the text box while the doctor was typing in it. The fix
    introduced the guard that then broke the first render — so both halves need
    holding down at once.
    """
    assert rendered["waiting"]["html"].strip()
    assert rendered["live"]["html"].strip()
