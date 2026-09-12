"""Unit tests for the deployment composition factory (task 14.1).

These focus on the wiring details of
:class:`~clinic_front_desk.deployment.app.ClinicFrontDeskApplication` and the
runtime entrypoint scaffolding — the shared-Data_Layer bundle, live-config
version tracking (Req 1.8), the practice-intelligence snapshot assembly from the
stores (Req 13.2), and entrypoint registration — against in-memory fakes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from clinic_front_desk.deployment.app import (
    ApplicationStores,
    build_memory_application,
)
from clinic_front_desk.deployment.runtime import (
    RuntimeConfig,
    build_scheduled_intelligence_entrypoint,
    build_voice_websocket_entrypoint,
    register_entrypoints,
)
from clinic_front_desk.models import (
    Appointment,
    ClinicKnowledgeBase,
    DayHours,
    Provider,
    ServiceConfig,
    Slot,
    SlotStatus,
    WaitlistEntry,
)
from clinic_front_desk.voice import PATIENT_FACING_TOOL_NAMES, VoiceFrontDeskAgent, VoiceFrontDeskStores

PROVIDER_ID = "prov-1"
SERVICE = "Hearing Test"


class FakeVoiceStream:
    """Minimal :class:`VoiceStream` fake (no Nova Sonic)."""

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
        for event in ():
            yield event

    async def close(self) -> None:
        return None


def _clinic() -> ClinicKnowledgeBase:
    return ClinicKnowledgeBase(
        location="123 Main St",
        hours={day: DayHours(open="09:00", close="17:00") for day in range(1, 6)},
        services=[ServiceConfig(name=SERVICE, price=150.0)],
        accepted_insurance=["Aetna"],
        providers=[Provider(id=PROVIDER_ID, name="Dr. Ada", specialty="ENT")],
    )


# ---------------------------------------------------------------------------
# Shared store bundle
# ---------------------------------------------------------------------------


def test_application_stores_voice_subset_maps_same_instances() -> None:
    app = build_memory_application(stream=FakeVoiceStream())
    vs: VoiceFrontDeskStores = app.stores.voice_stores()
    assert vs.appointments is app.stores.appointments
    assert vs.patients is app.stores.patients
    assert vs.waitlist is app.stores.waitlist
    assert vs.escalations is app.stores.escalations
    assert vs.knowledge_base is app.stores.knowledge_base
    assert vs.call_sessions is app.stores.call_sessions


def test_application_stores_is_frozen_bundle() -> None:
    app = build_memory_application(stream=FakeVoiceStream())
    assert isinstance(app.stores, ApplicationStores)


# ---------------------------------------------------------------------------
# Voice agent construction (live config)
# ---------------------------------------------------------------------------


def test_new_voice_agent_registers_ten_tools() -> None:
    app = build_memory_application(stream=FakeVoiceStream())
    app.save_config(_clinic())
    agent = app.new_voice_agent()
    assert isinstance(agent, VoiceFrontDeskAgent)
    assert len(agent.tool_names) == len(PATIENT_FACING_TOOL_NAMES)


def test_config_version_starts_zero_and_increments_on_save() -> None:
    app = build_memory_application(stream=FakeVoiceStream())
    assert app.config_version == 0
    assert app.last_config_event is None

    app.save_config(_clinic())
    assert app.config_version == 1
    assert app.last_config_event is not None

    app.save_config(_clinic())
    assert app.config_version == 2


def test_failed_validation_save_does_not_bump_version() -> None:
    app = build_memory_application(stream=FakeVoiceStream())
    # Missing required fields (no providers/services) -> validation failure.
    result = app.save_config(ClinicKnowledgeBase(location=""))
    assert result.ok is False
    assert app.config_version == 0


# ---------------------------------------------------------------------------
# Snapshot assembly (Req 13.2)
# ---------------------------------------------------------------------------


def test_assemble_snapshot_reads_through_stores() -> None:
    app = build_memory_application(
        stream=FakeVoiceStream(), now_provider=lambda: "2025-02-01"
    )
    app.save_config(_clinic())

    app.stores.appointments.seed_slots(
        [
            Slot(
                id="slot-1",
                provider_id=PROVIDER_ID,
                service=SERVICE,
                start="2025-01-20T10:00:00",
                end="2025-01-20T10:30:00",
                status=SlotStatus.OPEN,
            )
        ]
    )
    app.stores.appointments.create(
        Appointment(
            id="appt-1",
            provider_id=PROVIDER_ID,
            patient_id="pat-1",
            service=SERVICE,
            slot_id="slot-x",
            date="2025-01-15",
            time="09:00",
        )
    )
    app.stores.waitlist.add(
        WaitlistEntry(
            id="wl-1",
            patient_id="pat-2",
            service=SERVICE,
            preferred_slot_type="any",
            added_at="2025-01-10T09:00:00",
            seq=0,
        )
    )

    snapshot = app.assemble_snapshot()

    assert snapshot.now == "2025-02-01"
    assert snapshot.window_days == 30
    assert SERVICE in snapshot.offered_services
    assert [a.id for a in snapshot.appointments] == ["appt-1"]
    assert [s.id for s in snapshot.slots] == ["slot-1"]
    assert [e.id for e in snapshot.waitlist] == ["wl-1"]


def test_assemble_snapshot_empty_when_unconfigured() -> None:
    app = build_memory_application(stream=FakeVoiceStream())
    snapshot = app.assemble_snapshot()
    assert snapshot.appointments == []
    assert snapshot.slots == []
    assert snapshot.waitlist == []
    assert snapshot.offered_services == frozenset()


# ---------------------------------------------------------------------------
# run_intelligence
# ---------------------------------------------------------------------------


def test_run_intelligence_returns_synthesis_result() -> None:
    app = build_memory_application(stream=FakeVoiceStream())
    app.save_config(_clinic())
    for i in range(5):
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
    result = app.run_intelligence()
    assert result.analysis_failed is False
    assert len(result.created) == 1


# ---------------------------------------------------------------------------
# Runtime entrypoint registration
# ---------------------------------------------------------------------------


def test_register_entrypoints_uses_app_decorator() -> None:
    app = build_memory_application(stream=FakeVoiceStream())

    class RecordingRuntimeApp:
        def __init__(self) -> None:
            self.registered: list[Any] = []

        def entrypoint(self, func: Any) -> Any:
            self.registered.append(func)
            return func

    runtime_app = RecordingRuntimeApp()
    handlers = register_entrypoints(runtime_app, app)

    assert set(handlers) == {"voice", "scheduled"}
    assert len(runtime_app.registered) == 2
    assert handlers["voice"] in runtime_app.registered
    assert handlers["scheduled"] in runtime_app.registered


def test_register_entrypoints_tolerates_missing_decorator() -> None:
    app = build_memory_application(stream=FakeVoiceStream())

    class BareApp:
        pass

    handlers = register_entrypoints(BareApp(), app)  # type: ignore[arg-type]
    assert set(handlers) == {"voice", "scheduled"}


def test_scheduled_entrypoint_summary_shape() -> None:
    app = build_memory_application(stream=FakeVoiceStream())
    scheduled = build_scheduled_intelligence_entrypoint(app)
    summary = scheduled({"source": "schedule"}, None)
    assert set(summary) == {
        "analysis_failed",
        "decisions_created",
        "skipped_non_actionable",
        "skipped_below_threshold",
        "skipped_duplicate",
        "retained",
    }


def test_voice_entrypoint_is_callable() -> None:
    app = build_memory_application(stream=FakeVoiceStream())
    handler = build_voice_websocket_entrypoint(app)
    assert callable(handler)


def test_runtime_config_defaults() -> None:
    config = RuntimeConfig()
    assert config.analysis_interval_hours == 24.0
    assert config.table_name
    assert config.region
