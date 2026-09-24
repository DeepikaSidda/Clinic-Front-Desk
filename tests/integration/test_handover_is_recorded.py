"""A call a human took over is recorded with both sides on it.

The gap this closes: the doctor's voice and her typed-then-synthesised lines go
straight out to the caller from the handover service, never through the model's audio
output handler — which is the only place recording happened. So the recording of a
call she answered held the caller's side and **silence** where the clinic spoke.

Worse, the model's suppressed audio *was* still being recorded. The saved WAV
contained agent speech the caller never heard, on the clinic's channel, at the moment
the doctor was actually talking. A recording that invents one side of a medical
conversation is worse than one with a gap in it: the gap is obvious, the invention is
not.

Both channels matter here because the recording is stereo — caller left, clinic right —
laid out on the real timeline, so someone reviewing a complaint can hear who said what.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import pytest

from clinic_front_desk.handover.live import LiveCallRegistry, LiveHandoverService

pytestmark = pytest.mark.integration


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


SESSION = "call-1"


class _Recorder:
    """Stands in for CallRecorder, capturing what each channel was given."""

    def __init__(self) -> None:
        self.agent: list[tuple[bytes, int]] = []

    def add_agent_audio(self, pcm: bytes, *, sample_rate: int) -> None:
        self.agent.append((pcm, sample_rate))


def _wired() -> tuple[LiveHandoverService, LiveCallRegistry, _Recorder]:
    registry = LiveCallRegistry()
    recorder = _Recorder()

    async def send(_message: dict[str, Any]) -> None:
        return None

    call = registry.register(SESSION, send)
    call.record_agent_audio = lambda pcm, rate: recorder.add_agent_audio(
        pcm, sample_rate=rate
    )
    service = LiveHandoverService(registry)
    return service, registry, recorder


def test_the_doctors_own_voice_reaches_the_recording() -> None:
    """Her microphone, relayed to the caller, must also be captured."""
    service, _registry, recorder = _wired()
    spoken = bytes(range(256))

    relayed = _run(
        service.relay_doctor_audio(
            SESSION, base64.b64encode(spoken).decode("ascii"), sample_rate=16_000
        )
    )

    assert relayed is True
    assert recorder.agent, "the doctor's voice never reached the recording"
    captured, rate = recorder.agent[0]
    # Byte-exact: a recording of roughly-what-she-said is not a recording.
    assert captured == spoken
    assert rate == 16_000


def test_a_typed_line_reaches_the_recording_too(monkeypatch: Any) -> None:
    """The caller heard it, so it belongs on the recording.

    Covers the nobody-picked-up apology as well, which runs through the same path.
    """
    service, _registry, recorder = _wired()
    synthesised = bytes(1024)
    monkeypatch.setattr(service, "synthesize", lambda _text: synthesised)

    said = _run(service.say(SESSION, "Hello, this is the clinic."))

    assert said is True
    assert recorder.agent, "the typed handover never reached the recording"
    captured, _rate = recorder.agent[0]
    assert captured == synthesised


def test_recording_is_skipped_when_no_bucket_is_configured() -> None:
    """Recording is opt-in, so an unset hook must not break the handover."""
    registry = LiveCallRegistry()

    async def send(_message: dict[str, Any]) -> None:
        return None

    registry.register(SESSION, send)  # record_agent_audio left unset
    service = LiveHandoverService(registry)

    relayed = _run(
        service.relay_doctor_audio(
            SESSION, base64.b64encode(b"\x00\x01").decode("ascii")
        )
    )

    assert relayed is True, "the call must carry on without a recorder"


def test_a_failing_recorder_never_drops_the_doctors_voice() -> None:
    """Losing a recording must not cost the caller the conversation.

    Same rule the rest of the system follows: the call is the thing that matters, the
    recording is bookkeeping.
    """
    service, registry, _recorder = _wired()
    call = registry.get(SESSION)
    assert call is not None

    def explode(_pcm: bytes, _rate: int) -> None:
        raise RuntimeError("disk on fire")

    call.record_agent_audio = explode

    relayed = _run(
        service.relay_doctor_audio(
            SESSION, base64.b64encode(b"\x00\x01").decode("ascii")
        )
    )

    assert relayed is True
