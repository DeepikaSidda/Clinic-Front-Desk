"""Call recording and transcript capture (Req 11.5, 15.2).

Accumulates a Call_Session's audio and transcript while the call runs, then
renders them on end: the audio into a single WAV, the transcript into text stored
on the ``CallSession``.

Reconstructing the timeline
--------------------------
Audio only arrives while someone is speaking, so concatenating the chunks would
produce a recording shorter than the call with every pause removed — the two sides
would drift out of alignment and the result would be unusable for reviewing what
was actually said when. So each chunk is stamped with its offset from the start of
the call, and rendering lays chunks onto a silent timeline at those offsets.

The result is a **stereo** WAV at 24 kHz: the patient on the left channel, the
agent on the right. Separating the speakers means a reviewer can tell instantly
who said what, and overlaps (a barge-in) are audible as exactly that rather than
as garbled mono. The patient's 16 kHz input is upsampled ×1.5 to match Nova
Sonic's 24 kHz output.

Bounding memory
---------------
Raw PCM is roughly 2.9 MB per minute of stereo at 24 kHz, held in memory until the
call ends. :data:`MAX_RECORDING_SECONDS` caps how much is retained so an
abandoned-but-open connection cannot grow without limit; capture stops at the cap
and :attr:`CallRecorder.truncated` says so. The transcript is capped separately,
since it is stored in a DynamoDB item.
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass, field
from datetime import UTC, datetime

#: Nova Sonic's output rate, and the rendering rate for the whole recording.
OUTPUT_RATE = 24000

#: The rate patient audio arrives at, per the ``/ws`` audio contract.
INPUT_RATE = 16000

#: Hard cap on recorded audio per call. Beyond this, capture stops rather than
#: growing memory without bound.
MAX_RECORDING_SECONDS = 30 * 60

#: Hard cap on the stored transcript. A DynamoDB item is limited to 400 KB total,
#: and the transcript shares that item with the rest of the CallSession.
MAX_TRANSCRIPT_CHARS = 60_000

#: Marker appended when either cap truncates the record.
TRUNCATION_NOTICE = "\n[truncated: exceeded the per-call limit]"


def _pcm16_to_samples(pcm: bytes) -> list[int]:
    """Decode little-endian 16-bit PCM into signed ints."""
    count = len(pcm) // 2
    return list(
        int.from_bytes(pcm[i * 2 : i * 2 + 2], "little", signed=True)
        for i in range(count)
    )


def _upsample(samples: list[int], factor: float) -> list[int]:
    """Linearly resample ``samples`` by ``factor`` (1.5 for 16 kHz → 24 kHz)."""
    if factor == 1.0 or not samples:
        return samples
    out_len = int(len(samples) * factor)
    out: list[int] = []
    for i in range(out_len):
        position = i / factor
        low = int(position)
        frac = position - low
        first = samples[low]
        second = samples[low + 1] if low + 1 < len(samples) else first
        out.append(int(first + (second - first) * frac))
    return out


@dataclass
class _Chunk:
    """One captured audio chunk and where it belongs on the timeline."""

    offset_seconds: float
    samples: list[int]


@dataclass
class TranscriptTurn:
    """One finalized utterance in the call."""

    role: str
    text: str
    offset_seconds: float


@dataclass
class CallRecorder:
    """Captures one Call_Session's audio and transcript.

    Args:
        clock: Monotonic-ish time source in seconds. Defaults to a wall clock;
            inject a fake so rendering is deterministic in tests.
        max_seconds: Cap on recorded audio.
    """

    clock: object = None
    max_seconds: float = MAX_RECORDING_SECONDS
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    _t0: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        # Anchor the timeline to when the recorder was created — i.e. the start of
        # the call — not to the first chunk of audio. Anchoring on first audio makes
        # the recording shorter than the call (observed: 11.8 s of audio for a
        # 17.2 s call) and silently drops the opening silence, which is itself
        # information: it is how long the caller waited before anyone spoke.
        self._t0 = self._now()
    _patient: list[_Chunk] = field(default_factory=list, init=False)
    _agent: list[_Chunk] = field(default_factory=list, init=False)
    _turns: list[TranscriptTurn] = field(default_factory=list, init=False)
    truncated: bool = field(default=False, init=False)

    # -- timing -------------------------------------------------------------

    def _now(self) -> float:
        if callable(self.clock):
            return float(self.clock())
        import time

        return time.monotonic()

    def _offset(self) -> float:
        now = self._now()
        if self._t0 is None:
            self._t0 = now
        return max(0.0, now - self._t0)

    # -- capture ------------------------------------------------------------

    def add_patient_audio(self, pcm: bytes, *, sample_rate: int = INPUT_RATE) -> None:
        """Record a chunk of inbound patient audio."""
        self._add(self._patient, pcm, sample_rate)

    def add_agent_audio(self, pcm: bytes, *, sample_rate: int = OUTPUT_RATE) -> None:
        """Record a chunk of outbound agent audio."""
        self._add(self._agent, pcm, sample_rate)

    def _add(self, track: list[_Chunk], pcm: bytes, sample_rate: int) -> None:
        offset = self._offset()
        if offset > self.max_seconds:
            self.truncated = True
            return
        samples = _pcm16_to_samples(pcm)
        if sample_rate != OUTPUT_RATE:
            samples = _upsample(samples, OUTPUT_RATE / sample_rate)
        track.append(_Chunk(offset_seconds=offset, samples=samples))

    def add_turn(self, role: str, text: str) -> None:
        """Record a finalized transcript turn."""
        cleaned = (text or "").strip()
        if not cleaned:
            return
        self._turns.append(
            TranscriptTurn(
                # Case-insensitive, and "agent" accepted as well as "assistant".
                # An exact match against the lower-case spelling is what made every
                # stored transcript one-sided: Nova Sonic says "ASSISTANT", which fell
                # through to the patient branch, so the agent's own words were either
                # mislabelled or lost.
                role=(
                    "agent"
                    if str(role or "").strip().lower() in ("assistant", "agent")
                    else "patient"
                ),
                text=cleaned,
                offset_seconds=self._offset(),
            )
        )

    # -- state --------------------------------------------------------------

    @property
    def has_audio(self) -> bool:
        """Whether any audio was captured."""
        return bool(self._patient or self._agent)

    @property
    def turns(self) -> list[TranscriptTurn]:
        """The captured transcript turns, in order."""
        return list(self._turns)

    @property
    def duration_seconds(self) -> float:
        """Length of the rendered timeline."""
        end = 0.0
        for track in (self._patient, self._agent):
            for chunk in track:
                end = max(end, chunk.offset_seconds + len(chunk.samples) / OUTPUT_RATE)
        return end

    # -- rendering ----------------------------------------------------------

    def render_transcript(self) -> str | None:
        """Render the transcript as text, or ``None`` if nothing was said.

        Each line is ``[mm:ss] speaker: text`` so a reader can locate a moment in
        the recording without playing it end to end.
        """
        if not self._turns:
            return None
        lines = []
        for turn in self._turns:
            minutes, seconds = divmod(int(turn.offset_seconds), 60)
            lines.append(f"[{minutes:02d}:{seconds:02d}] {turn.role}: {turn.text}")
        text = "\n".join(lines)
        if len(text) > MAX_TRANSCRIPT_CHARS:
            keep = MAX_TRANSCRIPT_CHARS - len(TRUNCATION_NOTICE)
            text = text[:keep] + TRUNCATION_NOTICE
        return text

    def render_wav(self) -> bytes | None:
        """Render the call as a stereo 24 kHz WAV, or ``None`` if no audio.

        Patient audio goes to the left channel and agent audio to the right, each
        placed at its captured offset with silence in between, so the recording
        runs for the real duration of the call and the two sides stay aligned.
        """
        if not self.has_audio:
            return None

        total = int(self.duration_seconds * OUTPUT_RATE) + 1
        left = [0] * total
        right = [0] * total

        for track, channel in ((self._patient, left), (self._agent, right)):
            for chunk in track:
                start = int(chunk.offset_seconds * OUTPUT_RATE)
                for index, sample in enumerate(chunk.samples):
                    position = start + index
                    if position >= total:
                        break
                    # Sum rather than overwrite: chunks can overlap slightly, and
                    # clipping a sum is less surprising than dropping audio.
                    mixed = channel[position] + sample
                    channel[position] = max(-32768, min(32767, mixed))

        frames = bytearray()
        for index in range(total):
            frames += int(left[index]).to_bytes(2, "little", signed=True)
            frames += int(right[index]).to_bytes(2, "little", signed=True)

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(OUTPUT_RATE)
            handle.writeframes(bytes(frames))
        return buffer.getvalue()


__all__ = [
    "CallRecorder",
    "TranscriptTurn",
    "INPUT_RATE",
    "OUTPUT_RATE",
    "MAX_RECORDING_SECONDS",
    "MAX_TRANSCRIPT_CHARS",
    "TRUNCATION_NOTICE",
]
