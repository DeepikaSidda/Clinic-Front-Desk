"""AgentCore Runtime entrypoint scaffolding (task 14.1, Req 1.8, 16.1).

This module turns the framework-agnostic
:class:`~clinic_front_desk.deployment.app.ClinicFrontDeskApplication` into the
two deployable Amazon Bedrock AgentCore Runtime entrypoints the design's
"Runtime Topology" calls for:

- a **voice runtime handler** that launches the Voice_Front_Desk over AgentCore's
  bidirectional WebSocket transport, and
- a **scheduled handler** that fires one Practice_Intelligence analysis run.

Both share the one Data_Layer and the one :class:`~clinic_front_desk.dashboard.pubsub.DashboardChannel`
held by the application (Req 16.1).

Documented assumptions about the (experimental) AgentCore / Strands API
-----------------------------------------------------------------------
The Strands ``BidiAgent`` and the Bedrock AgentCore Runtime APIs are
**experimental and evolving**, so every real dependency is kept behind an
adapter and imported lazily. The concrete assumptions this scaffolding encodes —
each isolated so a single SDK change touches one place — are:

1. **Voice transport.** AgentCore exposes the Voice_Front_Desk over a persistent
   bidirectional WebSocket. The audio actually flows patient ↔ Nova Sonic
   *inside* the Strands ``BidiAgent`` that
   :class:`~clinic_front_desk.voice.stream.NovaSonicVoiceStream` drives; the
   runtime's job is only to (a) accept a connection, (b) create one
   :class:`~clinic_front_desk.voice.agent.VoiceSession` per connection reading
   live config (Req 1.8), and (c) drive its ``start`` → ``run`` → ``finalize``
   lifecycle. We therefore do **not** assume AgentCore hands us raw frames — the
   ``BidiAgent`` owns the socket. If a future SDK requires per-frame pumping,
   that belongs inside ``NovaSonicVoiceStream`` only.

2. **Entrypoint registration.** AgentCore apps register handlers via a decorator
   / registration call on an app object (e.g. ``BedrockAgentCoreApp`` with an
   ``@app.entrypoint`` decorator). We treat that object abstractly through the
   :class:`RuntimeApp` Protocol and never import a concrete one at module load.

3. **Scheduling.** The recurring ≤ 24 h cadence (Req 13.1) is enforced by
   :class:`~clinic_front_desk.intelligence.scheduler.AnalysisScheduler`. In
   deployment the actual timer is external (an AgentCore schedule / EventBridge
   rule / cron) that simply invokes the scheduled handler; the handler runs one
   analysis pass. The scheduler object is available for in-process drivers and
   to assert the interval invariant.

4. **Credentials / region / table.** All AWS resource construction (the boto3
   DynamoDB table, the Nova Sonic model binding) is confined to
   :func:`build_runtime_application` and the ``NovaSonicVoiceStream`` adapter,
   parameterised by :class:`RuntimeConfig`. Nothing here performs network I/O at
   import time.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from clinic_front_desk.models import CallOutcome, is_ok
from clinic_front_desk.voice.stream import NovaSonicVoiceStream

from .app import ClinicFrontDeskApplication, build_dynamo_application

__all__ = [
    "RuntimeConfig",
    "RuntimeApp",
    "VoiceEntrypoint",
    "ScheduledEntrypoint",
    "build_voice_websocket_entrypoint",
    "build_scheduled_intelligence_entrypoint",
    "build_runtime_application",
    "register_entrypoints",
]


# ---------------------------------------------------------------------------
# Runtime configuration (the only place AWS specifics are named).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeConfig:
    """Deployment configuration for the AgentCore Runtime.

    Keeps every environment-specific value in one place so the entrypoints and
    the application composition stay free of hard-coded resource names.

    Attributes:
        table_name: DynamoDB single-table name backing the Data_Layer.
        region: AWS region for DynamoDB and Bedrock.
        nova_sonic_model_id: The Nova Sonic model id bound to the ``BidiAgent``.
            Defaults to :attr:`NovaSonicVoiceStream.DEFAULT_MODEL_ID` rather than
            a literal, so the deployed model can never drift away from the one
            the voice adapter actually builds. Note the id matters beyond naming:
            the adapter only sends the ``turn_detection`` provider config for the
            v2 model (``amazon.nova-2-sonic-v1:0``), so pinning the v1 id here
            would silently drop barge-in endpointing sensitivity.
        analysis_interval_hours: Practice_Intelligence cadence; the scheduler
            clamps this to ≤ 24 h (Req 13.1).
        create_table_if_missing: Whether :func:`build_runtime_application`
            bootstraps the table when absent (useful for first deploy / local).
        endpoint_url: Optional DynamoDB endpoint override (e.g. DynamoDB-local).
    """

    table_name: str = "clinic-front-desk"
    region: str = "us-east-1"
    nova_sonic_model_id: str = NovaSonicVoiceStream.DEFAULT_MODEL_ID
    analysis_interval_hours: float = 24.0
    create_table_if_missing: bool = False
    endpoint_url: str | None = None
    #: S3 bucket for call recordings. Empty/``None`` means **calls are not
    #: recorded** — recording never switches itself on, it has to be configured.
    recordings_bucket: str | None = None
    #: Key prefix inside the recordings bucket.
    recordings_prefix: str = "call-recordings"
    #: Server-side encryption for recordings: ``AES256`` or ``aws:kms``.
    recordings_sse: str = "AES256"
    #: KMS key id, required when ``recordings_sse`` is ``aws:kms``.
    recordings_kms_key_id: str | None = None
    #: S3 bucket for the doctor's uploaded clinic documents. Empty/``None`` means
    #: no uploads and no document-backed answers. May be the same bucket as
    #: ``recordings_bucket`` — the prefixes keep them apart — which is the usual
    #: choice, since one bucket's access controls and encryption are one thing to
    #: get right instead of two.
    documents_bucket: str | None = None
    #: Key prefix inside the documents bucket.
    documents_prefix: str = "clinic-documents"
    #: Server-side encryption for documents: ``AES256`` or ``aws:kms``.
    documents_sse: str = "AES256"
    #: KMS key id, required when ``documents_sse`` is ``aws:kms``.
    documents_kms_key_id: str | None = None
    #: Bedrock embedding model used to search the documents. Retrieval needs both
    #: this and ``documents_bucket``.
    embedding_model_id: str = "amazon.titan-embed-text-v2:0"
    #: Bedrock text model that reads clinic details out of a document to pre-fill
    #: the onboarding wizard. Must support ``converse`` tool use.
    extraction_model_id: str = "amazon.nova-lite-v1:0"


# ---------------------------------------------------------------------------
# Abstract runtime surfaces (kept provider-agnostic).
# ---------------------------------------------------------------------------


#: A voice entrypoint handles one connected Call_Session and returns the final
#: outcome. It is ``async`` because the ``BidiAgent`` stream lifecycle is async.
VoiceEntrypoint = Callable[..., Awaitable["CallOutcome | None"]]

#: A scheduled entrypoint runs one analysis pass and returns a summary payload.
ScheduledEntrypoint = Callable[..., dict[str, Any]]


@runtime_checkable
class RuntimeApp(Protocol):
    """The AgentCore app object entrypoints register with (assumption #2).

    Modelled abstractly so the concrete ``BedrockAgentCoreApp`` (or whatever the
    experimental SDK provides) is never imported here. A real app exposes an
    ``entrypoint`` decorator; :func:`register_entrypoints` uses it if present.
    """

    def entrypoint(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """Register ``func`` as a runtime entrypoint and return it."""
        ...


# ---------------------------------------------------------------------------
# Entrypoint builders (fully testable against the memory application).
# ---------------------------------------------------------------------------


def build_voice_websocket_entrypoint(
    app: ClinicFrontDeskApplication,
) -> VoiceEntrypoint:
    """Build the voice runtime handler over the bidirectional WebSocket (assumption #1).

    The returned coroutine is what AgentCore invokes per connected call. It:

    1. starts a **fresh** :class:`VoiceSession` (so the guardrail + tools read
       clinic config live — Req 1.8),
    2. drives the ``BidiAgent`` stream lifecycle (``start`` → ``run``), and
    3. finalizes the Call_Session outcome on end (Req 11.5), defaulting to
       ``interrupted`` if the stream ends without the orchestration recording an
       outcome (Req 12.7).

    Args:
        app: The composed application whose shared Data_Layer the session uses.

    Returns:
        An async ``handler(session_id=None)`` returning the persisted
        :class:`~clinic_front_desk.models.CallOutcome` (or ``None`` if unknown).
    """

    async def handle_voice_connection(
        session_id: str | None = None,
    ) -> CallOutcome | None:
        session = app.start_voice_session(session_id)
        try:
            await session.start()
            await session.run()
        finally:
            # Persist the outcome the orchestration recorded; if none was set the
            # call ended without completing a task, so record it as interrupted
            # (Req 12.7). finalize is idempotent enough for a single call here.
            recorded = session.context.outcome
            result = session.finalize(recorded or CallOutcome.INTERRUPTED)
        if is_ok(result):
            return result.value.outcome
        return None

    return handle_voice_connection


def build_scheduled_intelligence_entrypoint(
    app: ClinicFrontDeskApplication,
) -> ScheduledEntrypoint:
    """Build the scheduled Practice_Intelligence handler (Req 13.1, 13.2).

    The returned callable is what an external AgentCore schedule / EventBridge
    rule invokes on the recurring cadence. It runs exactly one analysis pass —
    assembling the snapshot from the shared Data_Layer, running
    ``analyze_patterns`` + ``DecisionSynthesizer`` — and returns a small summary
    of what happened, suitable as a handler response / log line.

    Args:
        app: The composed application whose scheduler / synthesizer this drives.

    Returns:
        A ``handler(event=None, context=None)`` returning a summary dict.
    """

    def handle_scheduled_analysis(
        event: Any = None, context: Any = None
    ) -> dict[str, Any]:
        result = app.run_intelligence()
        return {
            "analysis_failed": result.analysis_failed,
            "decisions_created": len(result.created),
            "skipped_non_actionable": len(result.skipped_non_actionable),
            "skipped_below_threshold": len(result.skipped_below_threshold),
            "skipped_duplicate": len(result.skipped_duplicate),
            "retained": len(result.retained),
        }

    return handle_scheduled_analysis


# ---------------------------------------------------------------------------
# Production application builder (the AWS boundary).
# ---------------------------------------------------------------------------


def build_runtime_application(
    config: RuntimeConfig,
    *,
    stream: Any | None = None,
    stream_factory: Callable[[], Any] | None = None,
) -> ClinicFrontDeskApplication:
    """Build the production application over DynamoDB (assumption #4).

    All boto3 / AWS construction is confined here and imported lazily, so the
    rest of the deployment package (and its tests) never touch the network. The
    returned application is identical in shape to the in-memory one — the only
    difference is the DynamoDB-backed stores (Req 16.5).

    Args:
        config: The deployment configuration (table, region, model, cadence).
        stream / stream_factory: Optional voice-stream overrides; when omitted
            each voice agent builds its own Nova Sonic stream bound to
            ``config.nova_sonic_model_id``.

    Returns:
        A DynamoDB-backed :class:`ClinicFrontDeskApplication`.
    """
    from datetime import timedelta

    # Lazy: keep the AWS SDK out of import time. boto3 has no py.typed marker.
    import boto3  # type: ignore[import-untyped]

    from clinic_front_desk.data_layer.dynamodb import create_table, table_exists

    session = boto3.resource(
        "dynamodb", region_name=config.region, endpoint_url=config.endpoint_url
    )
    if config.create_table_if_missing and not table_exists(session, config.table_name):
        table = create_table(session, config.table_name)
    else:
        table = session.Table(config.table_name)

    # Call recording is built only when a bucket is named, so the default
    # deployment captures no patient audio at all.
    recordings = None
    if config.recordings_bucket:
        from clinic_front_desk.data_layer.s3 import create_recording_store

        recordings = create_recording_store(
            config.recordings_bucket,
            region=config.region,
            prefix=config.recordings_prefix,
            sse=config.recordings_sse,
            kms_key_id=config.recordings_kms_key_id,
        )

    # Likewise the document corpus: no bucket, no uploads and no document-backed
    # answers. The embedder is only built alongside it, since an embedder with
    # nothing to search would be a Bedrock client held open for no reason.
    documents = None
    embedder = None
    config_extractor = None
    if config.documents_bucket:
        from clinic_front_desk.data_layer.s3 import create_document_store
        from clinic_front_desk.documents import create_config_extractor, create_embedder

        documents = create_document_store(
            config.documents_bucket,
            region=config.region,
            prefix=config.documents_prefix,
            sse=config.documents_sse,
            kms_key_id=config.documents_kms_key_id,
        )
        embedder = create_embedder(
            region=config.region, model_id=config.embedding_model_id
        )
        config_extractor = create_config_extractor(
            region=config.region, model_id=config.extraction_model_id
        )

    return build_dynamo_application(
        table,
        stream=stream,
        stream_factory=stream_factory,
        model=config.nova_sonic_model_id,
        analysis_interval=timedelta(hours=config.analysis_interval_hours),
        recordings=recordings,
        documents=documents,
        embedder=embedder,
        config_extractor=config_extractor,
    )


def register_entrypoints(
    runtime_app: RuntimeApp, app: ClinicFrontDeskApplication
) -> dict[str, Callable[..., Any]]:
    """Register both entrypoints on an AgentCore app object (assumption #2).

    Builds the voice and scheduled handlers over ``app`` and registers them with
    ``runtime_app`` using its ``entrypoint`` decorator when available (the
    concrete ``BedrockAgentCoreApp`` API is experimental, so this is best-effort
    and never imported here). Returns the handlers by name regardless, so the
    caller can wire them manually if the app object differs.

    Returns:
        ``{"voice": <voice handler>, "scheduled": <scheduled handler>}``.
    """
    voice = build_voice_websocket_entrypoint(app)
    scheduled = build_scheduled_intelligence_entrypoint(app)

    register = getattr(runtime_app, "entrypoint", None)
    if callable(register):
        register(voice)
        register(scheduled)

    return {"voice": voice, "scheduled": scheduled}
