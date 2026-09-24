"""Transcribe a finished call's recording, so a human-held conversation is written down.

Why this exists: while a person holds the call the model is fed silence, so it
transcribes nothing. That is deliberate — it is what stops the agent talking over the
doctor — but it leaves the written record blank for the part a human actually handled.
The audio has both sides on it, and for a clinic the text is what gets read later.

Why **after** the call rather than live:

* The official streaming SDK, ``amazon-transcribe``, pins ``awscrt~=0.26.1``. Nova
  Sonic's bidirectional stream runs on 0.36.2. Downgrading the transport the whole
  voice agent depends on, to add a transcript, is the wrong trade.
* Batch transcription needs only ``boto3``, which is already a dependency, and reads
  the stereo WAV that is already in S3.
* Nothing leaves AWS. The browser's own speech API would have been live and free, but
  it ships audio to a third party, which is not where a patient conversation belongs.

The recording is stereo **by design** — caller left, clinic right — so channel
identification labels who said what instead of guessing from voices.

Everything here is best-effort. A transcript that fails to arrive must never cost the
call record: the audio and the agent's own transcript are already persisted before
this runs.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Channel 0 is the caller, channel 1 is the clinic. Set by the recorder when it
#: renders the stereo WAV, and asserted in its own tests — not guessed here.
CALLER_CHANNEL = "ch_0"
CLINIC_CHANNEL = "ch_1"

#: What each channel is called in the written record. Matches the labels the agent's
#: own transcript already uses, so one call reads as one document.
CHANNEL_LABELS = {CALLER_CHANNEL: "patient", CLINIC_CHANNEL: "clinic"}

#: Marks the transcribed section, so nobody mistakes it for the live transcript.
#:
#: It is a different kind of evidence: recognised from audio after the fact, with no
#: guardrail having seen it and no model having acted on it. Saying so is the honest
#: thing, and it explains the timestamps restarting from the top of the recording.
TRANSCRIBED_HEADER = (
    "--- transcribed from the call recording (both sides, after the call) ---"
)

#: How long to wait for a job. A short clinic call transcribes in well under a minute;
#: beyond this something is wrong and waiting longer helps nobody.
DEFAULT_TIMEOUT_SECONDS = 300.0

#: Gap between job status checks.
DEFAULT_POLL_SECONDS = 5.0


def _timestamp(seconds: float) -> str:
    """``[mm:ss]``, matching the agent transcript's own line format."""
    total = int(seconds)
    return f"[{total // 60:02d}:{total % 60:02d}]"


@dataclass
class ConversationTranscriber:
    """Runs Amazon Transcribe over a finished call recording.

    Args:
        region: AWS region for Transcribe. Resolved by the caller so it cannot
            disagree with the rest of the deployment — an unresolved region was a
            real outage here once, in the Polly path.
        client: Optional pre-built Transcribe client, for tests.
        http_get: Optional fetcher for the result document, for tests.
    """

    region: str
    client: Any | None = None
    http_get: Any | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    poll_seconds: float = DEFAULT_POLL_SECONDS
    language_code: str = "en-IN"
    _jobs: list[str] = field(default_factory=list)

    def _transcribe(self) -> Any:
        if self.client is None:
            import boto3  # type: ignore[import-untyped]

            self.client = boto3.client("transcribe", region_name=self.region)
        return self.client

    def _fetch(self, url: str) -> bytes:
        if self.http_get is not None:
            result: bytes = self.http_get(url)
            return result
        with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
            body: bytes = response.read()
            return body

    # -- the one public entry point ----------------------------------------

    def transcribe(self, session_id: str, media_uri: str) -> str | None:
        """Transcribe ``media_uri`` and render it as labelled transcript lines.

        Blocking, and expected to be called off the event loop — it waits on a job
        that takes tens of seconds. Returns ``None`` on any failure, having logged
        it: a missing transcript is a gap in the record, while a raised exception
        here would be a gap in the *call* record, which is worse.
        """
        job = f"clinic-{session_id}-{int(time.time())}"
        try:
            self._start(job, media_uri)
        except Exception as exc:  # noqa: BLE001 - never surface to a call
            logger.warning("transcription could not start for %s: %s", session_id, exc)
            return None

        try:
            url = self._await_result(job)
        except Exception as exc:  # noqa: BLE001
            logger.warning("transcription failed for %s: %s", session_id, exc)
            return None
        if url is None:
            return None

        try:
            document = json.loads(self._fetch(url))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "transcription result unreadable for %s: %s", session_id, exc
            )
            return None

        return self.render(document)

    # -- steps, separated so each is testable ------------------------------

    def _start(self, job: str, media_uri: str) -> None:
        self._transcribe().start_transcription_job(
            TranscriptionJobName=job,
            Media={"MediaFileUri": media_uri},
            MediaFormat="wav",
            LanguageCode=self.language_code,
            # The whole reason the recording is stereo. Without this, two speakers
            # come back as one run of text and the record cannot say who spoke.
            Settings={"ChannelIdentification": True},
        )
        self._jobs.append(job)

    def _await_result(self, job: str) -> str | None:
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            described = self._transcribe().get_transcription_job(
                TranscriptionJobName=job
            )
            detail = described["TranscriptionJob"]
            status = detail["TranscriptionJobStatus"]
            if status == "COMPLETED":
                uri: str = detail["Transcript"]["TranscriptFileUri"]
                return uri
            if status == "FAILED":
                logger.warning(
                    "transcription job %s failed: %s",
                    job,
                    detail.get("FailureReason", "no reason given"),
                )
                return None
            time.sleep(self.poll_seconds)
        logger.warning("transcription job %s did not finish in time", job)
        return None

    def render(self, document: dict[str, Any]) -> str | None:
        """Turn a Transcribe result into ``[mm:ss] speaker: text`` lines.

        Ordered by start time across both channels, which is what makes it read as a
        conversation rather than as two monologues.
        """
        results = (document or {}).get("results") or {}
        labelled: list[tuple[float, str, str]] = []

        for channel in results.get("channel_labels", {}).get("channels", []):
            label = CHANNEL_LABELS.get(str(channel.get("channel_label")))
            if label is None:
                continue
            for item in channel.get("items", []):
                # Only finalised alternatives carry text worth keeping.
                alternatives = item.get("alternatives") or []
                if not alternatives:
                    continue
                if item.get("type") != "pronunciation":
                    continue
                start = float(item.get("start_time") or 0.0)
                labelled.append((start, label, str(alternatives[0].get("content", ""))))

        if not labelled:
            return None

        labelled.sort(key=lambda entry: (entry[0], entry[1]))

        # Group consecutive words by speaker into utterances, so the output is
        # sentences rather than one line per word.
        lines: list[str] = [TRANSCRIBED_HEADER]
        current_label = labelled[0][1]
        current_start = labelled[0][0]
        words: list[str] = []
        for start, label, word in labelled:
            if label != current_label:
                if words:
                    lines.append(
                        f"{_timestamp(current_start)} {current_label}: "
                        f"{' '.join(words)}"
                    )
                current_label = label
                current_start = start
                words = []
            words.append(word)
        if words:
            lines.append(
                f"{_timestamp(current_start)} {current_label}: {' '.join(words)}"
            )

        return "\n".join(lines) if len(lines) > 1 else None


def merge_transcript(existing: str | None, transcribed: str | None) -> str | None:
    """Append the transcribed conversation to the agent's own transcript.

    Appended rather than spliced by timestamp. Splicing would imply the two came from
    one source and were interleaved reliably; they did not. The header says where the
    recognised text begins, and the live transcript above it stays exactly as the
    guardrails and the agent recorded it.
    """
    if not transcribed:
        return existing
    if not existing:
        return transcribed
    if TRANSCRIBED_HEADER in existing:
        # Already merged — re-running must not stack duplicates onto the record.
        return existing
    return f"{existing}\n{transcribed}"


__all__ = [
    "CALLER_CHANNEL",
    "CHANNEL_LABELS",
    "CLINIC_CHANNEL",
    "DEFAULT_POLL_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "TRANSCRIBED_HEADER",
    "ConversationTranscriber",
    "merge_transcript",
]
