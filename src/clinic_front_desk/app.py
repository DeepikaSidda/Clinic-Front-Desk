"""Package-root application composition entry point (task 14.1; Req 1.8, 16.1).

This is the documented public composition root for the whole system. It exposes
a single :func:`build_application` factory that assembles the four subsystems —
the Voice_Front_Desk, the scheduled Practice_Intelligence run, the shared
Data_Layer, and the Dashboard BFF — against **one** set of stores sharing **one**
:class:`~clinic_front_desk.dashboard.pubsub.DashboardChannel`, so every mutation
fans out to dashboard clients (Req 16.1).

The concrete composition (:class:`ClinicFrontDeskApplication` and the
in-memory / DynamoDB factories) lives in
:mod:`clinic_front_desk.deployment.app`; this module re-exports it under stable
package-root names and adds the :func:`build_application` dispatch factory the
design's "Runtime Topology" calls for. The AgentCore Runtime entrypoint
scaffolding is re-exported from :mod:`clinic_front_desk.runtime`.

Choosing a backend
-------------------
- **Local / demo / tests:** call ``build_application()`` (or pass an explicit
  in-memory ``stores`` bundle). No AWS credentials, network, or Bedrock needed —
  the whole app assembles against in-memory fakes and a fake voice stream, which
  is what the wiring smoke test exercises.
- **Production:** pass a boto3 DynamoDB ``table`` (see
  :func:`clinic_front_desk.data_layer.dynamodb.create_table`) via ``table=...``,
  or a fully-built :class:`RuntimeConfig` through
  :func:`clinic_front_desk.runtime.build_runtime_application`. The composition is
  identical; only the store implementations differ (Req 16.5).

Live clinic-config propagation (Req 1.8) is a property of the composition itself:
each Call_Session gets a freshly built voice agent that reads the offered-service
list live from the ``ClinicKnowledgeBaseStore``, and the patient-facing tools
read the knowledge base through the store on every call — so a config save is
reflected in responses that begin afterwards, with no restart. See
:mod:`clinic_front_desk.deployment.app` for the full rationale.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

from clinic_front_desk.dashboard.pubsub import DashboardChannel
from clinic_front_desk.deployment.app import (
    ApplicationStores,
    ClinicFrontDeskApplication,
    build_dynamo_application,
    build_memory_application,
)
from clinic_front_desk.intelligence.scheduler import DEFAULT_INTERVAL
from clinic_front_desk.intelligence.synthesizer import (
    DecisionSynthesizer,
    FailureRecorder,
    _noop_recorder,
)
from clinic_front_desk.models import ISODate
from clinic_front_desk.voice import ADMINISTRATIVE_ONLY_SYSTEM_PROMPT
from clinic_front_desk.voice.stream import VoiceStream

#: Public alias for the composed application type (the design's "Application").
Application = ClinicFrontDeskApplication

__all__ = [
    "Application",
    "ApplicationStores",
    "ClinicFrontDeskApplication",
    "build_application",
    "build_memory_application",
    "build_dynamo_application",
]


def build_application(
    *,
    stores: ApplicationStores | None = None,
    channel: DashboardChannel | None = None,
    table: Any | None = None,
    stream: VoiceStream | None = None,
    stream_factory: Callable[[], VoiceStream] | None = None,
    model: Any | None = None,
    system_prompt: str = ADMINISTRATIVE_ONLY_SYSTEM_PROMPT,
    analysis_interval: timedelta = DEFAULT_INTERVAL,
    synthesizer: DecisionSynthesizer | None = None,
    failure_recorder: FailureRecorder = _noop_recorder,
    now_provider: Callable[[], ISODate] | None = None,
    offered_services: Sequence[str] | None = None,
) -> Application:
    """Assemble the whole application against a chosen Data_Layer backend.

    This is the single composition factory the design's "Runtime Topology" calls
    for. It wires the Voice_Front_Desk, the scheduled Practice_Intelligence run,
    the shared Data_Layer, and the Dashboard BFF over **one** store bundle and
    **one** :class:`DashboardChannel` (Req 16.1), then returns the composed
    :class:`Application`.

    Backend selection (mutually exclusive):

    - ``stores`` + ``channel`` — compose over a caller-provided Data_Layer
      bundle. ``channel`` MUST be the same :class:`DashboardChannel` those stores
      were constructed with as their ``ChangeEmitter``, otherwise mutations will
      not fan out. Use this when you need direct control over the store
      instances (advanced / custom deployments).
    - ``table`` — compose over DynamoDB stores built on a boto3 ``Table``
      (production). A fresh shared channel is created internally.
    - neither — compose over in-memory fake stores (local / demo / tests). A
      fresh shared channel is created internally.

    Args:
        stores: A pre-built shared Data_Layer bundle. Requires ``channel``.
        channel: The shared change-event channel the ``stores`` emit on.
        table: A boto3 DynamoDB ``Table`` for the production backend.
        stream: A pre-built voice stream reused for every session (tests pass a
            fake here).
        stream_factory: A per-session voice-stream factory (production Nova
            Sonic wants one stream per call).
        model: Nova Sonic model id / ``BidiModel`` for the default stream.
        system_prompt: The guardrail system prompt (Req 10.1).
        analysis_interval: Practice_Intelligence cadence (clamped to ≤ 24 h by
            the scheduler, Req 13.1).
        synthesizer: Optional pre-built :class:`DecisionSynthesizer`.
        failure_recorder: Analysis-failure sink (Req 13.7).
        now_provider: Injectable reference-date source for the analysis window.
        offered_services: Reserved for callers that want to pin the guardrail's
            offered-service list; normally ``None`` so the agent reads live
            config from the store (Req 1.8).

    Returns:
        The composed :class:`Application` sharing one Data_Layer + one channel.

    Raises:
        ValueError: if ``stores`` is given without a matching ``channel``, or if
            both a custom ``stores`` bundle and a ``table`` are given.
    """
    if stores is not None and table is not None:
        raise ValueError("Pass either 'stores' or 'table', not both.")

    # Shared kwargs common to every construction path.
    common: dict[str, Any] = {
        "stream": stream,
        "stream_factory": stream_factory,
        "model": model,
        "system_prompt": system_prompt,
        "analysis_interval": analysis_interval,
        "failure_recorder": failure_recorder,
    }
    if now_provider is not None:
        common["now_provider"] = now_provider

    if stores is not None:
        if channel is None:
            raise ValueError(
                "When passing a custom 'stores' bundle you must also pass the "
                "'channel' those stores emit on so mutations fan out (Req 16.1)."
            )
        return ClinicFrontDeskApplication(
            channel=channel,
            stores=stores,
            synthesizer=synthesizer,
            **common,
        )

    if table is not None:
        # build_dynamo_application builds its own synthesizer over the shared
        # DynamoDB DecisionStore; it does not accept a pre-built one.
        return build_dynamo_application(table, **common)

    return build_memory_application(synthesizer=synthesizer, **common)
