"""Voice_Front_Desk orchestration and Amazon Nova Sonic integration.

This package builds up, across tasks 7.x and 9.x, the reactive patient-facing
agent:

- :class:`~clinic_front_desk.voice.session_context.SessionContext` — per-call
  fact retention and task step index (Req 11.1, 11.4, 12.3).
- :class:`~clinic_front_desk.voice.guardrails.GuardrailPolicy` and the
  :data:`~clinic_front_desk.voice.prompts.ADMINISTRATIVE_ONLY_SYSTEM_PROMPT` —
  the defense-in-depth administrative-only guardrail (Req 10), fed by
  :func:`~clinic_front_desk.voice.turn_signals.extract_turn`, which turns a
  patient transcript into the policy's structured signals so the tool-layer
  backstop runs on every live turn without depending on the model's discretion.
- :class:`~clinic_front_desk.voice.tool_orchestrator.ToolOrchestrator` — tool
  chaining and confirm-before-mutate (Req 11.2, 11.3, 4.5, 5.6).
- :class:`~clinic_front_desk.voice.turn_controller.TurnController` and
  :class:`~clinic_front_desk.voice.barge_in.BargeInHandler` — turn-level bounds
  and barge-in resume (Req 12.3-12.7).
- :class:`~clinic_front_desk.voice.stream.VoiceStreamManager` over the Strands
  ``BidiAgent`` + Nova Sonic stream, behind the
  :class:`~clinic_front_desk.voice.stream.VoiceStream` boundary (Req 12.1, 12.2).
- :class:`~clinic_front_desk.voice.agent.VoiceFrontDeskAgent` — the end-to-end
  composition root wiring the ten patient-facing tools, the guardrail prompt,
  and the orchestration to the voice stream (task 9.2, Req 2.4, 3.2, 11.2, 12.3).
"""

from __future__ import annotations

from .agent import (
    PATIENT_FACING_TOOL_NAMES,
    BoundToolset,
    VoiceFrontDeskAgent,
    VoiceFrontDeskStores,
    VoiceSession,
    build_patient_facing_tools,
    create_voice_front_desk_agent,
)
from .barge_in import BargeInCheckpoint, BargeInHandler
from .guardrails import GuardrailDecision, GuardrailPolicy, Turn, TurnClassification
from .prompts import (
    ADMINISTRATIVE_ONLY_SYSTEM_PROMPT,
    RECORDING_NOTICE_INSTRUCTION,
    with_recording_notice,
)
from .turn_signals import (
    ExtractedTurn,
    extract_turn,
    normalize_transcript,
    offers_escalation,
)
from .session_context import SessionContext
from .session_lifecycle import (
    AcceptingCalls,
    IntakeSignal,
    NotAcceptingCalls,
    check_intake_availability,
    evaluate_intake,
    finalize_session,
)
from .stream import (
    NovaSonicVoiceStream,
    VoiceStream,
    VoiceStreamManager,
)
from .tool_orchestrator import (
    ChainCompleted,
    ChainStep,
    MutationCommitted,
    MutationDeclined,
    NoAlternativeSlots,
    OfferToTakeMessage,
    ToolOrchestrator,
)
from .turn_controller import (
    EndSession,
    Escalate,
    NoAction,
    ReAsk,
    RePrompt,
    TurnController,
)

__all__ = [
    # composition root (task 9.2)
    "VoiceFrontDeskAgent",
    "VoiceFrontDeskStores",
    "VoiceSession",
    "BoundToolset",
    "build_patient_facing_tools",
    "create_voice_front_desk_agent",
    "PATIENT_FACING_TOOL_NAMES",
    # guardrails / prompt
    "GuardrailPolicy",
    "GuardrailDecision",
    "Turn",
    "TurnClassification",
    "ADMINISTRATIVE_ONLY_SYSTEM_PROMPT",
    "RECORDING_NOTICE_INSTRUCTION",
    "with_recording_notice",
    # transcript -> guardrail signals (the tool-layer backstop's input)
    "extract_turn",
    "ExtractedTurn",
    "normalize_transcript",
    "offers_escalation",
    # session context + lifecycle
    "SessionContext",
    "finalize_session",
    "check_intake_availability",
    "evaluate_intake",
    "AcceptingCalls",
    "NotAcceptingCalls",
    "IntakeSignal",
    # orchestration
    "ToolOrchestrator",
    "ChainStep",
    "ChainCompleted",
    "OfferToTakeMessage",
    "MutationCommitted",
    "MutationDeclined",
    "NoAlternativeSlots",
    # turn control + barge-in
    "TurnController",
    "ReAsk",
    "Escalate",
    "RePrompt",
    "EndSession",
    "NoAction",
    "BargeInHandler",
    "BargeInCheckpoint",
    # voice stream boundary
    "VoiceStreamManager",
    "VoiceStream",
    "NovaSonicVoiceStream",
]
