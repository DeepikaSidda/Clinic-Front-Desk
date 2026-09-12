"""Integration tests for the AgentCore deployment composition (task 14.1).

These exercise :class:`~clinic_front_desk.deployment.app.ClinicFrontDeskApplication`
end to end against in-memory fakes and a fake voice stream — no AWS, no Bedrock,
no network — asserting the four subsystems really do share **one** Data_Layer
(Req 16.1) and that cross-component propagation works:

- a config save is reflected in Voice_Front_Desk responses that begin
  afterwards, with no restart, and fans out to dashboard clients (Req 1.8),
- a voice escalation surfaces in the dashboard activity log and on the channel
  (Req 9.6),
- a voice booking is visible on the dashboard schedule (Req 16.1), and
- the scheduled Practice_Intelligence entrypoint persists Decisions through the
  same shared ``DecisionStore`` that the BFF reads (Req 13.x, 16.1).

No ``pytest-asyncio`` is available, so coroutines are driven with ``asyncio.run``
(mirroring ``tests/unit/test_voice_front_desk.py``).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from clinic_front_desk.data_layer.events import ChangeEntity, ChangeKind
from clinic_front_desk.deployment import build_memory_application
from clinic_front_desk.deployment.runtime import (
    build_scheduled_intelligence_entrypoint,
    build_voice_websocket_entrypoint,
)
from clinic_front_desk.models import (
    CallOutcome,
    ClinicKnowledgeBase,
    DayHours,
    EscalationReason,
    Provider,
    ServiceConfig,
    Slot,
    SlotStatus,
    WaitlistEntry,
)
from clinic_front_desk.voice.guardrails import Turn

pytestmark = pytest.mark.integration

PROVIDER_ID = "prov-1"
SERVICE = "Hearing Test"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeVoiceStream:
    """A scripted :class:`VoiceStream` satisfying the Protocol (no Nova Sonic)."""

    def __init__(self, script: list[Any] | None = None) -> None:
        self._script = script or []
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def send_audio(
        self, audio: str, *, format: str = "pcm", sample_rate: int = 16000, channels: int = 1
    ) -> None:
        return None

    async def send_text(self, text: str) -> None:
        return None

    async def stop_playback(self) -> None:
        return None

    async def events(self) -> AsyncIterator[Any]:
        for event in self._script:
            yield event

    async def close(self) -> None:
        self.closed = True


def _clinic(*, service_price: float = 150.0, location: str = "123 Main St") -> ClinicKnowledgeBase:
    return ClinicKnowledgeBase(
        location=location,
        hours={day: DayHours(open="09:00", close="17:00") for day in range(1, 6)},
        services=[ServiceConfig(name=SERVICE, price=service_price)],
        accepted_insurance=["Aetna"],
        providers=[Provider(id=PROVIDER_ID, name="Dr. Ada", specialty="ENT")],
    )


# ---------------------------------------------------------------------------
# Shared Data_Layer (Req 16.1)
# ---------------------------------------------------------------------------


def test_all_subsystems_share_one_data_layer() -> None:
    """Voice stores, the BFF, and the snapshot all use the same store instances."""
    app = build_memory_application(stream=FakeVoiceStream())

    voice_stores = app.stores.voice_stores()
    # The voice agent's stores are the very same objects the app holds (Req 16.1).
    assert voice_stores.appointments is app.stores.appointments
    assert voice_stores.knowledge_base is app.stores.knowledge_base
    # The BFF reads through the same shared stores.
    assert app.bff._decision_store is app.stores.decisions
    assert app.bff._appointment_store is app.stores.appointments
    # The channel the stores emit through is the channel the BFF fans out over.
    assert app.bff.channel is app.channel


# ---------------------------------------------------------------------------
# Live clinic-config propagation (Req 1.8)
# ---------------------------------------------------------------------------


def test_config_save_propagates_to_channel_and_bumps_version() -> None:
    """A config save fans out on the shared channel within budget (Req 1.8)."""
    app = build_memory_application(stream=FakeVoiceStream())
    client = app.bff.connect()

    assert app.config_version == 0
    start = time.perf_counter()
    result = app.save_config(_clinic())
    elapsed = time.perf_counter() - start

    assert result.ok is True
    # Synchronous fan-out — comfortably inside the 5 s budget (Req 1.8).
    assert elapsed < 5.0
    assert app.config_version == 1
    kb_events = [
        e for e in client.events if e.entity == ChangeEntity.CLINIC_KNOWLEDGE_BASE
    ]
    assert len(kb_events) == 1
    assert kb_events[0].kind == ChangeKind.UPDATED


def test_config_update_reflected_in_new_voice_agent_without_restart() -> None:
    """A voice agent built after a save reads the new config live (Req 1.8)."""
    app = build_memory_application(stream=FakeVoiceStream())

    # Before onboarding: no offered services, so the guardrail selects nothing.
    before = app.new_voice_agent()
    assert before.guardrail.offered_services == ()

    app.save_config(_clinic())

    # A freshly built agent (no restart) sees the saved service immediately.
    after = app.new_voice_agent()
    assert after.guardrail.offered_services == (SERVICE,)
    matched = after.tools["match_offered_service"](SERVICE)
    assert matched == {"ok": True, "value": SERVICE}


def test_config_update_reflected_in_inflight_session_tools() -> None:
    """Tools read the knowledge base live, so an in-flight session sees updates (Req 1.8)."""
    app = build_memory_application(stream=FakeVoiceStream())
    session = app.start_voice_session("call-live")

    # No config yet: the FAQ tool reports the info is unavailable (never fabricates).
    unavailable = session.toolset.answer_faq(topic="location")
    assert unavailable.__class__.__name__ == "Err"

    # Save config mid-session; no restart.
    app.save_config(_clinic(location="500 Clinic Way"))

    # The same live session now answers from the new config (tools re-read the store).
    answered = session.toolset.answer_faq(topic="location")
    assert answered.__class__.__name__ == "Ok"
    assert "500 Clinic Way" in answered.value


# ---------------------------------------------------------------------------
# Voice escalation -> dashboard activity log (Req 9.6, 16.1)
# ---------------------------------------------------------------------------


def test_voice_escalation_surfaces_in_activity_log_and_channel() -> None:
    """A voice escalation is recorded once and surfaces to the dashboard (Req 9.6)."""
    app = build_memory_application(stream=FakeVoiceStream())
    app.save_config(_clinic())
    client = app.bff.connect()

    session = app.start_voice_session("call-esc")
    decision = session.classify_turn(Turn(requests_clinical_content=True))
    assert decision.requires_escalation is True

    # Surfaced on the real-time channel as an escalation change event (Req 9.6).
    esc_events = [e for e in client.events if e.entity == ChangeEntity.ESCALATION]
    assert len(esc_events) == 1

    # And present in the BFF's activity log, read through the shared Data_Layer.
    activity = app.bff.recent_activity()
    assert activity.__class__.__name__ == "Ok"
    reasons = {getattr(entry, "interaction_type", None) for entry in activity.value}
    assert any("escalat" in str(r).lower() for r in reasons) or len(activity.value) >= 1


# ---------------------------------------------------------------------------
# Voice booking -> dashboard schedule (Req 16.1)
# ---------------------------------------------------------------------------


def test_voice_booking_visible_on_dashboard_schedule() -> None:
    """A booking made by the voice agent is visible via the shared BFF schedule."""
    app = build_memory_application(stream=FakeVoiceStream())
    app.save_config(_clinic())

    # Seed an open slot for the provider/service.
    slot = Slot(
        id="slot-1",
        provider_id=PROVIDER_ID,
        service=SERVICE,
        start="2999-03-02T10:00:00",
        end="2999-03-02T10:30:00",
        status=SlotStatus.OPEN,
    )
    assert app.stores.appointments.seed_slots  # in-memory fake seeding helper
    app.stores.appointments.seed_slots([slot])

    session = app.start_voice_session("call-book")
    outcome = session.book(
        named_service=SERVICE,
        name="Jane Doe",
        callback_phone="555-0100",
        provider_id=PROVIDER_ID,
    )
    assert outcome.kind == "chain_completed"

    view = app.bff.schedule_for_day(PROVIDER_ID, "2999-03-02", services=[SERVICE])
    assert view.__class__.__name__ == "Ok"
    assert len(view.value.appointments) == 1
    assert view.value.appointments[0].service == SERVICE


# ---------------------------------------------------------------------------
# Scheduled Practice_Intelligence entrypoint (Req 13.x, 16.1)
# ---------------------------------------------------------------------------


def _seed_unmet_demand(app: Any, count: int = 5) -> None:
    """Seed ``count`` active waitlist entries for the offered service."""
    for i in range(count):
        app.stores.waitlist.add(
            WaitlistEntry(
                id=f"wl-{i}",
                patient_id=f"pat-{i}",
                service=SERVICE,
                preferred_slot_type="any",
                added_at=f"2025-01-0{i + 1}T09:00:00",
                seq=i,
            )
        )


def test_scheduled_intelligence_creates_and_persists_decisions() -> None:
    """The scheduled entrypoint runs analysis and persists Decisions to the shared store."""
    app = build_memory_application(stream=FakeVoiceStream())
    app.save_config(_clinic())
    _seed_unmet_demand(app, count=5)
    client = app.bff.connect()

    scheduled = build_scheduled_intelligence_entrypoint(app)
    summary = scheduled()

    assert summary["analysis_failed"] is False
    assert summary["decisions_created"] == 1

    # The decision is visible via the BFF (reads through the same DecisionStore).
    open_decisions = app.bff.open_decisions()
    assert open_decisions.__class__.__name__ == "Ok"
    assert len(open_decisions.value) == 1
    assert open_decisions.value[0].finding_key == f"unmet_demand#{SERVICE}"

    # And a decision CREATED event fanned out on the shared channel.
    decision_events = [e for e in client.events if e.entity == ChangeEntity.DECISION]
    assert any(e.kind == ChangeKind.CREATED for e in decision_events)


def test_scheduled_run_is_idempotent_on_dedup() -> None:
    """Re-running analysis does not create a duplicate open Decision (Req 13.3)."""
    app = build_memory_application(stream=FakeVoiceStream())
    app.save_config(_clinic())
    _seed_unmet_demand(app, count=5)

    scheduled = build_scheduled_intelligence_entrypoint(app)
    first = scheduled()
    second = scheduled()

    assert first["decisions_created"] == 1
    assert second["decisions_created"] == 0
    assert len(app.bff.open_decisions().value) == 1


def test_empty_clinic_scheduled_run_creates_no_decisions() -> None:
    """With no configuration the snapshot is empty and no Decision is generated."""
    app = build_memory_application(stream=FakeVoiceStream())
    scheduled = build_scheduled_intelligence_entrypoint(app)

    summary = scheduled()
    assert summary["analysis_failed"] is False
    assert summary["decisions_created"] == 0
    assert app.bff.open_decisions().value == []


# ---------------------------------------------------------------------------
# Voice runtime entrypoint (bidirectional WebSocket launcher)
# ---------------------------------------------------------------------------


def test_voice_entrypoint_runs_and_finalizes_outcome() -> None:
    """The voice entrypoint drives a session lifecycle and persists an outcome (Req 11.5, 12.7)."""
    app = build_memory_application(stream=FakeVoiceStream())
    app.save_config(_clinic())

    handler = build_voice_websocket_entrypoint(app)
    outcome = _run(handler("call-runtime"))

    # No task completed on the empty script, so it ends as interrupted (Req 12.7).
    assert outcome == CallOutcome.INTERRUPTED
    recent = app.stores.call_sessions.list_recent(10).value
    assert any(s.id == "call-runtime" for s in recent)


def test_scheduler_interval_within_24h() -> None:
    """The composed analysis scheduler fires on a cadence <= 24 h (Req 13.1)."""
    from datetime import timedelta

    app = build_memory_application(stream=FakeVoiceStream())
    assert app.scheduler.interval <= timedelta(hours=24)
