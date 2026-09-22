"""The agent may not promise a handover it cannot see happen.

From a live call:

    caller  hello how long i need to wait
    agent   I understand you'd like to speak with a human agent.
    agent   Let me flag this request for escalation so the next available agent
            can take your call.
    agent   Please stay on the line — you'll be connected as soon as someone is free.

Three things wrong, and none of them the model's fault. The prompt said to "tell the
patient a human will follow up", so it did. Nothing guarantees anyone is watching the
console, so "you'll be connected as soon as someone is free" is invented. "Flag this
request for escalation" is internal vocabulary. And the caller asked how long they
would wait and never got an answer.

These tests pin the prompt rules that replaced it. Prompt assertions are weak
evidence about behaviour — the model can still stray — but they stop the *instruction*
being silently dropped again, which is what happened here.
"""

from __future__ import annotations

import pytest

from clinic_front_desk.voice import ADMINISTRATIVE_ONLY_SYSTEM_PROMPT

PROMPT = ADMINISTRATIVE_ONLY_SYSTEM_PROMPT.lower()


def test_the_prompt_no_longer_instructs_a_blanket_promise() -> None:
    """The exact instruction that produced the bad turn is gone."""
    assert "tell the patient a human will follow up" not in PROMPT


def test_the_agent_is_told_to_say_only_what_the_tool_reports() -> None:
    assert "say_to_caller" in PROMPT
    assert "handover_delivered" in PROMPT


def test_a_failed_handover_may_not_be_dressed_up() -> None:
    assert "nothing reached a person" in PROMPT
    assert "do not say they are being" in PROMPT


@pytest.mark.parametrize(
    "phrase",
    [
        "as soon as someone is free",
        "you're next",
        "it won't be long",
    ],
)
def test_the_invented_reassurances_are_named_and_forbidden(phrase: str) -> None:
    """Naming the exact phrases is what makes the rule enforceable."""
    assert phrase in PROMPT


def test_inventing_a_wait_time_is_forbidden() -> None:
    assert "never invent a wait" in PROMPT
    assert "cannot see a queue" in PROMPT


def test_internal_vocabulary_is_forbidden_out_loud() -> None:
    """A caller should never hear "escalation", "flag" or "ticket"."""
    assert 'never say "escalate"' in PROMPT
    assert "internal words" in PROMPT


def test_the_agent_is_told_to_answer_the_question_asked() -> None:
    """"How long do I have to wait" is a question, not a cue for a status update."""
    assert "answer the question they actually asked" in PROMPT
    assert "cannot say how long" in PROMPT


def test_an_alternative_is_always_offered() -> None:
    """Refusing to promise is only half an answer; give them a real route."""
    assert "take a message" in PROMPT
    assert "clinic's number" in PROMPT


def test_the_reassurance_is_not_repeated_in_a_loop() -> None:
    assert "do not keep reassuring" in PROMPT
