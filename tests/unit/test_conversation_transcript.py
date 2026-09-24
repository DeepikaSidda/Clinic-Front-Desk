"""Transcribing a human-held call, against a fake Transcribe.

While a person holds the call the model is fed silence, so it transcribes nothing and
that stretch is blank in the written record. The audio has both sides on it — the
recording is stereo by design, caller left and clinic right — so a batch job with
channel identification can say who said what after the fact.

Everything here runs with no AWS and no network. The interesting cases are the
failures: a transcript that does not arrive is a gap in the record, while an exception
escaping this path would damage the *call* record, which is already persisted by the
time any of it runs.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from clinic_front_desk.voice.conversation_transcript import (
    TRANSCRIBED_HEADER,
    ConversationTranscriber,
    merge_transcript,
)


def _word(channel_items: list[dict[str, Any]], start: float, text: str) -> None:
    channel_items.append(
        {
            "type": "pronunciation",
            "start_time": str(start),
            "end_time": str(start + 0.4),
            "alternatives": [{"content": text, "confidence": "0.99"}],
        }
    )


def _document() -> dict[str, Any]:
    """A Transcribe result shaped like the real one, two channels interleaved."""
    caller: list[dict[str, Any]] = []
    clinic: list[dict[str, Any]] = []
    _word(caller, 1.0, "my")
    _word(caller, 1.4, "ear")
    _word(caller, 1.8, "hurts")
    _word(clinic, 3.0, "how")
    _word(clinic, 3.4, "long")
    _word(clinic, 3.8, "has")
    _word(clinic, 4.2, "that")
    _word(clinic, 4.6, "been")
    _word(caller, 64.0, "three")
    _word(caller, 64.4, "days")
    return {
        "results": {
            "channel_labels": {
                "channels": [
                    {"channel_label": "ch_0", "items": caller},
                    {"channel_label": "ch_1", "items": clinic},
                ]
            }
        }
    }


class _FakeTranscribe:
    """Stands in for the boto3 Transcribe client."""

    def __init__(self, *, statuses: list[str] | None = None) -> None:
        self.started: list[dict[str, Any]] = []
        self.statuses = statuses or ["COMPLETED"]
        self.failure_reason = "unsupported media format"

    def start_transcription_job(self, **kwargs: Any) -> dict[str, Any]:
        self.started.append(kwargs)
        return {}

    def get_transcription_job(self, TranscriptionJobName: str) -> dict[str, Any]:  # noqa: N803
        status = self.statuses.pop(0) if self.statuses else "COMPLETED"
        job: dict[str, Any] = {"TranscriptionJobStatus": status}
        if status == "COMPLETED":
            job["Transcript"] = {"TranscriptFileUri": "https://example.invalid/t.json"}
        if status == "FAILED":
            job["FailureReason"] = self.failure_reason
        return {"TranscriptionJob": job}


def _transcriber(
    client: Any, document: dict[str, Any] | None = None
) -> ConversationTranscriber:
    payload = json.dumps(document if document is not None else _document()).encode()
    return ConversationTranscriber(
        region="us-east-1",
        client=client,
        http_get=lambda _url: payload,
        poll_seconds=0.0,
        timeout_seconds=5.0,
    )


def test_both_speakers_are_labelled_and_ordered() -> None:
    """The point of the whole thing: who said what, in the order they said it."""
    fake = _FakeTranscribe()

    text = _transcriber(fake).transcribe("call-1", "s3://bucket/call-1.wav")

    assert text is not None
    lines = text.splitlines()
    assert lines[0] == TRANSCRIBED_HEADER
    assert lines[1] == "[00:01] patient: my ear hurts"
    assert lines[2] == "[00:03] clinic: how long has that been"
    # Grouped into utterances rather than one line per word, and timestamped so a
    # reader can find the moment in the audio.
    assert lines[3] == "[01:04] patient: three days"


def test_channel_identification_is_requested() -> None:
    """Without it two speakers come back as one run of text and the record is mute."""
    fake = _FakeTranscribe()

    _transcriber(fake).transcribe("call-1", "s3://bucket/call-1.wav")

    assert fake.started, "no job was started"
    settings = fake.started[0]["Settings"]
    assert settings["ChannelIdentification"] is True
    assert fake.started[0]["Media"]["MediaFileUri"] == "s3://bucket/call-1.wav"


def test_it_waits_for_a_job_that_is_still_running() -> None:
    fake = _FakeTranscribe(statuses=["IN_PROGRESS", "IN_PROGRESS", "COMPLETED"])

    text = _transcriber(fake).transcribe("call-1", "s3://bucket/call-1.wav")

    assert text is not None


def test_a_failed_job_returns_nothing_rather_than_raising() -> None:
    """A missing transcript is a gap. A raised exception would be a lost call record."""
    fake = _FakeTranscribe(statuses=["FAILED"])

    assert _transcriber(fake).transcribe("call-1", "s3://bucket/call-1.wav") is None


def test_a_job_that_never_finishes_gives_up() -> None:
    """Bounded, so a stuck job cannot hold a background task open indefinitely."""
    fake = _FakeTranscribe(statuses=["IN_PROGRESS"] * 50)
    transcriber = ConversationTranscriber(
        region="us-east-1",
        client=fake,
        http_get=lambda _url: b"{}",
        poll_seconds=0.0,
        timeout_seconds=0.05,
    )

    assert transcriber.transcribe("call-1", "s3://bucket/call-1.wav") is None


def test_a_client_that_explodes_is_swallowed() -> None:
    class _Broken:
        def start_transcription_job(self, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("no such bucket")

    assert _transcriber(_Broken()).transcribe("c", "s3://b/c.wav") is None


def test_an_unreadable_result_document_is_swallowed() -> None:
    fake = _FakeTranscribe()
    transcriber = ConversationTranscriber(
        region="us-east-1",
        client=fake,
        http_get=lambda _url: b"not json at all",
        poll_seconds=0.0,
    )

    assert transcriber.transcribe("c", "s3://b/c.wav") is None


def test_silence_produces_nothing_rather_than_an_empty_header() -> None:
    """A header with no conversation under it is worse than no header."""
    empty = {"results": {"channel_labels": {"channels": []}}}

    assert _transcriber(_FakeTranscribe(), empty).transcribe("c", "s3://b/c.wav") is None


def test_unknown_channels_are_ignored() -> None:
    """Only the two channels the recorder writes are labelled; a third is not guessed."""
    document = {
        "results": {
            "channel_labels": {"channels": [{"channel_label": "ch_7", "items": []}]}
        }
    }

    assert (
        _transcriber(_FakeTranscribe(), document).transcribe("c", "s3://b/c.wav") is None
    )


# -- merging into the existing record ---------------------------------------


def test_merge_appends_below_the_live_transcript() -> None:
    """The live transcript stays exactly as the guardrails recorded it."""
    merged = merge_transcript("[00:01] patient: hello", f"{TRANSCRIBED_HEADER}\nx: y")

    assert merged is not None
    assert merged.startswith("[00:01] patient: hello")
    assert TRANSCRIBED_HEADER in merged


def test_merge_is_idempotent() -> None:
    """Re-running must not stack duplicate transcriptions onto a call record."""
    already = f"[00:01] patient: hello\n{TRANSCRIBED_HEADER}\nx: y"

    assert merge_transcript(already, f"{TRANSCRIBED_HEADER}\nz: w") == already


def test_merge_handles_a_call_with_no_live_transcript() -> None:
    """A call taken over immediately has nothing above the transcribed section."""
    assert merge_transcript(None, "something") == "something"


def test_merge_keeps_the_original_when_transcription_produced_nothing() -> None:
    assert merge_transcript("[00:01] patient: hello", None) == "[00:01] patient: hello"


@pytest.mark.parametrize("empty", ["", None])
def test_merge_with_nothing_on_either_side(empty: Any) -> None:
    assert merge_transcript(empty, empty) in (None, "")
