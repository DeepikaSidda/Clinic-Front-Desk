"""Unit tests for the ``TurnController`` turn-level state machine (task 7.7).

Covers the four voice-layer behaviours the controller signals (Req 12.4–12.7):

- Interpretation failures re-ask below the bound and escalate on the second
  consecutive failure (Req 12.4, 12.5).
- The consecutive-failure bound resets after a successfully interpreted turn
  ("same request" scope).
- Silence re-prompts the patient exactly once (Req 12.6).
- Voice-layer loss ends the session and records the ``interrupted`` outcome,
  idempotently (Req 12.7).

The property test for bounded retries (Property 16) lives in task 7.8.
"""

from __future__ import annotations

from clinic_front_desk.models import CallOutcome, EscalationReason
from clinic_front_desk.voice.turn_controller import (
    EndSession,
    Escalate,
    NoAction,
    ReAsk,
    RePrompt,
    TurnController,
)


# -- interpretation failures (Req 12.4, 12.5) ------------------------------


def test_first_interpretation_failure_re_asks() -> None:
    """Req 12.4: the first uninterpretable turn asks the patient to repeat."""
    tc = TurnController()

    action = tc.on_interpretation_failure()

    assert isinstance(action, ReAsk)
    assert action.attempt == 1
    assert tc.consecutive_interpretation_failures == 1
    assert not tc.escalated


def test_second_consecutive_failure_escalates() -> None:
    """Req 12.5: reaching the max of 2 consecutive failures escalates to a human."""
    tc = TurnController()

    tc.on_interpretation_failure()
    action = tc.on_interpretation_failure()

    assert isinstance(action, Escalate)
    assert action.reason == EscalationReason.OUTSIDE_ADMIN_RULES
    assert action.inform_human_follow_up is True
    assert tc.escalated


def test_re_ask_count_never_exceeds_the_bound() -> None:
    """Property 16 invariant: the number of re-ask prompts never exceeds two.

    Escalation fires exactly once, and further failures for the same request are
    inert until a successful turn resets the run.
    """
    tc = TurnController()

    actions = [tc.on_interpretation_failure() for _ in range(6)]

    re_asks = [a for a in actions if isinstance(a, ReAsk)]
    escalations = [a for a in actions if isinstance(a, Escalate)]
    assert len(re_asks) <= 2
    assert len(escalations) == 1
    # Everything after the single escalation is inert.
    assert all(isinstance(a, NoAction) for a in actions[2:])


def test_successful_turn_resets_failure_run() -> None:
    """The bound is per request: a successful turn resets the counter so the
    next failure re-asks again rather than escalating."""
    tc = TurnController()

    tc.on_interpretation_failure()  # ReAsk, count -> 1
    tc.on_interpretable_turn()  # reset

    assert tc.consecutive_interpretation_failures == 0
    action = tc.on_interpretation_failure()
    assert isinstance(action, ReAsk)
    assert action.attempt == 1


def test_successful_turn_after_escalation_allows_fresh_retries() -> None:
    """After escalation, a successful turn clears the escalation latch so a new
    request gets its own retry budget."""
    tc = TurnController()

    tc.on_interpretation_failure()
    tc.on_interpretation_failure()  # escalate
    assert tc.escalated

    tc.on_interpretable_turn()
    assert not tc.escalated
    assert isinstance(tc.on_interpretation_failure(), ReAsk)


# -- silence re-prompt (Req 12.6) ------------------------------------------


def test_silence_reprompts_once() -> None:
    """Req 12.6: 10 s of silence re-prompts the patient exactly once."""
    tc = TurnController()

    first = tc.on_silence_timeout()
    second = tc.on_silence_timeout()

    assert isinstance(first, RePrompt)
    assert isinstance(second, NoAction)
    assert tc.silence_reprompted


def test_silence_reprompt_latch_resets_after_interpretable_turn() -> None:
    """The single re-prompt is per silent stretch: speaking resets the latch."""
    tc = TurnController()

    assert isinstance(tc.on_silence_timeout(), RePrompt)
    tc.on_interpretable_turn()
    assert not tc.silence_reprompted
    assert isinstance(tc.on_silence_timeout(), RePrompt)


def test_silence_timeout_constant_is_ten_seconds() -> None:
    """Req 12.6: the silence threshold is 10 seconds."""
    assert TurnController.SILENCE_TIMEOUT_SECONDS == 10.0
    assert TurnController.MAX_INTERPRETATION_ATTEMPTS == 2


# -- voice-layer loss (Req 12.7) -------------------------------------------


def test_voice_layer_loss_ends_session_with_interrupted_outcome() -> None:
    """Req 12.7: voice-layer loss informs the patient, ends the session, and
    records the outcome as ``interrupted``."""
    tc = TurnController()

    action = tc.on_voice_layer_lost()

    assert isinstance(action, EndSession)
    assert action.outcome == CallOutcome.INTERRUPTED
    assert action.inform_patient is True
    assert tc.ended


def test_voice_layer_loss_is_idempotent() -> None:
    """The session ends once; a second loss event records no further outcome."""
    tc = TurnController()

    assert isinstance(tc.on_voice_layer_lost(), EndSession)
    assert isinstance(tc.on_voice_layer_lost(), NoAction)


def test_events_after_session_end_are_inert() -> None:
    """Once ended, interpretation-failure and silence events are inert (Req 12.7)."""
    tc = TurnController()
    tc.on_voice_layer_lost()

    assert isinstance(tc.on_interpretation_failure(), NoAction)
    assert isinstance(tc.on_silence_timeout(), NoAction)
    assert isinstance(tc.on_interpretable_turn(), NoAction)
