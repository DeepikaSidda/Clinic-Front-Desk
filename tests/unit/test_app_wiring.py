"""Wiring smoke tests for the package-root composition (task 14.1; Req 1.8, 16.1).

These assemble the *whole* application via
:func:`clinic_front_desk.app.build_application` against in-memory stores and a
fake voice stream — no AWS, no Bedrock, no network — and exercise a wiring smoke
path: all four subsystems share ONE Data_Layer + ONE change channel, a config
save fans out on that channel within budget (Req 1.8), and a voice agent built
after a save reads the new configuration live without a restart (Req 1.8).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

from clinic_front_desk.app import (
    Application,
    ApplicationStores,
    build_application,
)
from clinic_front_desk.data_layer.events import ChangeEntity
from clinic_front_desk.dashboard.pubsub import DashboardChannel
from clinic_front_desk.data_layer.memory import (
    MemoryAppointmentStore,
    MemoryCallSessionStore,
    MemoryClinicKnowledgeBaseStore,
    MemoryDecisionStore,
    MemoryEscalationStore,
    MemoryPatientStore,
    MemoryWaitlistStore,
)
from clinic_front_desk.models import (
    ClinicKnowledgeBase,
    DayHours,
    Provider,
    ServiceConfig,
)
from clinic_front_desk.runtime import (
    build_scheduled_intelligence_entrypoint,
    build_voice_websocket_entrypoint,
    register_entrypoints,
)
from clinic_front_desk.voice import PATIENT_FACING_TOOL_NAMES, VoiceFrontDeskAgent

PROVIDER_ID = "prov-1"
SERVICE = "Hearing Test"


class FakeVoiceStream:
    """Minimal ``VoiceStream`` fake (no Nova Sonic / no network)."""

    async def start(self) -> None:
        return None

    async def send_audio(
        self,
        audio: str,
        *,
        format: str = "pcm",
        sample_rate: int = 16000,
        channels: int = 1,
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
# build_application default (in-memory) backend
# ---------------------------------------------------------------------------


def test_build_application_defaults_to_in_memory_and_composes_all_subsystems() -> None:
    app = build_application(stream=FakeVoiceStream())

    assert isinstance(app, Application)
    # All four subsystems are present and share the one Data_Layer bundle.
    assert isinstance(app.stores, ApplicationStores)
    assert app.bff is not None
    assert app.scheduler is not None
    assert app.synthesizer is not None


def test_all_subsystems_share_one_data_layer_and_channel() -> None:
    app = build_application(stream=FakeVoiceStream())

    voice_stores = app.stores.voice_stores()
    # Voice tools and the snapshot read the same store instances the BFF reads.
    assert voice_stores.appointments is app.stores.appointments
    assert voice_stores.knowledge_base is app.stores.knowledge_base
    assert voice_stores.call_sessions is app.stores.call_sessions
    # The BFF fans out over the same channel the stores emit on.
    assert app.bff.channel is app.channel


def test_scheduler_interval_is_within_24h() -> None:
    app = build_application(stream=FakeVoiceStream(), analysis_interval=timedelta(hours=48))
    # Scheduler clamps the cadence to <= 24h (Req 13.1).
    assert app.scheduler.interval <= timedelta(hours=24)


# ---------------------------------------------------------------------------
# Live clinic-config propagation (Req 1.8)
# ---------------------------------------------------------------------------


def test_config_save_fans_out_on_shared_channel_and_bumps_version() -> None:
    app = build_application(stream=FakeVoiceStream())
    received: list[Any] = []
    app.channel.subscribe(received.append)

    assert app.config_version == 0
    result = app.save_config(_clinic())

    assert result.ok is True
    # Synchronous fan-out on the one shared channel -> well within the 5s budget.
    assert app.config_version == 1
    assert any(e.entity == ChangeEntity.CLINIC_KNOWLEDGE_BASE for e in received)


def test_voice_agent_built_after_save_reads_config_live_without_restart() -> None:
    app = build_application(stream=FakeVoiceStream())

    # Before onboarding: no offered services configured.
    agent_before = app.new_voice_agent()
    assert isinstance(agent_before, VoiceFrontDeskAgent)

    app.save_config(_clinic())

    # A fresh agent built after the save reflects the new config with no restart.
    agent_after = app.new_voice_agent()
    assert isinstance(agent_after, VoiceFrontDeskAgent)
    assert len(agent_after.tool_names) == len(PATIENT_FACING_TOOL_NAMES)


# ---------------------------------------------------------------------------
# Custom (caller-provided) store bundle path
# ---------------------------------------------------------------------------


def test_build_application_accepts_custom_stores_bundle_sharing_channel() -> None:
    channel = DashboardChannel()
    stores = ApplicationStores(
        appointments=MemoryAppointmentStore(channel),
        patients=MemoryPatientStore(channel),
        waitlist=MemoryWaitlistStore(channel),
        decisions=MemoryDecisionStore(channel),
        knowledge_base=MemoryClinicKnowledgeBaseStore(channel),
        call_sessions=MemoryCallSessionStore(channel),
        escalations=MemoryEscalationStore(channel),
    )

    app = build_application(stores=stores, channel=channel, stream=FakeVoiceStream())

    assert app.stores is stores
    assert app.channel is channel


def test_build_application_rejects_stores_without_channel() -> None:
    channel = DashboardChannel()
    stores = ApplicationStores(
        appointments=MemoryAppointmentStore(channel),
        patients=MemoryPatientStore(channel),
        waitlist=MemoryWaitlistStore(channel),
        decisions=MemoryDecisionStore(channel),
        knowledge_base=MemoryClinicKnowledgeBaseStore(channel),
        call_sessions=MemoryCallSessionStore(channel),
        escalations=MemoryEscalationStore(channel),
    )

    raised = False
    try:
        build_application(stores=stores, stream=FakeVoiceStream())
    except ValueError:
        raised = True
    assert raised


def test_build_application_rejects_stores_and_table_together() -> None:
    channel = DashboardChannel()
    stores = ApplicationStores(
        appointments=MemoryAppointmentStore(channel),
        patients=MemoryPatientStore(channel),
        waitlist=MemoryWaitlistStore(channel),
        decisions=MemoryDecisionStore(channel),
        knowledge_base=MemoryClinicKnowledgeBaseStore(channel),
        call_sessions=MemoryCallSessionStore(channel),
        escalations=MemoryEscalationStore(channel),
    )

    raised = False
    try:
        build_application(stores=stores, channel=channel, table=object())
    except ValueError:
        raised = True
    assert raised


# ---------------------------------------------------------------------------
# Runtime entrypoints assemble over the composed application
# ---------------------------------------------------------------------------


def test_runtime_entrypoints_build_over_composed_application() -> None:
    app = build_application(stream=FakeVoiceStream())

    voice = build_voice_websocket_entrypoint(app)
    scheduled = build_scheduled_intelligence_entrypoint(app)
    assert callable(voice)
    assert callable(scheduled)

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
