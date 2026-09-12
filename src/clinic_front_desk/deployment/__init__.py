"""AgentCore Runtime deployment wiring (task 14.1, Req 1.8, 16.1).

This package is the *composition root* for the whole system. It ties the four
independently built pieces together into deployable AgentCore Runtime
entrypoints, all sharing **one** Data_Layer (Req 16.1):

- **Voice_Front_Desk** — the reactive, patient-facing Strands ``BidiAgent``
  voiced by Nova Sonic, exposed over AgentCore's bidirectional WebSocket
  transport (design "Runtime Topology").
- **Practice_Intelligence** — the scheduled analysis entrypoint that assembles a
  :class:`~clinic_front_desk.intelligence.detectors.PatternInput` snapshot from
  the stores and runs ``analyze_patterns`` + ``DecisionSynthesizer`` on the
  :class:`~clinic_front_desk.intelligence.scheduler.AnalysisScheduler` cadence
  (≤ 24 h, Req 13.1).
- **Data_Layer** — the swappable store library, wired once with the
  :class:`~clinic_front_desk.dashboard.pubsub.DashboardChannel` as the shared
  :class:`~clinic_front_desk.data_layer.events.ChangeEmitter` so every mutation
  fans out to dashboard clients (Req 16.1).
- **Dashboard BFF** — the thin backend that reads exclusively through the
  Data_Layer and subscribes to the same channel for real-time updates.

Two public surfaces:

- :mod:`~clinic_front_desk.deployment.app` — the framework-agnostic
  :class:`~clinic_front_desk.deployment.app.ClinicFrontDeskApplication`
  composition factory. Fully unit-testable against in-memory fakes and a fake
  voice stream (:func:`~clinic_front_desk.deployment.app.build_memory_application`).
- :mod:`~clinic_front_desk.deployment.runtime` — the AgentCore Runtime
  entrypoint scaffolding (voice WebSocket launcher + scheduled intelligence
  handler) with all real AWS calls kept behind adapters and documented
  assumptions about the experimental AgentCore / Strands ``BidiAgent`` API.
- :mod:`~clinic_front_desk.deployment.server` — the deployable container surface:
  an ASGI app implementing the AgentCore Runtime HTTP protocol contract
  (``GET /ping``, ``POST /invocations``, ``WebSocket /ws``) on port 8080. This is
  what the ``Dockerfile`` runs. Needs the ``deploy`` extra (Starlette + uvicorn),
  so it is imported lazily rather than re-exported here — use
  ``from clinic_front_desk.deployment.server import create_asgi_app``.
"""

from __future__ import annotations

from .app import (
    ApplicationStores,
    ClinicFrontDeskApplication,
    build_dynamo_application,
    build_memory_application,
)
from .runtime import (
    RuntimeConfig,
    build_scheduled_intelligence_entrypoint,
    build_voice_websocket_entrypoint,
)

__all__ = [
    "ApplicationStores",
    "ClinicFrontDeskApplication",
    "build_memory_application",
    "build_dynamo_application",
    "RuntimeConfig",
    "build_voice_websocket_entrypoint",
    "build_scheduled_intelligence_entrypoint",
]
