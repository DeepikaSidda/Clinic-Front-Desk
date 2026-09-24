"""A caller whose socket dies must not stay on the doctor's console.

Observed on the deployed instance: two calls listed as **in progress**, "Caller not
yet identified", "Nothing said yet" — and twenty minutes old. They were deploy-check
probes that completed the WebSocket upgrade and then dropped the TCP connection
without a close frame, so no disconnect message was ever delivered and the receive
loop blocked forever.

Two costs, both real. The doctor is shown a live call that does not exist and offered
a button to take over a dead line. And the Nova Sonic stream behind it stays open,
billing per second, until the process restarts.

The same thing happens to a real caller who shuts a laptop or loses signal, which is
why this is a server fix and not just a fix to the probe.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from clinic_front_desk.deployment import server as server_module
from clinic_front_desk.deployment.app import build_memory_application
from clinic_front_desk.deployment.server import create_asgi_app

pytestmark = pytest.mark.integration

starlette_testclient = pytest.importorskip("starlette.testclient")
TestClient = starlette_testclient.TestClient


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_a_socket_that_goes_quiet_ends_the_call() -> None:
    """The receive loop is bounded, so a silent socket stops the pump.

    Driven through ``_pump_client`` directly with a ``receive`` that never returns —
    exactly what a dropped connection looks like from the server's side. Without the
    bound this hangs until the test times out.
    """
    server = create_asgi_app(build_memory_application()).state.server

    class _Session:
        session_id = "dead-socket"

        class manager:  # noqa: N801 - stands in for the stream manager
            @staticmethod
            async def send_audio(*args: Any, **kwargs: Any) -> None:
                raise AssertionError("nothing should be forwarded")

            @staticmethod
            async def send_text(*args: Any, **kwargs: Any) -> None:
                raise AssertionError("nothing should be forwarded")

    async def never_returns() -> Any:
        await asyncio.sleep(3600)

    async def scenario() -> None:
        # A short bound so the test is fast; the production default is 60s.
        original = server_module.CALLER_IDLE_TIMEOUT_SECONDS
        server_module.CALLER_IDLE_TIMEOUT_SECONDS = 0.2
        try:
            await asyncio.wait_for(
                server._pump_client(_Session(), never_returns), timeout=10
            )
        finally:
            server_module.CALLER_IDLE_TIMEOUT_SECONDS = original

    # Returns rather than hanging: that is the whole property.
    _run(scenario())


def test_the_console_stops_listing_a_call_once_its_session_ends() -> None:
    """Whatever ends a call, the registry must not keep it.

    The console reads the registry, so an entry that outlives its socket is a call
    the doctor can see and try to take over.
    """
    server = create_asgi_app(build_memory_application()).state.server
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    server.live_calls.register("s1", send)
    assert [c["session_id"] for c in server.live_calls.list_calls()] == ["s1"]

    server.live_calls.unregister("s1")

    assert server.live_calls.list_calls() == []


def test_a_real_call_is_not_reaped_while_it_streams() -> None:
    """The bound must not hang up on a caller who is simply not speaking.

    The client's AudioWorklet posts a frame about every 32 ms for the whole call,
    silence included, so frames keep arriving while the caller thinks. A timeout that
    fired on conversational pauses would cut people off mid-call.
    """
    client = TestClient(create_asgi_app(build_memory_application()))

    with client.websocket_connect("/ws") as socket:
        first = socket.receive_json()
        assert first["message_type"] == "session_started"
        # Keep the socket fed the way the browser does, then hang up cleanly.
        for _ in range(3):
            socket.send_json(
                {
                    "message_type": "user_audio",
                    "audio": "",
                    "format": "pcm",
                    "sample_rate": 16000,
                    "channels": 1,
                }
            )
        socket.send_json({"message_type": "end_session"})
