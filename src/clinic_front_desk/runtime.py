"""Package-root AgentCore Runtime entrypoint surface (task 14.1; Req 1.8, 16.1).

This module is the stable public home for the Amazon Bedrock AgentCore Runtime
wiring: the two deployable entrypoints the design's "Runtime Topology" calls for
— a bidirectional-WebSocket **voice** handler and a **scheduled** Practice_
Intelligence handler — plus the :class:`RuntimeConfig` that names every
environment-specific value and the production application builder.

The concrete scaffolding lives in :mod:`clinic_front_desk.deployment.runtime`,
where every dependency on the *experimental* AgentCore / Strands ``BidiAgent``
API is isolated behind a thin adapter and documented as an explicit assumption.
This module simply re-exports it under package-root names so callers write
``from clinic_front_desk.runtime import build_runtime_application`` rather than
reaching into the deployment sub-package.

Pair this with :func:`clinic_front_desk.app.build_application`: build the
composed application, then either register both entrypoints on an AgentCore app
object via :func:`register_entrypoints`, or wire them yourself with
:func:`build_voice_websocket_entrypoint` and
:func:`build_scheduled_intelligence_entrypoint`.
"""

from __future__ import annotations

from clinic_front_desk.deployment.runtime import (
    RuntimeApp,
    RuntimeConfig,
    ScheduledEntrypoint,
    VoiceEntrypoint,
    build_runtime_application,
    build_scheduled_intelligence_entrypoint,
    build_voice_websocket_entrypoint,
    register_entrypoints,
)

__all__ = [
    "RuntimeApp",
    "RuntimeConfig",
    "ScheduledEntrypoint",
    "VoiceEntrypoint",
    "build_runtime_application",
    "build_scheduled_intelligence_entrypoint",
    "build_voice_websocket_entrypoint",
    "register_entrypoints",
]
