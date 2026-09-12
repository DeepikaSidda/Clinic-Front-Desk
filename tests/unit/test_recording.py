"""Unit tests for call recording capture and rendering.

The property that matters most here is **timeline fidelity**. Audio only arrives
while someone is speaking, so a recorder that concatenates chunks produces a file
shorter than the call with every pause removed and the two speakers drifting out
of sync — technically "a recording", practically useless for reviewing what was
said when. These tests pin the offsets, the silence between them, and the
channel separation.
"""

from __future__ import annotations

import io
import wave

import pytest

from clinic_front_desk.voice.recording import (
    INPUT_RATE,
    MAX_TRANSCRIPT_CHARS,
    OUTPUT_RATE,
    TRUNCATION_NOTICE,
    CallRecorder,
)


class FakeClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def tone(samples: int, value: int = 8000) -> bytes:
    """``samples`` frames of constant-amplitude 16-bit PCM."""
    return b"".join(int(value).to_bytes(2, "little", signed=True) for _ in range(samples))


def read_wav(data: bytes) -> tuple[int, int, int, bytes]:
    """Return ``(channels, sample_width, frame_rate, frames)`` from a WAV."""
    with wave.open(io.BytesIO(data), "rb") as handle:
        return (
            handle.getnchannels(),
            handle.getsampwidth(),
            handle.getframerate(),
            handle.readframes(handle.getnframes()),
        )


def channel_samples(frames: bytes, channel: int) -> list[int]:
    """Extract one channel from interleaved stereo 16-bit frames."""
    stride = 4  # 2 channels x 2 bytes
    offset = channel * 2
    return [
        int.from_bytes(frames[i + offset : i + offset + 2], "little", signed=True)
        for i in range(0, len(frames), stride)
    ]


# ---------------------------------------------------------------------------
# Nothing captured
# ---------------------------------------------------------------------------


def test_no_audio_renders_no_wav() -> None:
    recorder = CallRecorder(clock=FakeClock())

    assert recorder.has_audio is False
    assert recorder.render_wav() is None


def test_no_turns_renders_no_transcript() -> None:
    assert CallRecorder(clock=FakeClock()).render_transcript() is None


def test_blank_turns_are_ignored() -> None:
    recorder = CallRecorder(clock=FakeClock())

    recorder.add_turn("user", "   ")
    recorder.add_turn("user", "")

    assert recorder.render_transcript() is None


# ---------------------------------------------------------------------------
# WAV shape
# ---------------------------------------------------------------------------


def test_renders_stereo_24k_16bit() -> None:
    recorder = CallRecorder(clock=FakeClock())
    recorder.add_agent_audio(tone(OUTPUT_RATE // 10))

    channels, width, rate, _ = read_wav(recorder.render_wav() or b"")

    assert (channels, width, rate) == (2, 2, OUTPUT_RATE)


def test_patient_is_left_and_agent_is_right() -> None:
    """Channel separation is what lets a reviewer tell who spoke."""
    clock = FakeClock()
    recorder = CallRecorder(clock=clock)

    recorder.add_patient_audio(tone(INPUT_RATE // 10, value=5000))
    clock.advance(1.0)
    recorder.add_agent_audio(tone(OUTPUT_RATE // 10, value=9000))

    _, _, _, frames = read_wav(recorder.render_wav() or b"")
    left = channel_samples(frames, 0)
    right = channel_samples(frames, 1)

    # The patient's audio is at the start on the left, and the left channel is
    # silent where the agent speaks (and vice versa).
    assert max(left[: OUTPUT_RATE // 10]) > 0
    assert max(abs(s) for s in right[: OUTPUT_RATE // 10]) == 0
    agent_window = slice(OUTPUT_RATE, OUTPUT_RATE + OUTPUT_RATE // 10)
    assert max(right[agent_window]) > 0
    assert max(abs(s) for s in left[agent_window]) == 0


def test_patient_audio_is_upsampled_to_the_output_rate() -> None:
    """16 kHz in, 24 kHz out: 0.5 s of speech must still last 0.5 s."""
    recorder = CallRecorder(clock=FakeClock())
    recorder.add_patient_audio(tone(INPUT_RATE // 2))

    _, _, _, frames = read_wav(recorder.render_wav() or b"")
    left = channel_samples(frames, 0)
    voiced = [index for index, sample in enumerate(left) if sample != 0]

    # ~0.5 s at 24 kHz = ~12000 frames, not the 8000 raw input samples.
    assert len(voiced) == pytest.approx(OUTPUT_RATE // 2, rel=0.02)


# ---------------------------------------------------------------------------
# Timeline fidelity — the point of the whole design
# ---------------------------------------------------------------------------


def test_silence_between_turns_is_preserved() -> None:
    """A 5 s gap must appear as 5 s of silence, not be squeezed out."""
    clock = FakeClock()
    recorder = CallRecorder(clock=clock)

    recorder.add_patient_audio(tone(INPUT_RATE // 10))  # 0.1 s at t=0
    clock.advance(5.0)
    recorder.add_agent_audio(tone(OUTPUT_RATE // 10))  # 0.1 s at t=5

    assert recorder.duration_seconds == pytest.approx(5.1, abs=0.05)
    _, _, _, frames = read_wav(recorder.render_wav() or b"")
    total_frames = len(frames) // 4
    assert total_frames == pytest.approx(5.1 * OUTPUT_RATE, rel=0.02)

    # The middle of the gap is silent on both channels.
    left = channel_samples(frames, 0)
    right = channel_samples(frames, 1)
    midpoint = int(2.5 * OUTPUT_RATE)
    assert left[midpoint] == 0
    assert right[midpoint] == 0


def test_chunks_land_at_their_captured_offsets() -> None:
    """The timeline is anchored at call start, so opening silence is preserved."""
    clock = FakeClock()
    recorder = CallRecorder(clock=clock)

    clock.advance(2.0)
    recorder.add_agent_audio(tone(OUTPUT_RATE // 20))

    _, _, _, frames = read_wav(recorder.render_wav() or b"")
    right = channel_samples(frames, 1)
    first_voiced = next(i for i, sample in enumerate(right) if sample != 0)

    # Two seconds of silence before anyone spoke, not a recording that jumps
    # straight to the first word.
    assert first_voiced == pytest.approx(2 * OUTPUT_RATE, rel=0.01)
    assert recorder.duration_seconds == pytest.approx(2.05, abs=0.05)


def test_overlapping_audio_on_one_channel_is_summed_and_clipped() -> None:
    """A barge-in can overlap chunks; summing must not wrap around."""
    clock = FakeClock()
    recorder = CallRecorder(clock=clock)

    recorder.add_agent_audio(tone(100, value=30000))
    recorder.add_agent_audio(tone(100, value=30000))  # same offset

    _, _, _, frames = read_wav(recorder.render_wav() or b"")
    right = channel_samples(frames, 1)

    assert max(right) == 32767  # clipped, not overflowed to a negative


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------


def test_transcript_is_timestamped_and_labels_the_speaker() -> None:
    clock = FakeClock()
    recorder = CallRecorder(clock=clock)

    recorder.add_turn("user", "what are your hours")
    clock.advance(64.0)
    recorder.add_turn("assistant", "nine to five, Monday to Friday")

    transcript = recorder.render_transcript() or ""

    assert transcript.splitlines() == [
        "[00:00] patient: what are your hours",
        "[01:04] agent: nine to five, Monday to Friday",
    ]


def test_transcript_is_capped_for_the_dynamodb_item() -> None:
    """The transcript shares a 400 KB DynamoDB item with the rest of the session."""
    recorder = CallRecorder(clock=FakeClock())
    for _ in range(4000):
        recorder.add_turn("user", "x" * 100)

    transcript = recorder.render_transcript() or ""

    assert len(transcript) <= MAX_TRANSCRIPT_CHARS
    assert transcript.endswith(TRUNCATION_NOTICE)


def test_turns_are_exposed_for_inspection() -> None:
    recorder = CallRecorder(clock=FakeClock())
    recorder.add_turn("user", "hello")

    assert [(t.role, t.text) for t in recorder.turns] == [("patient", "hello")]


# ---------------------------------------------------------------------------
# Memory bound
# ---------------------------------------------------------------------------


def test_capture_stops_at_the_duration_cap() -> None:
    """An abandoned-but-open connection must not grow memory without limit."""
    clock = FakeClock()
    recorder = CallRecorder(clock=clock, max_seconds=10.0)

    recorder.add_agent_audio(tone(OUTPUT_RATE // 10))
    clock.advance(11.0)
    recorder.add_agent_audio(tone(OUTPUT_RATE // 10))

    assert recorder.truncated is True
    assert recorder.duration_seconds == pytest.approx(0.1, abs=0.05)
