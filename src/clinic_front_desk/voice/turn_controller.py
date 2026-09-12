"""``TurnController`` — the Voice_Front_Desk turn-level state machine.

Task 7.7 (Req 12.4, 12.5, 12.6, 12.7). See design "Voice_Front_Desk"
sub-components and the Error Handling "Voice-layer errors" table.

The controller is modelled as a **deterministic state machine** that, given a
voice-layer event, returns the *action to take* rather than performing any side
effect itself. This keeps it pure and lets it be unit/property tested without a
real audio stream (design: "The Nova Sonic stream is mocked at the
``VoiceStreamManager`` boundary"). The surrounding orchestration (task 9.2)
turns the returned actions into concrete behaviour — speaking a re-ask prompt,
invoking the ``flag_for_human`` tool, finalizing the Call_Session, etc.

Handled events and their bounded behaviour:

- **Interpretation failure** (:meth:`TurnController.on_interpretation_failure`):
  consecutive uninterpretable turns for the *same request* are bounded to a
  maximum of :data:`TurnController.MAX_INTERPRETATION_ATTEMPTS` (2). Each failure
  below the maximum yields a :class:`ReAsk` (ask the patient to repeat or
  rephrase, Req 12.4); reaching the maximum yields an :class:`Escalate` that
  signals a ``flag_for_human`` escalation and a human-follow-up message
  (Req 12.5). This is Property 16: *the agent re-asks at most twice and, upon
  the second consecutive failure, invokes ``flag_for_human``*.
- **Silence timeout** (:meth:`TurnController.on_silence_timeout`): after
  :data:`TurnController.SILENCE_TIMEOUT_SECONDS` (10 s) of no speech the patient
  is re-prompted exactly **once** (Req 12.6); further silence yields
  :class:`NoAction`.
- **Voice-layer loss** (:meth:`TurnController.on_voice_layer_lost`): signals
  :class:`EndSession` — inform the patient, end the Call_Session, and record the
  outcome as :data:`~clinic_front_desk.models.CallOutcome.INTERRUPTED`
  (Req 12.7). This is terminal; subsequent events yield :class:`NoAction`.

A successfully interpreted turn (:meth:`TurnController.on_interpretable_turn`)
ends the current "same request" run: it resets the consecutive-failure counter
and the silence re-prompt latch so the bounds apply per request, not per call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Union

from clinic_front_desk.models import CallOutcome, EscalationReason

# ---------------------------------------------------------------------------
# Actions — a discriminated union describing what the caller should do next.
# Mirrors the ``ToolError`` union style: one frozen dataclass per ``kind``.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReAsk:
    """Ask the patient to repeat or rephrase the request (Req 12.4).

    ``attempt`` is the 1-based index of the consecutive interpretation failure
    that produced this re-ask (always ``< MAX_INTERPRETATION_ATTEMPTS``).
    """

    attempt: int
    kind: Literal["re_ask"] = "re_ask"


@dataclass(frozen=True)
class Escalate:
    """Signal a ``flag_for_human`` escalation after the retry bound is hit (Req 12.5).

    The controller does not call the tool itself; it returns this signal so the
    orchestration can invoke ``flag_for_human`` with the session context and
    inform the patient that a human will follow up.
    """

    reason: EscalationReason = EscalationReason.OUTSIDE_ADMIN_RULES
    inform_human_follow_up: bool = True
    kind: Literal["escalate"] = "escalate"


@dataclass(frozen=True)
class RePrompt:
    """Re-prompt the patient once after a silence timeout (Req 12.6)."""

    kind: Literal["re_prompt"] = "re_prompt"


@dataclass(frozen=True)
class EndSession:
    """Signal that the Call_Session must end after voice-layer loss (Req 12.7).

    Instructs the caller to inform the patient that the call cannot continue,
    end the Call_Session, and record its outcome (``interrupted``) through the
    Data_Layer.
    """

    outcome: CallOutcome = CallOutcome.INTERRUPTED
    inform_patient: bool = True
    kind: Literal["end_session"] = "end_session"


@dataclass(frozen=True)
class NoAction:
    """No turn-level action is required for this event."""

    kind: Literal["no_action"] = "no_action"


#: The discriminated union of actions a :class:`TurnController` can return.
TurnAction = Union[ReAsk, Escalate, RePrompt, EndSession, NoAction]


class TurnController:
    """Deterministic per-Call_Session turn controller (Req 12.4–12.7).

    A single instance tracks the state of one Call_Session. It is intentionally
    side-effect free: every event handler mutates only the controller's own
    counters/latches and returns a :data:`TurnAction`.
    """

    #: Maximum consecutive interpretation-failure attempts for the same request
    #: before escalating (Req 12.4, 12.5).
    MAX_INTERPRETATION_ATTEMPTS: int = 2

    #: Silence duration, in seconds, that triggers a single re-prompt (Req 12.6).
    SILENCE_TIMEOUT_SECONDS: float = 10.0

    def __init__(self) -> None:
        self._interpretation_failures: int = 0
        self._silence_reprompted: bool = False
        self._escalated: bool = False
        self._ended: bool = False

    # -- read-only state (useful for orchestration and tests) ---------------

    @property
    def consecutive_interpretation_failures(self) -> int:
        """Consecutive uninterpretable turns for the current request."""
        return self._interpretation_failures

    @property
    def escalated(self) -> bool:
        """Whether the current request has been escalated to a human."""
        return self._escalated

    @property
    def silence_reprompted(self) -> bool:
        """Whether the one allowed silence re-prompt has been issued."""
        return self._silence_reprompted

    @property
    def ended(self) -> bool:
        """Whether the Call_Session has been ended (voice-layer loss)."""
        return self._ended

    # -- events -------------------------------------------------------------

    def on_interpretation_failure(self) -> TurnAction:
        """Handle a turn whose speech could not be interpreted (Req 12.4, 12.5).

        Bounds consecutive re-asks for the same request to
        :data:`MAX_INTERPRETATION_ATTEMPTS`. Returns :class:`ReAsk` while below
        the bound and :class:`Escalate` upon reaching it (the second consecutive
        failure). Once escalated (or once the session has ended), further
        failures for the same request yield :class:`NoAction` so the escalation
        fires exactly once and the re-ask count never exceeds the bound.
        """
        if self._ended or self._escalated:
            return NoAction()

        self._interpretation_failures += 1
        if self._interpretation_failures >= self.MAX_INTERPRETATION_ATTEMPTS:
            # Reached the maximum of 2 consecutive attempts -> escalate (Req 12.5).
            self._escalated = True
            return Escalate()

        # Still under the maximum -> ask to repeat or rephrase (Req 12.4).
        return ReAsk(attempt=self._interpretation_failures)

    def on_interpretable_turn(self) -> TurnAction:
        """Reset per-request bounds after a successfully interpreted turn.

        A successful turn ends the current "same request" run: the
        consecutive-failure counter, the escalation latch, and the silence
        re-prompt latch are cleared so Req 12.4–12.6 apply per request. No
        turn-level action is required here (the interpreted turn is handled by
        the tool orchestration), so :class:`NoAction` is returned.
        """
        if self._ended:
            return NoAction()
        self._interpretation_failures = 0
        self._escalated = False
        self._silence_reprompted = False
        return NoAction()

    def on_silence_timeout(self) -> TurnAction:
        """Handle 10 s of no speech: re-prompt the patient once (Req 12.6).

        The first silence timeout yields :class:`RePrompt`; any subsequent
        timeout before the patient speaks again yields :class:`NoAction`. The
        latch resets on the next interpretable turn.
        """
        if self._ended or self._silence_reprompted:
            return NoAction()
        self._silence_reprompted = True
        return RePrompt()

    def on_voice_layer_lost(self) -> TurnAction:
        """Handle voice-layer loss (Req 12.7).

        Signals :class:`EndSession` — inform the patient, end the Call_Session,
        and record the outcome as ``interrupted``. This is terminal and
        idempotent: after the first loss the session is marked ended and any
        further event yields :class:`NoAction`, so the outcome is recorded once.
        """
        if self._ended:
            return NoAction()
        self._ended = True
        return EndSession(outcome=CallOutcome.INTERRUPTED)


__all__ = [
    "ReAsk",
    "Escalate",
    "RePrompt",
    "EndSession",
    "NoAction",
    "TurnAction",
    "TurnController",
]
