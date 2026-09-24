"""A call transcript must record both sides of the conversation.

The bug: every stored transcript held only the patient's turns. The doctor reads the
transcript to see what happened on a call, and it showed her half a conversation —
the questions, never the answers.

The cause was a case-sensitive string comparison in two places. Nova Sonic labels
roles in upper case (``"ASSISTANT"``, ``"USER"``), and both the stream adapter and the
recorder compared against the lower-case spelling. ``"ASSISTANT" == "assistant"`` is
false, so every agent turn fell through to the patient branch and was either
mislabelled or dropped.

Worth its own file because the failure was silent in the worst way: nothing errored,
the transcript existed and looked plausible, and the missing half only shows up if you
know what the agent said.
"""

from __future__ import annotations

import pytest

from clinic_front_desk.voice.recording import CallRecorder
from clinic_front_desk.voice.stream import NovaSonicVoiceStream


def _translate(role: str, text: str = "hello", final: bool = True) -> object | None:
    return NovaSonicVoiceStream._translate(
        {
            "type": "bidi_transcript_stream",
            "is_final": final,
            "role": role,
            "text": text,
        }
    )


# -- the stream adapter -----------------------------------------------------


@pytest.mark.parametrize("role", ["ASSISTANT", "assistant", "Assistant", " assistant "])
def test_an_assistant_turn_is_recognised_however_it_is_spelled(role: str) -> None:
    """``ASSISTANT`` is what Nova Sonic actually sends. That was the live case."""
    event = _translate(role)

    assert event is not None
    assert event.role == "assistant", f"{role!r} was not recognised as the agent"


@pytest.mark.parametrize("role", ["USER", "user", "User"])
def test_a_user_turn_stays_the_user(role: str) -> None:
    event = _translate(role)

    assert event is not None
    assert event.role == "user"


def test_an_unknown_role_falls_back_to_the_caller() -> None:
    """Safer than guessing it is the agent: a caller's words attributed to the
    clinic would put things in the record the clinic never said."""
    event = _translate("SYSTEM")

    assert event is not None
    assert event.role == "user"


def test_a_partial_caller_turn_is_ignored_but_a_partial_agent_turn_is_not() -> None:
    """Finality is required of the caller and not of the agent.

    Written the other way round first, on the assumption that partial means partial
    for everyone. The deployed model disagrees: it never marks its own output final,
    so applying the rule evenly is what deleted the agent's half of every transcript.
    """
    assert _translate("USER", final=False) is None
    assert _translate("ASSISTANT", final=False) is not None


# -- the recorder -----------------------------------------------------------


@pytest.mark.parametrize("role", ["ASSISTANT", "assistant", "agent", "AGENT"])
def test_the_recorder_attributes_the_agent_correctly(role: str) -> None:
    recorder = CallRecorder()

    recorder.add_turn(role, "I have Monday at nine.")

    assert [turn.role for turn in recorder.turns] == ["agent"], role


@pytest.mark.parametrize("role", ["USER", "user", "patient", "PATIENT"])
def test_the_recorder_attributes_the_caller_correctly(role: str) -> None:
    recorder = CallRecorder()

    recorder.add_turn(role, "I would like to book a hearing test.")

    assert [turn.role for turn in recorder.turns] == ["patient"], role


def test_a_rendered_transcript_carries_both_speakers() -> None:
    """The whole point: a reader can see the exchange, not just the questions."""
    recorder = CallRecorder()
    recorder.add_turn("USER", "How much does a consultation cost?")
    recorder.add_turn("ASSISTANT", "It is 500 rupees.")

    rendered = recorder.render_transcript()

    assert rendered is not None
    assert "patient: How much does a consultation cost?" in rendered
    assert "agent: It is 500 rupees." in rendered


def test_the_agent_is_not_silently_relabelled_as_the_patient() -> None:
    """The exact regression. Both lines used to come back as the patient."""
    recorder = CallRecorder()
    recorder.add_turn("USER", "question")
    recorder.add_turn("ASSISTANT", "answer")

    roles = [turn.role for turn in recorder.turns]

    assert roles == ["patient", "agent"], roles


# -- the assistant's turns arrive unfinalised, so they can repeat ------------


def test_an_unfinalised_assistant_turn_is_still_surfaced() -> None:
    """The actual cause, and the one a guess got wrong first.

    Observed on the deployed model: the caller's turn arrives ``is_final=True`` and
    the assistant's arrives ``is_final=False``, every time. Requiring finality of both
    dropped the entire agent side.
    """
    event = _translate("assistant", final=False)

    assert event is not None
    assert event.role == "assistant"


def test_an_unfinalised_caller_turn_is_still_ignored() -> None:
    """Partial recognition of the caller changes word by word as they speak."""
    assert _translate("user", final=False) is None


def test_a_repeated_agent_line_is_not_printed_twice() -> None:
    recorder = CallRecorder()
    recorder.add_turn("assistant", "I have Monday at nine.")
    recorder.add_turn("assistant", "I have Monday at nine.")

    assert [turn.text for turn in recorder.turns] == ["I have Monday at nine."]


def test_a_line_that_grew_replaces_the_fragment() -> None:
    """Unfinalised text can be delivered as it is produced."""
    recorder = CallRecorder()
    recorder.add_turn("assistant", "I have Monday")
    recorder.add_turn("assistant", "I have Monday at nine.")

    assert [turn.text for turn in recorder.turns] == ["I have Monday at nine."]


def test_a_shorter_repeat_does_not_truncate_the_line() -> None:
    recorder = CallRecorder()
    recorder.add_turn("assistant", "I have Monday at nine.")
    recorder.add_turn("assistant", "I have Monday")

    assert [turn.text for turn in recorder.turns] == ["I have Monday at nine."]


def test_two_genuinely_different_lines_are_both_kept() -> None:
    """Collapsing must not swallow a second thing actually said."""
    recorder = CallRecorder()
    recorder.add_turn("assistant", "I have Monday at nine.")
    recorder.add_turn("assistant", "Shall I book it?")

    assert len(recorder.turns) == 2


def test_the_same_words_from_different_speakers_are_both_kept() -> None:
    """Collapsing applies within one speaker, not across the two."""
    recorder = CallRecorder()
    recorder.add_turn("user", "Monday at nine")
    recorder.add_turn("assistant", "Monday at nine")

    assert [turn.role for turn in recorder.turns] == ["patient", "agent"]


def test_a_caller_repeating_themselves_is_not_collapsed() -> None:
    """Only the agent's turns repeat for technical reasons.

    The caller's arrive finalised, so "yes" then "yes" is them actually saying it
    twice — a fact about the call. Collapsing that would quietly edit the record.
    """
    recorder = CallRecorder()
    recorder.add_turn("user", "yes")
    recorder.add_turn("user", "yes")

    assert [turn.text for turn in recorder.turns] == ["yes", "yes"]
