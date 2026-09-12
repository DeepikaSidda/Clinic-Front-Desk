"""Integration tests for the served dashboard UI (Req 1.1, 9.6, 14, 15).

These drive the real ASGI app through Starlette's ``TestClient``, asserting that
the dashboard is actually *served* — the page assembles every permitted region,
each component's ``data-*-endpoint`` resolves to a live partial, the decisions
feed's JSON and approve/dismiss endpoints work, the change stream fans out, and
the role gate is enforced identically on the page, every partial, and the stream.

Everything runs against in-memory fakes and a fake voice stream — no AWS, no
network.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from clinic_front_desk.dashboard.role_gate import Role
from clinic_front_desk.deployment import build_memory_application
from clinic_front_desk.deployment.dashboard_app import (
    STATIC_FILES,
    ChangeEventStream,
    DashboardWebApp,
)
from clinic_front_desk.deployment.server import ROLE_HEADER, create_asgi_app
from clinic_front_desk.models import (
    Appointment,
    AppointmentStatus,
    CallOutcome,
    CallSession,
    ClinicKnowledgeBase,
    DayHours,
    Decision,
    DecisionKind,
    DecisionStatus,
    Escalation,
    EscalationReason,
    PatientRef,
    Provider,
    ServiceConfig,
    Slot,
    SlotStatus,
    WaitlistEntry,
    is_ok,
)

pytestmark = pytest.mark.integration

starlette_testclient = pytest.importorskip(
    "starlette.testclient",
    reason="deploy extra not installed; run `pip install -e .[deploy]`",
)
TestClient = starlette_testclient.TestClient

PROVIDER = "prov-1"
SERVICE = "Hearing Test"
DAY = "2026-03-04"
DOCTOR = {ROLE_HEADER: Role.DOCTOR.value}
ASSISTANT = {ROLE_HEADER: Role.ASSISTANT.value}


class FakeVoiceStream:
    """A no-op :class:`VoiceStream` so composition needs no Nova Sonic."""

    async def start(self) -> None:
        return None

    async def send_audio(
        self, audio: str, *, format: str = "pcm", sample_rate: int = 16000, channels: int = 1
    ) -> None:
        return None

    async def send_text(self, text: str) -> None:
        return None

    async def stop_playback(self) -> None:
        return None

    async def events(self) -> AsyncIterator[Any]:
        while True:
            await asyncio.sleep(0.01)
            yield None

    async def close(self) -> None:
        return None


def _clinic() -> ClinicKnowledgeBase:
    return ClinicKnowledgeBase(
        location="118 Harbour Road",
        hours={day: DayHours(open="09:00", close="17:00") for day in range(1, 6)},
        services=[
            ServiceConfig(name=SERVICE, price=180.0, prep_instructions="Bring ID.")
        ],
        providers=[Provider(id=PROVIDER, name="Dr. Amara Reyes", specialty="ENT")],
        accepted_insurance=["Acme Health"],
    )


def _decision(decision_id: str, **overrides: Any) -> Decision:
    fields: dict[str, Any] = {
        "id": decision_id,
        "kind": DecisionKind.GAP_FILL,
        "finding_key": f"fk-{decision_id}",
        "summary": "An 11:30 slot is unfilled while 2 patients wait.",
        "recommended_action": "Book the earliest waiting patient.",
        "generated_at": "2026-03-04T09:00:00Z",
        "supporting_record_count": 7,
        "action_payload": {"slot_id": "slot-open"},
    }
    fields.update(overrides)
    return Decision(**fields)


def _seed(app: Any) -> None:
    """Seed a configured clinic with a day of schedule, activity, and decisions."""
    saved = app.save_config(_clinic())
    assert saved.ok, saved.validation

    app.stores.appointments.create(
        Appointment(
            id="appt-1",
            provider_id=PROVIDER,
            patient_id="pat-1",
            service=SERVICE,
            slot_id="slot-booked",
            date=DAY,
            time="09:00",
            status=AppointmentStatus.BOOKED,
        )
    )
    app.stores.appointments.seed_slot(
        Slot(
            id="slot-open",
            provider_id=PROVIDER,
            service=SERVICE,
            start=f"{DAY}T11:30:00Z",
            end=f"{DAY}T12:15:00Z",
            status=SlotStatus.OPEN,
        )
    )
    app.stores.waitlist.add(
        WaitlistEntry(
            id="wl-1",
            patient_id="pat-waiting",
            service=SERVICE,
            preferred_slot_type="any",
            added_at="2026-03-01T09:00:00Z",
            seq=0,
        )
    )
    app.stores.call_sessions.create(
        CallSession(
            id="call-1",
            started_at="2026-03-04T08:00:00Z",
            ended_at="2026-03-04T08:04:00Z",
            outcome=CallOutcome.BOOKED,
            patient_ref=PatientRef(name="Dana Ellis", callback_phone="555-0100"),
        )
    )
    app.stores.escalations.create(
        Escalation(
            id="esc-1",
            reason=EscalationReason.CLINICAL_CONTENT,
            call_session_id="call-1",
            context="Caller described symptoms.",
            created_at="2026-03-04T08:10:00Z",
            patient_ref=PatientRef(name="Marta Silva"),
        )
    )
    app.stores.decisions.create(_decision("dec-1"))


@pytest.fixture()
def app() -> Any:
    composed = build_memory_application(stream=FakeVoiceStream())
    _seed(composed)
    return composed


@pytest.fixture()
def client(app: Any) -> Any:
    return TestClient(create_asgi_app(app))


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def test_page_assembles_every_region_for_the_doctor(client: Any) -> None:
    """The page arrives complete — each permitted region already filled."""
    response = client.get("/", headers=DOCTOR)
    body = response.text

    assert response.status_code == 200
    for view in ("schedule", "call_activity", "impact_metrics", "decisions"):
        assert f'data-view="{view}"' in body
    # Each region's component actually rendered into it, not just an empty mount.
    assert 'data-component="schedule-view"' in body
    assert 'data-component="call-activity-log"' in body
    assert 'data-component="impact-metrics-strip"' in body
    assert 'data-component="decisions-feed"' in body
    # No region is left as a bare mount point. This is the assertion that caught
    # the shell emitting its body twice (once inside a comment): the components
    # were present in the commented copy while every visible region stayed empty.
    assert '<div class="dashboard-shell__view-body" data-role="view-mount"></div>' not in body


def test_shell_emits_each_region_exactly_once(client: Any) -> None:
    """Regression: an unbounded token replace duplicated the whole shell body."""
    body = client.get("/", headers=DOCTOR).text

    for view in ("schedule", "call_activity", "impact_metrics", "decisions"):
        assert body.count(f'data-view="{view}"') == 1


def test_page_links_the_stylesheet_and_controllers(client: Any) -> None:
    body = client.get("/", headers=DOCTOR).text

    assert '<link rel="stylesheet" href="/static/dashboard.css" />' in body
    for script in (
        "decisions_feed.js",
        "schedule_view.js",
        "activity_metrics.js",
        "dashboard_bootstrap.js",
    ):
        assert f'src="/static/{script}"' in body
    # The bootstrap must load last so the controllers it wires are defined.
    assert body.index("dashboard_bootstrap.js") > body.index("decisions_feed.js")


def test_page_passes_the_role_and_event_endpoint_to_the_client(client: Any) -> None:
    body = client.get("/", headers=DOCTOR).text

    assert '<meta name="clinic-role" content="doctor" />' in body
    assert '<meta name="clinic-events-endpoint" content="/dashboard/events" />' in body


def test_page_strips_developer_comments(client: Any) -> None:
    """Task/requirement notes in the partials are not shipped to the browser."""
    body = client.get("/", headers=DOCTOR).text

    assert "<!--" not in body
    assert "task 13.5" not in body


def test_page_preserves_the_embedded_feed_json_through_comment_stripping() -> None:
    """A summary containing a comment token must not corrupt the embedded JSON.

    The feed renderer escapes ``</`` but not ``<!--``, and decision summaries carry
    patient-influenced text — so comment stripping has to leave the embedded
    view-model untouched or the feed would hydrate from truncated JSON.
    """
    composed = build_memory_application(stream=FakeVoiceStream())
    _seed(composed)
    composed.stores.decisions.create(
        _decision(
            "dec-tricky",
            finding_key="fk-tricky",
            summary="Demand for <!-- odd --> service",
            generated_at="2026-03-04T10:00:00Z",
        )
    )
    http = TestClient(create_asgi_app(composed))

    body = http.get("/", headers=DOCTOR).text

    start = body.index('<script id="decisions-feed-data"')
    payload = body[body.index(">", start) + 1 : body.index("</script>", start)]
    feed = json.loads(payload)
    assert {card["id"] for card in feed["cards"]} == {"dec-1", "dec-tricky"}


def test_page_redirects_to_onboarding_when_unconfigured() -> None:
    """Req 1.1: first access with no configuration presents onboarding."""
    composed = build_memory_application(stream=FakeVoiceStream())
    http = TestClient(create_asgi_app(composed))

    response = http.get("/", headers=DOCTOR, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/onboarding"


def test_onboarding_page_is_styled_and_served(client: Any) -> None:
    response = client.get("/onboarding")

    assert response.status_code == 200
    assert '<link rel="stylesheet" href="/static/dashboard.css" />' in response.text
    assert 'src="/static/onboarding_wizard.js"' in response.text
    assert 'id="onboarding-wizard"' in response.text


# ---------------------------------------------------------------------------
# Role-scoped access (Req 15.5, 15.7)
# ---------------------------------------------------------------------------


def test_assistant_sees_only_its_permitted_regions(client: Any) -> None:
    """Req 15.5: only the views permitted for the assigned role are presented."""
    body = client.get("/", headers=ASSISTANT).text

    assert 'data-view="schedule"' in body
    assert 'data-view="call_activity"' in body
    assert 'data-view="impact_metrics"' not in body
    assert 'data-view="decisions"' not in body


def test_no_role_gets_the_denied_page_with_no_data_regions(client: Any) -> None:
    """Req 15.7: a viewer with no role is shown no schedule/activity/metrics."""
    body = client.get("/").text

    assert 'data-role="access-denied"' in body
    for view in ("schedule", "call_activity", "impact_metrics", "decisions"):
        assert f'data-view="{view}"' not in body
    # No component rendered at all, so there is no data to leak.
    assert 'data-component="schedule-view"' not in body
    assert "555-0100" not in body
    assert "appt-1" not in body


@pytest.mark.parametrize(
    "path",
    [
        "/dashboard/schedule",
        "/dashboard/activity",
        "/dashboard/metrics",
        "/dashboard/decisions",
        "/dashboard/events",
    ],
)
def test_partials_deny_a_viewer_without_a_role(client: Any, path: str) -> None:
    """The gate is enforced on every live endpoint, not just the page."""
    response = client.get(path)

    assert response.status_code == 403
    assert response.headers["x-amzn-ErrorType"] == "AccessDeniedException"


@pytest.mark.parametrize(
    "path", ["/dashboard/metrics", "/dashboard/decisions"]
)
def test_assistant_is_denied_the_doctor_only_endpoints(client: Any, path: str) -> None:
    assert client.get(path, headers=ASSISTANT).status_code == 403


@pytest.mark.parametrize("path", ["/dashboard/schedule", "/dashboard/activity"])
def test_assistant_is_allowed_its_own_endpoints(client: Any, path: str) -> None:
    assert client.get(path, headers=ASSISTANT).status_code == 200


def test_role_may_come_from_the_query_for_local_runs(client: Any) -> None:
    assert client.get("/dashboard/decisions?role=doctor").status_code == 200


# ---------------------------------------------------------------------------
# Live partials — the endpoints the controllers re-fetch
# ---------------------------------------------------------------------------


def test_schedule_partial_defaults_to_the_configured_provider_and_today(
    app: Any,
) -> None:
    """Req 15.1: the current day for the provider, with no parameters needed."""
    dashboard = DashboardWebApp(app)
    partial = dashboard.schedule_partial(Role.DOCTOR.value)

    assert f'data-provider-id="{PROVIDER}"' in partial
    assert f'data-day="{dashboard.today()}"' in partial


def test_schedule_partial_serves_a_selected_day(client: Any) -> None:
    """Req 15.6: selecting another day returns that day's schedule."""
    response = client.get(
        f"/dashboard/schedule?provider_id={PROVIDER}&day={DAY}", headers=DOCTOR
    )

    assert response.status_code == 200
    assert f'data-day="{DAY}"' in response.text
    # The seeded appointment and open slot for that day are both rendered.
    assert 'data-appointment-id="appt-1"' in response.text
    assert 'data-slot-id="slot-open"' in response.text


def test_activity_partial_renders_calls_and_escalations(client: Any) -> None:
    """Req 15.2, 9.6: the log carries both call outcomes and escalations."""
    body = client.get("/dashboard/activity", headers=DOCTOR).text

    assert 'data-component="call-activity-log"' in body
    assert 'data-interaction="booked"' in body
    assert 'data-interaction="escalated"' in body
    # The aggregation identifies a patient by callback phone when present, which
    # is the stable identifier Req 15.2 asks each entry to expose.
    assert "555-0100" in body


def test_activity_partial_honours_a_limit(client: Any) -> None:
    body = client.get("/dashboard/activity?limit=0", headers=DOCTOR).text

    assert 'data-component="call-activity-log"' in body
    assert "555-0100" not in body


def test_activity_partial_rejects_a_negative_limit(client: Any) -> None:
    assert client.get("/dashboard/activity?limit=-1", headers=DOCTOR).status_code == 400


@pytest.mark.parametrize("window", [7, 30, 90])
def test_metrics_partial_serves_each_supported_window(client: Any, window: int) -> None:
    """Req 15.3: the 7/30/90-day period selector drives this endpoint."""
    response = client.get(f"/dashboard/metrics?window={window}", headers=DOCTOR)

    assert response.status_code == 200
    assert f'data-window-days="{window}"' in response.text
    assert 'data-role="hours-saved"' in response.text
    assert 'data-role="no-show-trend"' in response.text


@pytest.mark.parametrize("window", ["1", "45", "abc"])
def test_metrics_partial_rejects_an_unsupported_window(client: Any, window: str) -> None:
    assert client.get(f"/dashboard/metrics?window={window}", headers=DOCTOR).status_code == 400


def test_metrics_counts_approved_gap_fills_as_recovered(app: Any) -> None:
    """Req 15.3: the recovered count needs *approved* decisions.

    Approving moves a Decision out of the open feed, so this is precisely the read
    ``DecisionStore.list_by_status`` exists to serve — a regression here would
    silently report zero recovered appointments forever.
    """
    dashboard = DashboardWebApp(app)
    before = dashboard.impact_metrics(90).waitlist_recovered_count

    app.stores.decisions.create(
        _decision("dec-recovered", finding_key="fk-recovered")
    )
    resolved = app.stores.decisions.set_status(
        "dec-recovered", DecisionStatus.APPROVED, "2026-03-04T09:30:00Z"
    )
    assert is_ok(resolved)

    after = DashboardWebApp(
        app, now=lambda: __import__("datetime").datetime.fromisoformat(
            "2026-03-04T12:00:00+00:00"
        )
    ).impact_metrics(90)
    assert after.waitlist_recovered_count == before + 1


# ---------------------------------------------------------------------------
# Decisions feed JSON + approve/dismiss
# ---------------------------------------------------------------------------


def test_decisions_endpoint_returns_the_feed_view_model(client: Any) -> None:
    response = client.get("/dashboard/decisions", headers=DOCTOR)

    assert response.status_code == 200
    feed = response.json()
    assert [card["id"] for card in feed["cards"]] == ["dec-1"]
    assert feed["cards"][0]["supporting_record_count"] == 7


def test_approving_a_gap_fill_books_from_the_waitlist(client: Any, app: Any) -> None:
    """Req 14.3, 8.2-8.4: approve executes the action through the Data_Layer."""
    response = client.post("/dashboard/decisions/dec-1/approve", headers=DOCTOR)

    assert response.status_code == 200
    assert response.json()["outcome"] == "approved"
    # The waitlisted patient was booked and their entry removed.
    booked = app.stores.appointments.list_by_patient("pat-waiting")
    assert is_ok(booked)
    assert len(booked.value) == 1
    remaining = app.stores.waitlist.list_by_service_ordered(SERVICE)
    assert is_ok(remaining)
    assert remaining.value == []
    # And it left the open feed (Req 14.5).
    assert client.get("/dashboard/decisions", headers=DOCTOR).json()["cards"] == []


def test_dismissing_a_decision_executes_no_action(client: Any, app: Any) -> None:
    """Req 14.4: dismiss records the status and performs no action."""
    response = client.post("/dashboard/decisions/dec-1/dismiss", headers=DOCTOR)

    assert response.json()["outcome"] == "dismissed"
    booked = app.stores.appointments.list_by_patient("pat-waiting")
    assert is_ok(booked)
    assert booked.value == []


def test_resolving_an_unknown_decision_reports_not_found(client: Any) -> None:
    """A 200 with a non-resolved outcome: the card is restored with the error."""
    response = client.post("/dashboard/decisions/nope/approve", headers=DOCTOR)

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "not_found"
    assert body["error"]


def test_unknown_decision_action_is_rejected(client: Any) -> None:
    assert client.post("/dashboard/decisions/dec-1/destroy", headers=DOCTOR).status_code == 400


# ---------------------------------------------------------------------------
# Change stream
# ---------------------------------------------------------------------------


"""
The SSE endpoint is an endless response, which a blocking test client cannot
drive without deadlocking on close, so the stream is exercised through
:class:`ChangeEventStream` directly. Its HTTP wrapper is covered by the
role-gate test above (an unauthenticated viewer must not receive a live feed of
clinic mutations) and was verified end to end against a running server.
"""


def test_event_stream_fans_out_a_mutation(app: Any) -> None:
    """A store mutation reaches a connected client as an SSE ``change`` frame."""

    async def scenario() -> list[bytes]:
        stream = ChangeEventStream(app.channel, asyncio.get_running_loop())
        frames = stream.events(max_frames=2)
        first = await anext(frames)
        # Mutating through the store is what emits on the shared channel.
        app.stores.decisions.create(_decision("dec-live", finding_key="fk-live"))
        second = await anext(frames)
        return [first, second]

    connected, change = asyncio.run(scenario())

    assert connected == b": connected\n\n"
    assert change.startswith(b"event: change\ndata: ")
    payload = json.loads(change.split(b"data: ", 1)[1])
    assert payload == {"entity": "decision", "id": "dec-live", "kind": "created"}


def test_event_stream_unsubscribes_when_the_client_goes_away(app: Any) -> None:
    """A finished stream must not leak a channel subscription."""
    before = app.channel.subscriber_count

    async def scenario() -> int:
        stream = ChangeEventStream(app.channel, asyncio.get_running_loop())
        during = app.channel.subscriber_count
        # Draining to max_frames runs the generator's finally, which unsubscribes.
        async for _ in stream.events(max_frames=1):
            pass
        return during

    during = asyncio.run(scenario())

    assert during == before + 1
    assert app.channel.subscriber_count == before


def test_event_stream_drops_rather_than_blocking_a_store_write(app: Any) -> None:
    """A wedged client must not apply backpressure to a booking.

    The channel broadcasts synchronously inside the store write, so a full queue
    has to drop. Blocking here would stall the mutation itself.
    """

    async def scenario() -> tuple[int, bool]:
        stream = ChangeEventStream(app.channel, asyncio.get_running_loop(), max_queue=1)
        for index in range(4):
            app.stores.decisions.create(
                _decision(f"dec-flood-{index}", finding_key=f"fk-flood-{index}")
            )
        # Let the queued call_soon_threadsafe callbacks run.
        await asyncio.sleep(0)
        writes_succeeded = is_ok(app.stores.decisions.list_open())
        stream.close()
        return stream.dropped, writes_succeeded

    dropped, writes_succeeded = asyncio.run(scenario())

    assert dropped >= 1
    assert writes_succeeded


def test_event_stream_route_sets_no_buffering_headers(app: Any) -> None:
    """Proxy buffering would defer events past the budgets (Req 14.8, 15.4).

    Asserted on the response object without consuming the endless body.
    """
    from clinic_front_desk.deployment.server import AgentCoreServer

    server = AgentCoreServer(app)
    asgi = create_asgi_app(app, server=server)
    route = next(r for r in asgi.routes if getattr(r, "path", None) == "/dashboard/events")

    async def call() -> Any:
        from starlette.requests import Request

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/dashboard/events",
            "query_string": b"role=doctor",
            "headers": [],
            "app": asgi,
        }
        response = await route.endpoint(Request(scope))
        return response

    response = asyncio.run(call())

    assert response.media_type == "text/event-stream"
    assert response.headers["x-accel-buffering"] == "no"
    assert "no-cache" in response.headers["cache-control"]


# ---------------------------------------------------------------------------
# Static assets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(STATIC_FILES))
def test_every_allow_listed_static_asset_is_served(client: Any, name: str) -> None:
    response = client.get(f"/static/{name}")

    assert response.status_code == 200
    assert response.text
    if name.endswith(".css"):
        assert response.headers["content-type"].startswith("text/css")
    else:
        assert response.headers["content-type"].startswith("text/javascript")


@pytest.mark.parametrize(
    "name", ["server.py", "../server.py", "..%2Fapp.py", "secrets.env"]
)
def test_static_route_serves_only_allow_listed_files(client: Any, name: str) -> None:
    """The allow-list is the security boundary against arbitrary file reads."""
    assert client.get(f"/static/{name}").status_code == 404


def test_stylesheet_covers_the_rendered_component_classes(client: Any) -> None:
    """Guards against markup/CSS drift: every component root must be styled."""
    css = client.get("/static/dashboard.css").text

    for selector in (
        ".decision-card",
        ".schedule-view__appointment",
        ".call-activity-log__row",
        ".impact-metrics-strip__card",
        ".dashboard-shell__denied",
        ".field-error",
        ".service-row",
        ".provider-row",
    ):
        assert selector in css


# ---------------------------------------------------------------------------
# Voice client (/voice) — the page that lets a human speak to the agent
# ---------------------------------------------------------------------------


def test_voice_client_page_is_served(client: Any) -> None:
    response = client.get("/voice")

    assert response.status_code == 200
    body = response.text
    assert '<link rel="stylesheet" href="/static/dashboard.css" />' in body
    assert 'src="/static/voice_client.js"' in body
    assert 'data-role="start"' in body
    assert 'data-role="stop"' in body


def test_voice_client_needs_no_role(client: Any) -> None:
    """It is the caller's side of the phone call and carries no clinic data."""
    response = client.get("/voice")

    assert response.status_code == 200
    # Nothing role-gated leaked into it.
    assert "555-0100" not in response.text
    assert "appt-1" not in response.text
    assert 'data-component="schedule-view"' not in response.text


def test_voice_client_script_is_allow_listed(client: Any) -> None:
    response = client.get("/static/voice_client.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    # The two rates the /ws audio contract depends on.
    assert "16000" in response.text
    assert "24000" in response.text


def test_voice_client_styles_exist(client: Any) -> None:
    css = client.get("/static/dashboard.css").text

    for selector in (
        ".voice-client",
        ".voice-client__call",
        ".voice-client__meter-fill",
        ".voice-client__line--agent",
    ):
        assert selector in css


# ---------------------------------------------------------------------------
# Per-call transcript + recording playback
# ---------------------------------------------------------------------------


def test_call_record_returns_the_transcript(app: Any, client: Any) -> None:
    app.stores.call_sessions.finalize(
        "call-1",
        CallOutcome.BOOKED,
        PatientRef(name="Dana Ellis"),
        ended_at="2026-03-04T08:04:00Z",
        transcript="[00:00] patient: what are your hours",
    )

    response = client.get("/dashboard/calls/call-1", headers=DOCTOR)

    assert response.status_code == 200
    body = response.json()
    assert body["call_session_id"] == "call-1"
    assert body["transcript"] == "[00:00] patient: what are your hours"
    assert body["outcome"] == "booked"
    assert body["ended_at"] == "2026-03-04T08:04:00Z"
    # No recording store configured, so no audio and no playback URL.
    assert body["recording_uri"] is None
    assert body["playback_url"] is None


def _recorded_app() -> tuple[Any, Any]:
    """A configured app with one recorded, transcribed call, plus a client."""
    from clinic_front_desk.data_layer.memory import MemoryCallRecordingStore

    composed = build_memory_application(stream=FakeVoiceStream())
    _seed(composed)
    recordings = MemoryCallRecordingStore()
    object.__setattr__(composed.stores, "recordings", recordings)
    recordings.put("call-1", b"RIFF-fake-wav")
    composed.stores.call_sessions.finalize(
        "call-1",
        CallOutcome.BOOKED,
        PatientRef(name="Dana Ellis"),
        ended_at="2026-03-04T08:04:00Z",
        transcript="[00:00] patient: hello",
        recording_uri="memory://recordings/call-1.wav",
    )
    return composed, TestClient(create_asgi_app(composed))


def test_call_record_returns_a_playable_url_when_recorded() -> None:
    """A ``memory://`` URI is not fetchable, so it falls back to the stream route."""
    _, http = _recorded_app()

    body = http.get("/dashboard/calls/call-1", headers=DOCTOR).json()

    assert body["recording_uri"] == "memory://recordings/call-1.wav"
    assert body["playback_url"] == "/dashboard/calls/call-1/recording"


def test_recording_stream_route_serves_the_audio() -> None:
    _, http = _recorded_app()

    response = http.get("/dashboard/calls/call-1/recording", headers=DOCTOR)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/wav")
    assert response.content == b"RIFF-fake-wav"
    # Patient audio must not be cached by intermediaries.
    assert "no-store" in response.headers["cache-control"]


def test_recording_stream_route_is_role_gated() -> None:
    _, http = _recorded_app()

    assert http.get("/dashboard/calls/call-1/recording").status_code == 403


def test_recording_stream_route_404s_when_recording_is_disabled(client: Any) -> None:
    assert client.get("/dashboard/calls/call-1/recording", headers=DOCTOR).status_code == 404


def test_recording_stream_route_404s_for_an_unrecorded_call() -> None:
    _, http = _recorded_app()

    assert http.get("/dashboard/calls/other/recording", headers=DOCTOR).status_code == 404


def test_call_record_is_gated_on_the_call_activity_view(client: Any) -> None:
    """A transcript is the most sensitive thing the dashboard serves."""
    assert client.get("/dashboard/calls/call-1").status_code == 403


def test_assistant_may_read_a_call_record(client: Any) -> None:
    """The assistant has the call-activity view, so the transcript comes with it."""
    assert client.get("/dashboard/calls/call-1", headers=ASSISTANT).status_code == 200


def test_unknown_call_is_not_found(client: Any) -> None:
    assert client.get("/dashboard/calls/nope", headers=DOCTOR).status_code == 404
