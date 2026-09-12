"""``ClinicFrontDeskApplication`` — the single application-composition factory.

Task 14.1 (Req 1.8, 16.1). See design "Runtime Topology".

This module composes the four subsystems over **one shared Data_Layer** so the
Voice_Front_Desk, the scheduled Practice_Intelligence run, and the Dashboard BFF
all read and write through the same store instances (Req 16.1). Those stores are
constructed with a single :class:`~clinic_front_desk.dashboard.pubsub.DashboardChannel`
as their :class:`~clinic_front_desk.data_layer.events.ChangeEmitter`, so every
successful mutation — a booked appointment, a resolved decision, a saved config
— fans out to connected dashboard clients through that one channel.

Live clinic-config update propagation (Req 1.8)
-----------------------------------------------
Req 1.8: *a config update must be reflected in Voice_Front_Desk responses that
begin within 5 seconds of the save, without a restart.* Two mechanisms deliver
this, both here:

1. **No caching at startup.** :meth:`ClinicFrontDeskApplication.new_voice_agent`
   builds a *fresh* :class:`~clinic_front_desk.voice.agent.VoiceFrontDeskAgent`
   for each Call_Session, reading the offered-service list live from the
   ``ClinicKnowledgeBaseStore`` at that moment (it passes ``offered_services=None``
   so the agent reads through the store). The patient-facing tools
   (``answer_faq``, ``match_offered_service``, ``check_availability``) already
   read the knowledge base through the store on every call, so a response that
   *begins* after a save observes the new configuration — no process restart,
   no cached snapshot.
2. **Immediate change propagation.** :meth:`ClinicFrontDeskApplication.save_config`
   persists through the same shared store, whose successful ``save`` emits a
   ``CLINIC_KNOWLEDGE_BASE`` :class:`~clinic_front_desk.data_layer.events.ChangeEvent`
   synchronously on the shared channel. The application subscribes to that event
   to bump :attr:`config_version` / :attr:`last_config_event`, giving an
   observable, sub-second signal that the new config is live (well inside the
   5 s budget).

Testability
-----------
Everything here depends only on the store *interfaces*, the
:class:`~clinic_front_desk.voice.stream.VoiceStream` Protocol, and the pure
intelligence functions — never on boto3, Bedrock, or AgentCore. So the whole
composition runs against in-memory fakes and a fake voice stream via
:func:`build_memory_application`. The DynamoDB wiring (:func:`build_dynamo_application`)
shares the identical composition, swapping only the store implementations
(Req 16.5).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from clinic_front_desk.config import ConfigSaveResult, save_clinic_config
from clinic_front_desk.dashboard.bff import DashboardBFF
from clinic_front_desk.dashboard.pubsub import DashboardChannel
from clinic_front_desk.data_layer.events import ChangeEntity, ChangeEvent
from clinic_front_desk.data_layer.interfaces import (
    AppointmentStore,
    CallRecordingStore,
    CallSessionStore,
    ClinicDocumentStore,
    ClinicKnowledgeBaseStore,
    DecisionStore,
    EscalationStore,
    PatientStore,
    WaitlistStore,
)
from clinic_front_desk.documents.embeddings import Embedder
from clinic_front_desk.documents.extraction import ConfigExtractor
from clinic_front_desk.intelligence import PatternInput, analyze_patterns
from clinic_front_desk.intelligence.scheduler import DEFAULT_INTERVAL, AnalysisScheduler
from clinic_front_desk.intelligence.synthesizer import (
    DecisionSynthesizer,
    FailureRecorder,
    SynthesisResult,
    _noop_recorder,
)
from clinic_front_desk.models import (
    ClinicKnowledgeBase,
    ISODate,
    is_ok,
)
from clinic_front_desk.voice.clinic_briefing import with_clinic_briefing
from clinic_front_desk.voice import (
    ADMINISTRATIVE_ONLY_SYSTEM_PROMPT,
    with_recording_notice,
    VoiceFrontDeskAgent,
    VoiceFrontDeskStores,
    VoiceSession,
    create_voice_front_desk_agent,
)
from clinic_front_desk.voice.stream import VoiceStream

__all__ = [
    "ApplicationStores",
    "ClinicFrontDeskApplication",
    "build_memory_application",
    "build_dynamo_application",
]


#: How many most-recent call sessions the analysis snapshot pulls (Req 13.2).
DEFAULT_SESSION_SNAPSHOT_LIMIT = 500

#: Default analysis window in days for the assembled snapshot.
DEFAULT_SNAPSHOT_WINDOW_DAYS = 30


def _utc_today() -> ISODate:
    """Default reference date (ISO ``YYYY-MM-DD``) for the analysis window."""
    return datetime.now(UTC).date().isoformat()


def _date_range(start: ISODate, end: ISODate) -> list[ISODate]:
    """Inclusive list of ISO dates from ``start`` to ``end`` (empty if reversed)."""
    try:
        first = date.fromisoformat(start)
        last = date.fromisoformat(end)
    except ValueError:
        return []
    if last < first:
        return []
    days = (last - first).days
    return [(first + timedelta(days=offset)).isoformat() for offset in range(days + 1)]


def _minus_days(day: ISODate, count: int) -> ISODate:
    """The ISO date ``count`` days before ``day``."""
    return (date.fromisoformat(day) - timedelta(days=count)).isoformat()


# ---------------------------------------------------------------------------
# The shared Data_Layer bundle.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplicationStores:
    """The Data_Layer stores, sharing one change emitter (Req 16.1).

    This is the *one* Data_Layer every subsystem reads and writes through. The
    same instances back the voice tools, the practice-intelligence snapshot, and
    the dashboard reads, so a mutation from any subsystem is immediately visible
    to the others and fans out on the shared channel.
    """

    appointments: AppointmentStore
    patients: PatientStore
    waitlist: WaitlistStore
    decisions: DecisionStore
    knowledge_base: ClinicKnowledgeBaseStore
    call_sessions: CallSessionStore
    escalations: EscalationStore
    #: Call audio, in object storage rather than the DynamoDB table. ``None``
    #: disables recording entirely, which is the default: no bucket configured
    #: means no patient audio is captured or retained.
    recordings: CallRecordingStore | None = None
    #: The doctor's uploaded clinic documents, also in object storage. The portal
    #: writes here; the FAQ tool reads through it for descriptive questions the
    #: configuration has no field for. ``None`` means no uploads and no retrieval.
    documents: ClinicDocumentStore | None = None
    #: Embeds a caller's question to search ``documents``. Kept beside the store
    #: rather than inside it because embedding is a Bedrock call, not persistence,
    #: and the store must stay usable (upload, list, download) without it.
    #: Retrieval requires both; either one missing disables it.
    embedder: Embedder | None = None
    #: Reads structured clinic details out of an uploaded document to pre-fill the
    #: onboarding wizard. Separate from ``embedder`` because they are different
    #: models used at different moments — a deployment can search documents
    #: without offering the pre-fill, or the reverse.
    config_extractor: ConfigExtractor | None = None

    def voice_stores(self) -> VoiceFrontDeskStores:
        """The subset of stores the Voice_Front_Desk depends on (task 9.2).

        A fresh :class:`DocumentKnowledge` is built per call rather than held on
        this bundle. It caches the chunk corpus for the length of a call — long
        enough that a caller's follow-up question costs no extra object read, short
        enough that a document uploaded mid-morning is live on the next call
        without an explicit cache invalidation. Same reasoning as the per-session
        agent rebuild for clinic config (Req 1.8).
        """
        documents = None
        if self.documents is not None and self.embedder is not None:
            from clinic_front_desk.documents.retrieval import DocumentKnowledge

            documents = DocumentKnowledge(store=self.documents, embedder=self.embedder)
        return VoiceFrontDeskStores(
            appointments=self.appointments,
            patients=self.patients,
            waitlist=self.waitlist,
            escalations=self.escalations,
            knowledge_base=self.knowledge_base,
            call_sessions=self.call_sessions,
            documents=documents,
        )


# ---------------------------------------------------------------------------
# The composition root.
# ---------------------------------------------------------------------------


class ClinicFrontDeskApplication:
    """Composes both agents, the Data_Layer, and the Dashboard BFF (task 14.1).

    Construct via :func:`build_memory_application` (tests / local) or
    :func:`build_dynamo_application` (production). All three subsystems share the
    :class:`ApplicationStores` bundle and the :class:`DashboardChannel` given
    here.
    """

    def __init__(
        self,
        *,
        channel: DashboardChannel,
        stores: ApplicationStores,
        stream: VoiceStream | None = None,
        stream_factory: Callable[[], VoiceStream] | None = None,
        model: Any | None = None,
        system_prompt: str = ADMINISTRATIVE_ONLY_SYSTEM_PROMPT,
        analysis_interval: timedelta = DEFAULT_INTERVAL,
        synthesizer: DecisionSynthesizer | None = None,
        failure_recorder: FailureRecorder = _noop_recorder,
        snapshot_window_days: int = DEFAULT_SNAPSHOT_WINDOW_DAYS,
        session_snapshot_limit: int = DEFAULT_SESSION_SNAPSHOT_LIMIT,
        now_provider: Callable[[], ISODate] = _utc_today,
    ) -> None:
        """Wire the application.

        Args:
            channel: The shared pub/sub channel; it is the ``ChangeEmitter`` the
                ``stores`` were constructed with, and the BFF fans out over it.
            stores: The single shared Data_Layer bundle (Req 16.1).
            stream: A pre-built :class:`VoiceStream` reused for every voice
                session (typically a fake in tests).
            stream_factory: A per-session :class:`VoiceStream` factory
                (production wants one Nova Sonic stream per call). When neither
                ``stream`` nor ``stream_factory`` is set, each voice agent builds
                its own Nova Sonic stream.
            model: Nova Sonic model id / ``BidiModel`` for the default stream.
            system_prompt: The guardrail system prompt (Req 10.1).
            analysis_interval: Practice_Intelligence cadence (clamped to ≤ 24 h
                by the scheduler, Req 13.1).
            synthesizer: A :class:`DecisionSynthesizer`; one is built over the
                shared ``DecisionStore`` when omitted.
            failure_recorder: Sink for analysis failures (Req 13.7); used only
                when building the default synthesizer.
            snapshot_window_days: Analysis window for the assembled snapshot.
            session_snapshot_limit: Max recent call sessions pulled per run.
            now_provider: Injectable reference-date source for the snapshot
                window (defaults to UTC today).
        """
        self._channel = channel
        self._stores = stores
        self._stream = stream
        self._stream_factory = stream_factory
        self._model = model
        # Recording a call without telling the caller is unlawful in
        # all-party-consent jurisdictions, so the notice is attached here rather
        # than left to whoever configures the prompt: configuring a recording
        # store cannot silently skip announcing it. Absent when recording is off,
        # so the agent never claims a call is recorded when it is not.
        self._system_prompt = (
            with_recording_notice(system_prompt)
            if stores.recordings is not None
            else system_prompt
        )
        self._snapshot_window_days = snapshot_window_days
        self._session_snapshot_limit = session_snapshot_limit
        self._now_provider = now_provider

        # Dashboard BFF over the shared stores + channel (Req 16.1).
        self._bff = DashboardBFF(
            channel=channel,
            decision_store=stores.decisions,
            appointment_store=stores.appointments,
            call_session_store=stores.call_sessions,
            escalation_store=stores.escalations,
            waitlist_store=stores.waitlist,
            patient_store=stores.patients,
            clinic_knowledge_base_store=stores.knowledge_base,
        )

        # Practice_Intelligence: synthesizer over the shared DecisionStore, and a
        # scheduler whose run callback assembles the snapshot and synthesizes.
        self._synthesizer = synthesizer or DecisionSynthesizer(
            stores.decisions, failure_recorder=failure_recorder
        )
        # The scheduler calls its run callback with no arguments; wrap
        # ``run_intelligence`` (whose ``now`` is optional) so the zero-argument
        # RunCallback contract is explicit rather than relying on the default.
        self._scheduler = AnalysisScheduler(
            lambda: self.run_intelligence(), interval=analysis_interval
        )

        # Live config-update observability (Req 1.8): subscribe to the shared
        # channel and track knowledge-base change events. Because emit() is
        # synchronous, this fires within the same call as save_config, well
        # inside the 5 s budget.
        self._config_version = 0
        self._last_config_event: ChangeEvent | None = None
        self._config_subscription = channel.subscribe(self._on_change_event)

    # -- shared Data_Layer + real-time channel -----------------------------

    @property
    def channel(self) -> DashboardChannel:
        """The shared change-event channel (stores emit; BFF/clients subscribe)."""
        return self._channel

    @property
    def stores(self) -> ApplicationStores:
        """The single shared Data_Layer bundle (Req 16.1)."""
        return self._stores

    @property
    def bff(self) -> DashboardBFF:
        """The Dashboard BFF reading through the shared Data_Layer."""
        return self._bff

    # -- Voice_Front_Desk ---------------------------------------------------

    def new_voice_agent(
        self, *, offered_services: Sequence[str] | None = None
    ) -> VoiceFrontDeskAgent:
        """Build a fresh Voice_Front_Desk agent reading config live (Req 1.8).

        A new agent is built per Call_Session so the guardrail's offered-service
        list is read live from the ``ClinicKnowledgeBaseStore`` at session start
        (``offered_services=None`` → read through the store). Combined with the
        tools' per-call knowledge-base reads, this is what lets a config save be
        reflected in responses that begin afterwards without a restart (Req 1.8).
        """
        # The clinic's static facts go into the prompt, not down a tool call.
        # Measured on the live stream, Nova Sonic reaches END_TURN before a tool
        # executes, so a detail it had to fetch was not available while it was
        # speaking and it filled the gap from its own priors. Briefing it up front
        # is what makes "answer from the clinic's own information" achievable.
        # Built per call, so an upload or a config change is live on the next one.
        return create_voice_front_desk_agent(
            self._stores.voice_stores(),
            stream=self._stream,
            stream_factory=self._stream_factory,
            model=self._model,
            system_prompt=with_clinic_briefing(
                self._system_prompt,
                self._stores.knowledge_base,
                self._stores.documents,
                appointments=self._stores.appointments,
            ),
            offered_services=offered_services,
        )

    def start_voice_session(
        self, session_id: str | None = None, *, open_record: bool = True
    ) -> VoiceSession:
        """Start one Call_Session on a freshly built, live-config voice agent.

        Equivalent to ``new_voice_agent().start_session(...)`` — the fresh agent
        guarantees the session reflects the current clinic configuration
        (Req 1.8, Req 16.1).
        """
        agent = self.new_voice_agent()
        return agent.start_session(session_id, open_record=open_record)

    # -- Practice_Intelligence (scheduled entrypoint) ----------------------

    @property
    def synthesizer(self) -> DecisionSynthesizer:
        """The decision synthesizer over the shared ``DecisionStore``."""
        return self._synthesizer

    @property
    def scheduler(self) -> AnalysisScheduler:
        """The analysis scheduler (≤ 24 h cadence, Req 13.1)."""
        return self._scheduler

    def assemble_snapshot(
        self, now: ISODate | None = None, *, window_days: int | None = None
    ) -> PatternInput:
        """Assemble the analysis snapshot from the shared Data_Layer (Req 13.2).

        Reads only through the store interfaces (Req 16.1): the offered services
        and provider roster from the knowledge base, then per-provider
        appointments across the window's days, per-provider/service open slots,
        per-service waitlist entries, and the most-recent call sessions. When the
        clinic is unconfigured every read is empty, yielding an empty snapshot
        (no findings, no decisions).
        """
        window = window_days if window_days is not None else self._snapshot_window_days
        today = now or self._now_provider()
        window_start = _minus_days(today, window)
        days = _date_range(window_start, today)

        kb_result = self._stores.knowledge_base.get()
        kb = kb_result.value if is_ok(kb_result) else None
        providers = list(kb.providers) if kb else []
        offered = [service.name for service in kb.services] if kb else []

        appointments = []
        slots = []
        for provider in providers:
            for day in days:
                appts = self._stores.appointments.list_by_provider_and_day(
                    provider.id, day
                )
                if is_ok(appts):
                    appointments.extend(appts.value)
            for service in offered:
                open_slots = self._stores.appointments.list_open_slots(
                    provider.id, service, window_start
                )
                if is_ok(open_slots):
                    slots.extend(open_slots.value)

        waitlist = []
        for service in offered:
            entries = self._stores.waitlist.list_by_service_ordered(service)
            if is_ok(entries):
                waitlist.extend(entries.value)

        sessions_result = self._stores.call_sessions.list_recent(
            self._session_snapshot_limit
        )
        call_sessions = sessions_result.value if is_ok(sessions_result) else []

        return PatternInput(
            appointments=appointments,
            slots=slots,
            waitlist=waitlist,
            call_sessions=call_sessions,
            offered_services=frozenset(offered),
            named_service_requests=[],
            window_days=window,
            now=today,
        )

    def run_intelligence(self, now: ISODate | None = None) -> SynthesisResult:
        """Run one Practice_Intelligence analysis pass (Req 13.2–13.8).

        Assembles the snapshot, runs ``analyze_patterns`` over it, and hands the
        result to the :class:`DecisionSynthesizer`, which applies the generation
        gates and persists qualifying Decisions through the shared
        ``DecisionStore``. This is the callback the :attr:`scheduler` fires and
        the body of the scheduled AgentCore entrypoint.
        """
        snapshot = self.assemble_snapshot(now)
        analysis = analyze_patterns(snapshot)
        return self._synthesizer.synthesize(analysis)

    # -- live clinic-config propagation (Req 1.8) --------------------------

    def save_config(
        self, kb: ClinicKnowledgeBase
    ) -> ConfigSaveResult:
        """Validate and atomically save clinic config through the shared store.

        On success the shared store emits a ``CLINIC_KNOWLEDGE_BASE`` change
        event synchronously (bumping :attr:`config_version`), and every voice
        session started afterwards — plus every knowledge-base read the tools
        perform — observes the new configuration with no restart (Req 1.8, 1.4).
        """
        return save_clinic_config(self._stores.knowledge_base, kb)

    @property
    def config_version(self) -> int:
        """Count of clinic-config change events seen on the shared channel.

        Increments synchronously whenever a config save succeeds, providing an
        observable, sub-second signal that a new configuration is live (Req 1.8).
        """
        return self._config_version

    @property
    def last_config_event(self) -> ChangeEvent | None:
        """The most recent ``CLINIC_KNOWLEDGE_BASE`` change event, if any."""
        return self._last_config_event

    def _on_change_event(self, event: ChangeEvent) -> None:
        """Channel subscriber: track live clinic-config updates (Req 1.8)."""
        if event.entity == ChangeEntity.CLINIC_KNOWLEDGE_BASE:
            self._config_version += 1
            self._last_config_event = event


# ---------------------------------------------------------------------------
# Factories.
# ---------------------------------------------------------------------------


def build_memory_application(
    *,
    stream: VoiceStream | None = None,
    stream_factory: Callable[[], VoiceStream] | None = None,
    model: Any | None = None,
    system_prompt: str = ADMINISTRATIVE_ONLY_SYSTEM_PROMPT,
    analysis_interval: timedelta = DEFAULT_INTERVAL,
    synthesizer: DecisionSynthesizer | None = None,
    failure_recorder: FailureRecorder = _noop_recorder,
    now_provider: Callable[[], ISODate] = _utc_today,
    record_calls: bool = False,
    embedder: Embedder | None = None,
    config_extractor: ConfigExtractor | None = None,
) -> ClinicFrontDeskApplication:
    """Wire a :class:`ClinicFrontDeskApplication` over in-memory fake stores.

    Constructs one :class:`DashboardChannel` and gives it to all seven in-memory
    stores as their shared ``ChangeEmitter``, so voice, intelligence, and the BFF
    share one Data_Layer that fans changes out through the one channel. This is
    the composition used by the composition/integration tests and local runs; it
    requires no AWS credentials or network.
    """
    from clinic_front_desk.data_layer.memory import (
        MemoryAppointmentStore,
        MemoryCallSessionStore,
        MemoryClinicKnowledgeBaseStore,
        MemoryDecisionStore,
        MemoryEscalationStore,
        MemoryPatientStore,
        MemoryWaitlistStore,
    )

    from clinic_front_desk.data_layer.memory import (
        MemoryCallRecordingStore,
        MemoryClinicDocumentStore,
    )

    channel = DashboardChannel()
    stores = ApplicationStores(
        appointments=MemoryAppointmentStore(channel),
        patients=MemoryPatientStore(channel),
        waitlist=MemoryWaitlistStore(channel),
        decisions=MemoryDecisionStore(channel),
        knowledge_base=MemoryClinicKnowledgeBaseStore(channel),
        call_sessions=MemoryCallSessionStore(channel),
        escalations=MemoryEscalationStore(channel),
        # Opt-in even for the fake: recording patient audio should never switch
        # itself on just because a backend happens to be available.
        recordings=MemoryCallRecordingStore(channel) if record_calls else None,
        # Always present in memory: uploading a document has none of the consent
        # weight of capturing patient audio, and the portal needs somewhere to
        # write for a local run to be usable at all. Retrieval still stays off
        # until an ``embedder`` is supplied.
        documents=MemoryClinicDocumentStore(channel),
        embedder=embedder,
        config_extractor=config_extractor,
    )
    return ClinicFrontDeskApplication(
        channel=channel,
        stores=stores,
        stream=stream,
        stream_factory=stream_factory,
        model=model,
        system_prompt=system_prompt,
        analysis_interval=analysis_interval,
        synthesizer=synthesizer,
        failure_recorder=failure_recorder,
        now_provider=now_provider,
    )


def build_dynamo_application(
    table: Any,
    *,
    stream: VoiceStream | None = None,
    stream_factory: Callable[[], VoiceStream] | None = None,
    model: Any | None = None,
    system_prompt: str = ADMINISTRATIVE_ONLY_SYSTEM_PROMPT,
    analysis_interval: timedelta = DEFAULT_INTERVAL,
    failure_recorder: FailureRecorder = _noop_recorder,
    now_provider: Callable[[], ISODate] = _utc_today,
    recordings: CallRecordingStore | None = None,
    documents: ClinicDocumentStore | None = None,
    embedder: Embedder | None = None,
    config_extractor: ConfigExtractor | None = None,
) -> ClinicFrontDeskApplication:
    """Wire a :class:`ClinicFrontDeskApplication` over DynamoDB stores.

    Identical composition to :func:`build_memory_application`, swapping only the
    store implementations (Req 16.5): all seven DynamoDB stores are built over
    the one ``table`` sharing the one :class:`DashboardChannel` emitter.

    Args:
        table: A boto3 DynamoDB ``Table`` resource (e.g. from
            :func:`clinic_front_desk.data_layer.dynamodb.create_table`). Kept as
            a parameter — never constructed here — so the boto3 boundary stays
            outside this framework-agnostic module.
        recordings: An optional :class:`CallRecordingStore` for call audio (e.g.
            :func:`clinic_front_desk.data_layer.s3.create_recording_store`).
            ``None`` — the default — means calls are not recorded at all.
        documents: An optional :class:`ClinicDocumentStore` for the doctor's
            uploads (e.g.
            :func:`clinic_front_desk.data_layer.s3.create_document_store`).
        embedder: Embeds caller questions to search ``documents``. Both this and
            ``documents`` must be supplied for the FAQ tool to consult uploads;
            with only ``documents`` the portal can still accept and list files.
    """
    from clinic_front_desk.data_layer.dynamodb import create_stores

    channel = DashboardChannel()
    dynamo = create_stores(table, channel)
    stores = ApplicationStores(
        appointments=dynamo.appointments,
        patients=dynamo.patients,
        waitlist=dynamo.waitlist,
        decisions=dynamo.decisions,
        knowledge_base=dynamo.clinic_knowledge_base,
        call_sessions=dynamo.call_sessions,
        escalations=dynamo.escalations,
        recordings=recordings,
        documents=documents,
        embedder=embedder,
        config_extractor=config_extractor,
    )
    return ClinicFrontDeskApplication(
        channel=channel,
        stores=stores,
        stream=stream,
        stream_factory=stream_factory,
        model=model,
        system_prompt=system_prompt,
        analysis_interval=analysis_interval,
        failure_recorder=failure_recorder,
        now_provider=now_provider,
    )
