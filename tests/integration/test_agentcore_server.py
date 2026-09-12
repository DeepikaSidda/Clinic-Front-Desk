"""AgentCore Runtime contract tests for the deployable container (task 14.1).

These drive the real ASGI app produced by
:func:`clinic_front_desk.deployment.server.create_asgi_app` through Starlette's
``TestClient``, asserting the container actually honours the Amazon Bedrock
AgentCore Runtime HTTP protocol contract:

- ``GET /ping`` reports ``Healthy``, and ``HealthyBusy`` while a call is up.
- ``POST /invocations`` dispatches the scheduled Practice_Intelligence run and
  the role-gated dashboard reads, returning contract-shaped native HTTP errors
  with the exception name in ``x-amzn-ErrorType``.
- ``WebSocket /ws`` runs one Call_Session end to end and persists its outcome.

Everything runs against in-memory fakes and a fake voice stream — no AWS, no
Bedrock, no network — so the deployable surface is verified in CI.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from clinic_front_desk.dashboard.role_gate import Role
from clinic_front_desk.deployment import build_memory_application
from clinic_front_desk.deployment.server import (
    PING_HEALTHY,
    PING_HEALTHY_BUSY,
    ROLE_HEADER,
    SESSION_ID_HEADER,
    AgentCoreServer,
    create_asgi_app,
    runtime_config_from_env,
)
from clinic_front_desk.models import (
    CallOutcome,
    ClinicKnowledgeBase,
    DayHours,
    Provider,
    ServiceConfig,
    is_ok,
)

pytestmark = pytest.mark.integration

starlette_testclient = pytest.importorskip(
    "starlette.testclient",
    reason="deploy extra not installed; run `pip install -e .[deploy]`",
)
TestClient = starlette_testclient.TestClient

PROVIDER_ID = "prov-1"
SERVICE = "Hearing Test"


class FakeVoiceStream:
    """A scripted :class:`VoiceStream` satisfying the Protocol (no Nova Sonic).

    ``events()`` blocks forever after the script is exhausted so the model-event
    task stays alive for the duration of the connection, which is what a real
    Nova Sonic stream does — that way the test exercises the "client hung up
    first" path rather than "model stream closed immediately".
    """

    def __init__(self, script: list[Any] | None = None, *, hold_open: bool = True) -> None:
        self._script = script or []
        self._hold_open = hold_open
        self.started = False
        self.closed = False
        self.sent_text: list[str] = []
        self.sent_audio: list[str] = []

    async def start(self) -> None:
        self.started = True

    async def send_audio(
        self, audio: str, *, format: str = "pcm", sample_rate: int = 16000, channels: int = 1
    ) -> None:
        self.sent_audio.append(audio)

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)

    async def stop_playback(self) -> None:
        return None

    async def events(self) -> AsyncIterator[Any]:
        for event in self._script:
            yield event
        while self._hold_open:
            await asyncio.sleep(0.01)

    async def close(self) -> None:
        self.closed = True


def _clinic() -> ClinicKnowledgeBase:
    return ClinicKnowledgeBase(
        location="123 Main St",
        hours={day: DayHours(open="09:00", close="17:00") for day in range(1, 6)},
        services=[ServiceConfig(name=SERVICE, price=150.0, prep_instructions="Bring ID.")],
        providers=[Provider(id=PROVIDER_ID, name="Dr. Reyes", specialty="ENT")],
        accepted_insurance=["Acme Health"],
    )


@pytest.fixture()
def stream() -> FakeVoiceStream:
    return FakeVoiceStream()


@pytest.fixture()
def client(stream: FakeVoiceStream) -> Any:
    """A TestClient over the real ASGI app on a configured in-memory application."""
    app = build_memory_application(stream=stream)
    saved = app.save_config(_clinic())
    assert saved.ok, saved.validation
    return TestClient(create_asgi_app(app))


# ---------------------------------------------------------------------------
# GET /ping
# ---------------------------------------------------------------------------


def test_ping_reports_healthy(client: Any) -> None:
    response = client.get("/ping")

    assert response.status_code == 200
    assert response.json() == {"status": PING_HEALTHY}


def test_ping_omits_time_of_last_update(client: Any) -> None:
    """The contract warns that advancing this on every ping breaks idle timeout."""
    assert "time_of_last_update" not in client.get("/ping").json()


def test_ping_reports_healthy_busy_while_a_call_is_in_flight() -> None:
    app = build_memory_application(stream=FakeVoiceStream())
    server = AgentCoreServer(app)
    asgi = create_asgi_app(app, server=server)

    with TestClient(asgi) as http:
        assert http.get("/ping").json()["status"] == PING_HEALTHY
        with http.websocket_connect("/ws") as socket:
            assert socket.receive_json()["message_type"] == "session_started"
            assert http.get("/ping").json()["status"] == PING_HEALTHY_BUSY


# ---------------------------------------------------------------------------
# POST /invocations — dispatch
# ---------------------------------------------------------------------------


def test_run_intelligence_action_returns_a_summary(client: Any) -> None:
    response = client.post("/invocations", json={"action": "run_intelligence"})

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "run_intelligence"
    # An unpopulated clinic yields no findings, so no Decisions and no failure.
    assert body["analysis_failed"] is False
    assert body["decisions_created"] == 0


def test_config_version_action_tracks_a_live_config_save(client: Any) -> None:
    before = client.post("/invocations", json={"action": "config_version"}).json()
    server = client.app.state.server
    saved = server.app.save_config(_clinic())
    assert saved.ok

    after = client.post("/invocations", json={"action": "config_version"}).json()

    assert after["config_version"] > before["config_version"]


def test_dashboard_shell_action_renders_the_role_scoped_shell(client: Any) -> None:
    granted = client.post(
        "/invocations", json={"action": "dashboard_shell", "role": Role.DOCTOR.value}
    ).json()
    denied = client.post("/invocations", json={"action": "dashboard_shell"}).json()

    assert 'data-view="schedule"' in granted["html"]
    assert 'data-role="access-denied"' in denied["html"]
    assert 'data-view="schedule"' not in denied["html"]


# ---------------------------------------------------------------------------
# POST /invocations — errors mapped onto the contract's native HTTP codes
# ---------------------------------------------------------------------------


def test_missing_action_is_a_validation_error(client: Any) -> None:
    response = client.post("/invocations", json={})

    assert response.status_code == 400
    assert response.headers["x-amzn-ErrorType"] == "ValidationException"


def test_unknown_action_is_a_validation_error(client: Any) -> None:
    response = client.post("/invocations", json={"action": "drop_all_tables"})

    assert response.status_code == 400
    assert response.headers["x-amzn-ErrorType"] == "ValidationException"
    assert "unknown action" in response.json()["message"]


def test_non_json_body_is_a_validation_error(client: Any) -> None:
    response = client.post(
        "/invocations", content=b"not json", headers={"content-type": "application/json"}
    )

    assert response.status_code == 400
    assert response.headers["x-amzn-ErrorType"] == "ValidationException"


def test_non_object_json_body_is_a_validation_error(client: Any) -> None:
    response = client.post("/invocations", json=[1, 2, 3])

    assert response.status_code == 400


def test_approve_decision_requires_a_decision_id(client: Any) -> None:
    response = client.post(
        "/invocations",
        json={"action": "approve_decision", "role": Role.DOCTOR.value},
    )

    assert response.status_code == 400
    assert "decision_id" in response.json()["message"]


# ---------------------------------------------------------------------------
# POST /invocations — role-scoped access (Req 15.5, 15.7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    ["open_decisions", "activity", "schedule"],
)
def test_no_role_is_denied_and_returns_no_data(client: Any, action: str) -> None:
    """Req 15.7: a viewer without a role gets no schedule/activity/metrics data."""
    response = client.post(
        "/invocations", json={"action": action, "provider_id": PROVIDER_ID}
    )

    assert response.status_code == 403
    assert response.headers["x-amzn-ErrorType"] == "AccessDeniedException"
    body = response.json()
    assert "decisions" not in body
    assert "entries" not in body
    assert "schedule" not in body


def test_doctor_role_may_read_the_decisions_feed(client: Any) -> None:
    response = client.post(
        "/invocations", json={"action": "open_decisions", "role": Role.DOCTOR.value}
    )

    assert response.status_code == 200
    assert response.json()["decisions"] == []


def test_assistant_role_may_read_activity_but_not_decisions(client: Any) -> None:
    """Req 15.5: only the views permitted for the assigned role are served."""
    activity = client.post(
        "/invocations", json={"action": "activity", "role": Role.ASSISTANT.value}
    )
    decisions = client.post(
        "/invocations", json={"action": "open_decisions", "role": Role.ASSISTANT.value}
    )

    assert activity.status_code == 200
    assert activity.json()["entries"] == []
    assert decisions.status_code == 403


def test_role_arrives_via_header_as_well_as_body(client: Any) -> None:
    response = client.post(
        "/invocations",
        json={"action": "open_decisions"},
        headers={ROLE_HEADER: Role.DOCTOR.value},
    )

    assert response.status_code == 200


def test_unknown_role_is_denied(client: Any) -> None:
    response = client.post(
        "/invocations", json={"action": "open_decisions", "role": "intern"}
    )

    assert response.status_code == 403


def test_schedule_defaults_to_the_current_day(client: Any) -> None:
    app = build_memory_application(stream=FakeVoiceStream())
    server = AgentCoreServer(app, today=lambda: "2026-03-04")
    http = TestClient(create_asgi_app(app, server=server))

    response = http.post(
        "/invocations",
        json={
            "action": "schedule",
            "role": Role.DOCTOR.value,
            "provider_id": PROVIDER_ID,
        },
    )

    assert response.status_code == 200
    assert response.json()["schedule"]["day"] == "2026-03-04"


def test_schedule_honours_an_explicit_day(client: Any) -> None:
    response = client.post(
        "/invocations",
        json={
            "action": "schedule",
            "role": Role.DOCTOR.value,
            "provider_id": PROVIDER_ID,
            "day": "2026-05-06",
            "services": [SERVICE],
        },
    )

    assert response.status_code == 200
    assert response.json()["schedule"]["day"] == "2026-05-06"


def test_activity_limit_must_be_a_non_negative_integer(client: Any) -> None:
    response = client.post(
        "/invocations",
        json={"action": "activity", "role": Role.DOCTOR.value, "limit": -5},
    )

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# WebSocket /ws — the Voice_Front_Desk transport
# ---------------------------------------------------------------------------


def test_ws_runs_a_call_session_and_persists_its_outcome(stream: FakeVoiceStream) -> None:
    app = build_memory_application(stream=stream)
    http = TestClient(create_asgi_app(app))

    with http.websocket_connect(
        "/ws", headers={SESSION_ID_HEADER: "runtime-session-7"}
    ) as socket:
        started = socket.receive_json()
        assert started["message_type"] == "session_started"
        # The AgentCore runtime session id becomes the Call_Session id.
        assert started["session_id"] == "runtime-session-7"
        socket.send_json({"message_type": "user_text", "text": "I'd like to book."})
        socket.send_json({"message_type": "end_session"})
        ended = socket.receive_json()

    assert ended["message_type"] == "session_ended"
    # No task completed, so the call is recorded as interrupted (Req 12.7).
    assert ended["outcome"] == CallOutcome.INTERRUPTED.value
    assert stream.sent_text == ["I'd like to book."]
    assert stream.closed is True

    persisted = app.stores.call_sessions.list_recent(10)
    assert is_ok(persisted)
    assert [record.id for record in persisted.value] == ["runtime-session-7"]
    assert persisted.value[0].outcome == CallOutcome.INTERRUPTED


def test_ws_forwards_base64_audio_from_a_json_frame(stream: FakeVoiceStream) -> None:
    app = build_memory_application(stream=stream)
    http = TestClient(create_asgi_app(app))

    with http.websocket_connect("/ws") as socket:
        socket.receive_json()
        socket.send_json({"message_type": "user_audio", "audio": "QUJD"})
        socket.send_json({"message_type": "end_session"})
        socket.receive_json()

    assert stream.sent_audio == ["QUJD"]


def test_ws_accepts_a_binary_frame_as_raw_pcm(stream: FakeVoiceStream) -> None:
    """A binary frame is base64-encoded for the stream so clients need not."""
    app = build_memory_application(stream=stream)
    http = TestClient(create_asgi_app(app))

    with http.websocket_connect("/ws") as socket:
        socket.receive_json()
        socket.send_bytes(b"ABC")
        socket.send_json({"message_type": "end_session"})
        socket.receive_json()

    assert stream.sent_audio == ["QUJD"]


class CancellingOnCloseStream(FakeVoiceStream):
    """A stream whose ``close()`` raises ``CancelledError``, as Nova Sonic's does.

    Tearing down the real Bedrock bidirectional event stream cancels an in-flight
    future, so ``close()`` raises ``asyncio.CancelledError``. Because that derives
    from ``BaseException``, a ``suppress(Exception)`` around teardown would let it
    escape and skip the Call_Session finalize — losing the outcome that Req 11.5
    and 12.7 require to be persisted on every session end.
    """

    async def close(self) -> None:
        self.closed = True
        raise asyncio.CancelledError


def test_ws_persists_the_outcome_even_when_stream_teardown_is_cancelled() -> None:
    """Regression (Req 11.5, 12.7): teardown cancellation must not lose the outcome."""
    stream = CancellingOnCloseStream()
    app = build_memory_application(stream=stream)
    http = TestClient(create_asgi_app(app))

    with http.websocket_connect(
        "/ws", headers={SESSION_ID_HEADER: "cancelled-teardown"}
    ) as socket:
        socket.receive_json()
        socket.send_json({"message_type": "end_session"})
        ended = socket.receive_json()

    assert ended["message_type"] == "session_ended"
    assert ended["outcome"] == CallOutcome.INTERRUPTED.value

    persisted = app.stores.call_sessions.list_recent(10)
    assert is_ok(persisted)
    assert [record.id for record in persisted.value] == ["cancelled-teardown"]
    assert persisted.value[0].outcome == CallOutcome.INTERRUPTED


def test_ws_forwards_barge_in_so_the_client_can_stop_playback() -> None:
    """Req 12.2 from the *patient's* side of the call.

    The stream manager stops producing audio within the budget, but a browser
    holding several buffered seconds would keep talking over the patient. The
    client needs to be told, so it can flush its queue.
    """
    from clinic_front_desk.voice.stream import BargeInDetected

    stream = FakeVoiceStream(
        [BargeInDetected(reason="user_speech")], hold_open=False
    )
    app = build_memory_application(stream=stream)
    http = TestClient(create_asgi_app(app))

    with http.websocket_connect("/ws") as socket:
        assert socket.receive_json()["message_type"] == "session_started"
        barge_in = socket.receive_json()

    assert barge_in["message_type"] == "barge_in"
    assert isinstance(barge_in["latency_ms"], (int, float))
    assert barge_in["within_budget"] is True


def test_ws_generates_a_session_id_when_the_header_is_absent(
    stream: FakeVoiceStream,
) -> None:
    app = build_memory_application(stream=stream)
    http = TestClient(create_asgi_app(app))

    with http.websocket_connect("/ws") as socket:
        started = socket.receive_json()
        socket.send_json({"message_type": "end_session"})
        socket.receive_json()

    assert started["session_id"]


# ---------------------------------------------------------------------------
# Environment-driven configuration
# ---------------------------------------------------------------------------


def test_runtime_config_reads_the_container_environment() -> None:
    config = runtime_config_from_env(
        {
            "CLINIC_TABLE_NAME": "clinic-prod",
            "AWS_REGION": "eu-west-1",
            "CLINIC_NOVA_SONIC_MODEL_ID": "amazon.nova-sonic-v1:0",
            "CLINIC_ANALYSIS_INTERVAL_HOURS": "12",
            "CLINIC_CREATE_TABLE_IF_MISSING": "true",
        }
    )

    assert config.table_name == "clinic-prod"
    assert config.region == "eu-west-1"
    assert config.nova_sonic_model_id == "amazon.nova-sonic-v1:0"
    assert config.analysis_interval_hours == 12.0
    assert config.create_table_if_missing is True


def test_deployed_model_id_matches_the_voice_adapter_default() -> None:
    """The deployed Nova Sonic id must be the one the adapter actually builds.

    Regression guard: the IAM policy grants exactly two foundation-model ARNs, so
    a drifted default here would fail a real call with AccessDeniedException, and
    the adapter only attaches the ``turn_detection`` provider config when the id
    is the v2 model — so a drift also silently drops barge-in endpointing.
    """
    from clinic_front_desk.voice.stream import NovaSonicVoiceStream

    assert (
        runtime_config_from_env({}).nova_sonic_model_id
        == NovaSonicVoiceStream.DEFAULT_MODEL_ID
    )


def test_voice_adapter_default_is_the_real_nova_sonic_v2_id() -> None:
    """Pin the real ids: v2 is ``amazon.nova-2-sonic-v1:0``, not ``nova-sonic-v2:0``."""
    from clinic_front_desk.voice.stream import NovaSonicVoiceStream

    assert NovaSonicVoiceStream.DEFAULT_MODEL_ID == "amazon.nova-2-sonic-v1:0"
    # The adapter's turn-detection gate keys off this substring (v2-only feature).
    assert "nova-2-sonic" in NovaSonicVoiceStream.DEFAULT_MODEL_ID


def test_runtime_config_falls_back_to_defaults_on_a_bad_interval() -> None:
    config = runtime_config_from_env({"CLINIC_ANALYSIS_INTERVAL_HOURS": "soon"})

    assert config.analysis_interval_hours == 24.0
    assert config.create_table_if_missing is False


# ---------------------------------------------------------------------------
# Call recording + transcript persistence
# ---------------------------------------------------------------------------


def _pcm(samples: int, value: int = 6000) -> bytes:
    return b"".join(int(value).to_bytes(2, "little", signed=True) for _ in range(samples))


def test_call_without_a_recording_store_records_no_audio() -> None:
    """Recording is opt-in: no configured store means no patient audio at all."""
    import base64

    stream = FakeVoiceStream()
    app = build_memory_application(stream=stream)  # record_calls defaults to False
    http = TestClient(create_asgi_app(app))

    assert app.stores.recordings is None

    with http.websocket_connect("/ws", headers={SESSION_ID_HEADER: "no-rec"}) as socket:
        socket.receive_json()
        socket.send_json(
            {
                "message_type": "user_audio",
                "audio": base64.b64encode(_pcm(1600)).decode(),
                "sample_rate": 16000,
            }
        )
        socket.send_json({"message_type": "end_session"})
        socket.receive_json()

    persisted = app.stores.call_sessions.list_recent(5)
    assert is_ok(persisted)
    session = persisted.value[0]
    assert session.recording_uri is None


def test_recorded_call_persists_audio_and_links_it_from_the_session() -> None:
    import base64

    from clinic_front_desk.voice.stream import InterpretedTurn

    stream = FakeVoiceStream(
        [InterpretedTurn(text="what are your hours", role="user")]
    )
    app = build_memory_application(stream=stream, record_calls=True)
    http = TestClient(create_asgi_app(app))

    with http.websocket_connect("/ws", headers={SESSION_ID_HEADER: "rec-1"}) as socket:
        socket.receive_json()
        socket.send_json(
            {
                "message_type": "user_audio",
                "audio": base64.b64encode(_pcm(3200)).decode(),
                "sample_rate": 16000,
            }
        )
        socket.send_json({"message_type": "end_session"})
        # Drain until the session ends (a transcript frame arrives first).
        while socket.receive_json()["message_type"] != "session_ended":
            pass

    # The audio is in the recording store...
    recordings = app.stores.recordings
    assert recordings is not None
    stored = recordings.get("rec-1")
    assert is_ok(stored)
    assert stored.value is not None
    assert stored.value.startswith(b"RIFF")  # a real WAV container

    # ...and the CallSession points at it, alongside the transcript and end time.
    persisted = app.stores.call_sessions.list_recent(5)
    assert is_ok(persisted)
    session = next(s for s in persisted.value if s.id == "rec-1")
    assert session.recording_uri == "memory://recordings/rec-1.wav"
    assert session.transcript is not None
    assert "what are your hours" in session.transcript
    assert session.ended_at is not None


def test_transcript_is_captured_even_without_audio_recording() -> None:
    """The transcript is text on a record the doctor already sees; audio is not."""
    from clinic_front_desk.voice.stream import InterpretedTurn

    stream = FakeVoiceStream(
        [InterpretedTurn(text="i would like to book a hearing test", role="user")]
    )
    app = build_memory_application(stream=stream)
    http = TestClient(create_asgi_app(app))

    with http.websocket_connect("/ws", headers={SESSION_ID_HEADER: "tx-1"}) as socket:
        socket.receive_json()
        socket.send_json({"message_type": "end_session"})
        while socket.receive_json()["message_type"] != "session_ended":
            pass

    persisted = app.stores.call_sessions.list_recent(5)
    assert is_ok(persisted)
    session = next(s for s in persisted.value if s.id == "tx-1")
    assert session.transcript is not None
    assert "book a hearing test" in session.transcript
    assert session.recording_uri is None


def test_a_failed_recording_upload_does_not_lose_the_call() -> None:
    """Req 16.6: losing a recording must never lose the Call_Session record."""
    import base64

    from clinic_front_desk.models import Err, StoreError, StoreErrorKind

    class BrokenRecordingStore:
        def put(self, *args: Any, **kwargs: Any) -> Any:
            return Err(
                StoreError(
                    kind=StoreErrorKind.STORE_FAILURE,
                    detail="bucket on fire",
                    store="Broken",
                )
            )

        def get(self, call_session_id: str) -> Any:
            return Err(
                StoreError(
                    kind=StoreErrorKind.STORE_FAILURE, detail="nope", store="Broken"
                )
            )

        def playback_url(self, call_session_id: str, **kwargs: Any) -> Any:
            return Err(
                StoreError(
                    kind=StoreErrorKind.STORE_FAILURE, detail="nope", store="Broken"
                )
            )

    app = build_memory_application(stream=FakeVoiceStream(), record_calls=True)
    object.__setattr__(app.stores, "recordings", BrokenRecordingStore())
    http = TestClient(create_asgi_app(app))

    with http.websocket_connect("/ws", headers={SESSION_ID_HEADER: "fail-1"}) as socket:
        socket.receive_json()
        socket.send_json(
            {
                "message_type": "user_audio",
                "audio": base64.b64encode(_pcm(1600)).decode(),
                "sample_rate": 16000,
            }
        )
        socket.send_json({"message_type": "end_session"})
        ended = socket.receive_json()

    assert ended["message_type"] == "session_ended"
    persisted = app.stores.call_sessions.list_recent(5)
    assert is_ok(persisted)
    session = next(s for s in persisted.value if s.id == "fail-1")
    # The call survived; only the recording pointer is absent.
    assert session.outcome == CallOutcome.INTERRUPTED
    assert session.recording_uri is None


# ---------------------------------------------------------------------------
# Recording consent notice
# ---------------------------------------------------------------------------


def test_recording_enabled_attaches_the_consent_notice_to_the_prompt() -> None:
    """Recording without telling the caller is unlawful where all-party consent
    applies, so enabling a recording store must not be able to skip the notice."""
    from clinic_front_desk.voice import RECORDING_NOTICE_INSTRUCTION

    app = build_memory_application(stream=FakeVoiceStream(), record_calls=True)
    agent = app.new_voice_agent()

    assert RECORDING_NOTICE_INSTRUCTION in agent.system_prompt
    prompt = agent.system_prompt.lower()
    assert "call recording (this call is being recorded)" in prompt
    # Announced once, up front, and objecting routes to a human.
    assert "first reply" in prompt
    assert "escalate" in prompt


def test_recording_disabled_omits_the_consent_notice() -> None:
    """The agent must never claim a call is recorded when it is not."""
    from clinic_front_desk.voice import RECORDING_NOTICE_INSTRUCTION

    app = build_memory_application(stream=FakeVoiceStream())
    agent = app.new_voice_agent()

    assert RECORDING_NOTICE_INSTRUCTION not in agent.system_prompt
    # Nothing may suggest the *call* is being recorded. Matched on phrases about
    # the call rather than the bare word "recorded", which the prompt now also
    # uses for intake fields recorded on a patient record — a different meaning
    # that would make this assertion fire on unrelated wording.
    lowered = agent.system_prompt.lower()
    for phrase in (
        "call is recorded",
        "call is being recorded",
        "this call is recorded",
        "recording this call",
        "the call is recorded",
    ):
        assert phrase not in lowered, phrase


def test_consent_notice_is_added_even_to_a_custom_prompt() -> None:
    """The obligation follows the recording, not the prompt the caller chose."""
    from clinic_front_desk.voice import RECORDING_NOTICE_INSTRUCTION

    app = build_memory_application(
        stream=FakeVoiceStream(), record_calls=True, system_prompt="Custom prompt."
    )
    agent = app.new_voice_agent()

    assert agent.system_prompt.startswith("Custom prompt.")
    assert RECORDING_NOTICE_INSTRUCTION in agent.system_prompt


def test_the_prompt_makes_the_agent_spell_the_name_back() -> None:
    """Names are the least reliable thing a phone line captures.

    A caller who said "Sidda Deepika" was booked as "siddha devika". Prompt
    wording cannot guarantee correct recognition, but reading the name back and
    spelling it is what a human receptionist does, and it is the one field
    everything else is filed under.
    """
    from clinic_front_desk.voice import ADMINISTRATIVE_ONLY_SYSTEM_PROMPT

    prompt = ADMINISTRATIVE_ONLY_SYSTEM_PROMPT
    lowered = prompt.lower()

    assert "read the name back" in lowered
    assert "spell" in lowered
    # Their spelling outranks the transcription.
    assert "their spelling always wins" in lowered
    # The mobile number too, since it is the other identifier.
    assert "read the mobile number back" in lowered


def test_the_prompt_keeps_names_readable_but_health_details_silent() -> None:
    """The two rules pull in opposite directions and must not be confused.

    A name is not private from the person who just said it, and confirming it is
    the only defence against a mishearing. A blood group read aloud can be
    overheard by whoever else is in the room.
    """
    from clinic_front_desk.voice import ADMINISTRATIVE_ONLY_SYSTEM_PROMPT

    lowered = ADMINISTRATIVE_ONLY_SYSTEM_PROMPT.lower()

    assert "do not read blood group, weight or height back" in lowered
    assert "read the name back" in lowered
    # The prompt says so explicitly, so the model is not left inferring it.
    assert "opposite of the rule for blood group" in lowered


def test_the_prompt_forbids_dressing_the_offer_up_as_clinical_judgement() -> None:
    """Observed on a live call, after the consultation offer started working.

    The agent said: "For an itching sensation inside your nose, the safest option
    is to book an ENT Consultation." The outcome was right and the wording was not.
    "Safest option" claims a risk assessment it never made, and naming the symptom
    as the reason makes a symptom-independent answer look tailored — a clinical
    opinion in everything but name.
    """
    from clinic_front_desk.voice import ADMINISTRATIVE_ONLY_SYSTEM_PROMPT

    lowered = ADMINISTRATIVE_ONLY_SYSTEM_PROMPT.lower()

    assert "safest option" in lowered
    assert "the doctor decides" in lowered
    # The permitted reason, and the forbidden framings, are both stated.
    assert "same answer for every symptom" in lowered
    assert "isn't sure which service they need" in lowered
