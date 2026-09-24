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


def test_partial_transcripts_are_still_ignored() -> None:
    """Only finalised text belongs in a record."""
    assert _translate("ASSISTANT", final=False) is None


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
