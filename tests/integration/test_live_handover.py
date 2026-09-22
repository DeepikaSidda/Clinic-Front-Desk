"""A live takeover: the caller stays on the line and hears a person.

The properties worth pinning are about the caller's experience, not the plumbing:

*   they are told a human joined, rather than the voice silently changing;
*   the human's words reach them as playable audio on the channel already open;
*   the frames are labelled with Polly's real sample rate, because the browser
    resamples from the label and a wrong one plays at the wrong pitch;
*   a doctor joining late can read what they missed;
*   synthesis failing does not drop the call.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from clinic_front_desk.handover.live import (
    FRAME_SAMPLES,
    POLLY_SAMPLE_RATE,
    LiveCallRegistry,
    LiveHandoverService,
)

SESSION = "call-live-1"


def _run(coro: Any) -> Any:
    """Drive a coroutine to completion (no pytest-asyncio in this project)."""
    return asyncio.run(coro)


class Recorder:
    """Stands in for one caller's WebSocket."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [m for m in self.sent if m.get("message_type") == kind]


class FakePolly:
    def __init__(self, seconds: float = 0.2, raises: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raises = raises
        self._bytes = int(POLLY_SAMPLE_RATE * 2 * seconds)

    def synthesize_speech(self, **kwargs: Any) -> dict[str, Any]:
        if self.raises is not None:
            raise self.raises
        self.calls.append(kwargs)

        class Stream:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data

        return {"AudioStream": Stream(b"\x01\x00" * (self._bytes // 2))}


def _service(polly: Any | None = None) -> tuple[LiveHandoverService, LiveCallRegistry, Recorder]:
    registry = LiveCallRegistry()
    recorder = Recorder()
    registry.register(SESSION, recorder)
    return LiveHandoverService(registry, polly=polly or FakePolly()), registry, recorder


# -- the registry -----------------------------------------------------------


def test_a_call_is_visible_while_it_is_open() -> None:
    _, registry, _ = _service()
    assert [c["session_id"] for c in registry.list_calls()] == [SESSION]


def test_a_closed_call_disappears() -> None:
    _, registry, _ = _service()
    registry.unregister(SESSION)
    assert registry.list_calls() == []


def test_calls_waiting_for_a_human_sort_first() -> None:
    """The doctor is looking for the one that needs them, not a list of everything."""
    _, registry, _ = _service()
    registry.register("call-quiet", Recorder())
    registry.mark_needs_human(SESSION, "asked for a person")

    assert registry.list_calls()[0]["session_id"] == SESSION


def test_the_reason_and_identity_reach_the_console() -> None:
    _, registry, _ = _service()
    registry.mark_needs_human(SESSION, "asked about symptoms")
    registry.identify(SESSION, name="Sailaja Devi", phone="9900012307")

    summary = registry.list_calls()[0]
    assert summary["reason"] == "asked about symptoms"
    assert summary["patient_name"] == "Sailaja Devi"
    assert summary["callback_phone"] == "9900012307"


def test_a_late_joining_doctor_can_read_what_they_missed() -> None:
    _, registry, _ = _service()
    registry.record_turn(SESSION, "patient", "my ear hurts")
    registry.record_turn(SESSION, "agent", "I can't advise on that")

    assert [t["text"] for t in registry.transcript_of(SESSION)] == [
        "my ear hurts",
        "I can't advise on that",
    ]


# -- taking over ------------------------------------------------------------


def test_the_caller_is_told_a_human_joined() -> None:
    """Silence then a different voice, with no explanation, is disorienting."""
    service, registry, recorder = _service()

    assert _run(service.take_over(SESSION)) is True

    joined = recorder.of_type("human_joined")
    assert joined and "joined the call" in joined[0]["text"]
    assert registry.get(SESSION).taken_over is True  # type: ignore[union-attr]


def test_taking_over_an_unknown_call_fails_quietly() -> None:
    service, _, _ = _service()
    assert _run(service.take_over("no-such-call")) is False


def test_the_call_can_be_handed_back_to_the_agent() -> None:
    service, registry, recorder = _service()
    _run(service.take_over(SESSION))

    assert _run(service.release(SESSION)) is True
    assert registry.get(SESSION).taken_over is False  # type: ignore[union-attr]
    assert recorder.of_type("human_left")


# -- the human speaking -----------------------------------------------------


def test_the_humans_words_arrive_as_playable_audio() -> None:
    service, _, recorder = _service()

    assert _run(service.say(SESSION, "Hello, this is the clinic.")) is True

    audio = recorder.of_type("agent_audio")
    assert audio, "the caller must actually hear something"
    assert all(base64.b64decode(frame["audio"]) for frame in audio)


def test_frames_carry_pollys_real_sample_rate() -> None:
    """Mislabelling the rate plays the human back at the wrong pitch."""
    service, _, recorder = _service()
    _run(service.say(SESSION, "Hello"))
    for frame in recorder.of_type("agent_audio"):
        assert frame["sample_rate"] == POLLY_SAMPLE_RATE
        assert frame["format"] == "pcm"
        assert frame["channels"] == 1


def test_audio_is_framed_not_sent_as_one_blob() -> None:
    """The client plays frames as they land; one large buffer arrives late."""
    service, _, recorder = _service()
    _run(service.say(SESSION, "a longer sentence for the caller"))
    frames = recorder.of_type("agent_audio")
    assert len(frames) > 1
    assert all(
        len(base64.b64decode(f["audio"])) <= FRAME_SAMPLES * 2 for f in frames
    )


def test_the_doctors_line_appears_in_the_transcript() -> None:
    service, registry, recorder = _service()
    _run(service.say(SESSION, "I can help with that"))
    assert recorder.of_type("transcript")[0]["role"] == "human"
    assert registry.transcript_of(SESSION)[-1]["text"] == "I can help with that"


def test_a_synthesis_failure_does_not_drop_the_call() -> None:
    """Polly failing must cost the sentence, not the call."""
    service, _, recorder = _service(FakePolly(raises=RuntimeError("polly down")))

    assert _run(service.say(SESSION, "Hello")) is False
    # The doctor still sees their line, and the socket is untouched otherwise.
    assert recorder.of_type("transcript")
    assert recorder.of_type("agent_audio") == []


def test_an_indian_english_neural_voice_is_requested() -> None:
    """A human stepping in should not sound like a different clinic."""
    polly = FakePolly()
    service, _, _ = _service(polly)
    _run(service.say(SESSION, "Hello"))
    assert polly.calls[0]["VoiceId"] == "Kajal"
    assert polly.calls[0]["Engine"] == "neural"
    assert polly.calls[0]["SampleRate"] == str(POLLY_SAMPLE_RATE)


# -- what the doctor reads at a glance -------------------------------------
#
# From a real call: the console showed the escalation reason as "patient_request".
# Correct as an API value, and wrong on a screen someone reads in a second or two
# while deciding whether to pick up a call that is already ringing.


def test_the_reason_is_shown_in_words() -> None:
    _, registry, _ = _service()
    registry.mark_needs_human(SESSION, "patient_request")

    summary = registry.list_calls()[0]
    assert summary["reason_label"] == "Asked to speak to a person"
    # The raw value stays, because API consumers should not parse prose.
    assert summary["reason"] == "patient_request"


def test_every_escalation_reason_has_a_label() -> None:
    """A new reason must not surface as a raw enum on the doctor's screen."""
    from clinic_front_desk.handover.live import REASON_LABELS
    from clinic_front_desk.models import EscalationReason

    for reason in EscalationReason:
        assert str(reason) in REASON_LABELS, reason


def test_an_unknown_reason_falls_back_to_itself() -> None:
    """Better a raw string than a blank space where the reason should be."""
    _, registry, _ = _service()
    registry.mark_needs_human(SESSION, "something_new")

    assert registry.list_calls()[0]["reason_label"] == "something_new"
