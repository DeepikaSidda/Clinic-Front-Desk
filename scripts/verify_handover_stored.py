"""Prove a patient + human-agent conversation is stored in AWS, with both voices on it.

Runs against the **deployed** URL and then reads the object back out of S3, because
"the code calls the recorder" is not the same claim as "the conversation is in AWS".

What it does, end to end:

1. places a real call on the public WebSocket;
2. takes it over as the doctor through the token-gated talk socket;
3. the doctor speaks, and the caller speaks, with deliberately loud distinct tones;
4. hangs up, waits for the upload;
5. downloads the stereo WAV from S3 and measures **each channel separately**.

Measured as per-channel energy rather than byte equality on purpose: the recorder
lays both sides on a common timeline and resamples to the output rate, so the bytes
are legitimately not the ones that were sent. What must be true is that the clinic's
channel is not silent — which is exactly what was broken, because the doctor's audio
bypassed the only place recording happened.

    python scripts/verify_handover_stored.py
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import io
import json
import time
import wave
from pathlib import Path
from typing import Any

import websockets

HOST = "d21u7cmj563imv.cloudfront.net"
CALLER_WS = f"wss://{HOST}/ws"
TALK_WS = f"wss://{HOST}/dashboard/live/{{session_id}}/talk?role=doctor&k={{token}}"
BUCKET = "clinic-recordings-414691912352"
PREFIX = "call-recordings"

#: Loud square waves, one per speaker, so per-channel energy is unmistakable.
def tone(amplitude: int, period: int, seconds: float = 1.5, rate: int = 16_000) -> bytes:
    samples = int(rate * seconds)
    out = bytearray()
    for index in range(samples):
        value = amplitude if (index // period) % 2 == 0 else -amplitude
        out += int(value).to_bytes(2, "little", signed=True)
    return bytes(out)


DOCTOR_TONE = tone(amplitude=18_000, period=10)
CALLER_TONE = tone(amplitude=18_000, period=25)


def token() -> str:
    return Path(".secrets/console_token.txt").read_text(encoding="utf-8").strip()


def frame(kind: str, pcm: bytes, rate: int = 16_000) -> str:
    return json.dumps(
        {
            "message_type": kind,
            "audio": base64.b64encode(pcm).decode("ascii"),
            "format": "pcm",
            "sample_rate": rate,
            "channels": 1,
        }
    )


async def stream(socket: Any, kind: str, pcm: bytes, chunk: int = 1024) -> None:
    for offset in range(0, len(pcm), chunk):
        await socket.send(frame(kind, pcm[offset : offset + chunk]))
        await asyncio.sleep(chunk / 2 / 16_000)


def rms(samples: bytes) -> float:
    """Root-mean-square of 16-bit little-endian PCM, as a fraction of full scale."""
    count = len(samples) // 2
    if not count:
        return 0.0
    total = 0
    for index in range(count):
        value = int.from_bytes(
            samples[index * 2 : index * 2 + 2], "little", signed=True
        )
        total += value * value
    return (total / count) ** 0.5 / 32768.0


def split_channels(wav_bytes: bytes) -> tuple[int, int, bytes, bytes]:
    with contextlib.closing(wave.open(io.BytesIO(wav_bytes), "rb")) as handle:
        channels = handle.getnchannels()
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    if channels != 2:
        return channels, rate, raw, b""
    left = bytearray()
    right = bytearray()
    for index in range(0, len(raw) - 3, 4):
        left += raw[index : index + 2]
        right += raw[index + 2 : index + 4]
    return channels, rate, bytes(left), bytes(right)


async def place_call() -> str | None:
    """Call, take it over as the doctor, both speak, hang up. Returns the session id."""
    secret = token()
    async with websockets.connect(CALLER_WS, open_timeout=40, max_size=None) as caller:
        heard: list[dict[str, Any]] = []

        async def drain() -> None:
            with contextlib.suppress(Exception):
                async for raw in caller:
                    with contextlib.suppress(TypeError, ValueError):
                        heard.append(json.loads(raw))

        reader = asyncio.create_task(drain())
        for _ in range(160):
            if any(m.get("message_type") == "session_started" for m in heard):
                break
            await asyncio.sleep(0.25)
        session_id = next(
            (
                str(m.get("session_id"))
                for m in heard
                if m.get("message_type") == "session_started"
            ),
            None,
        )
        if not session_id:
            print("  FAIL  the call never started")
            reader.cancel()
            return None
        print(f"  ok    call placed: {session_id}")

        talk = TALK_WS.format(session_id=session_id, token=secret)
        doctor = await websockets.connect(talk, open_timeout=40, max_size=None)
        print("  ok    doctor joined by voice")
        try:
            await asyncio.sleep(0.5)
            # The doctor speaks first, then the caller — sequentially, so each lands
            # on its own channel without the two tones overlapping.
            await stream(doctor, "doctor_audio", DOCTOR_TONE)
            await asyncio.sleep(0.4)
            await stream(caller, "user_audio", CALLER_TONE)
            await asyncio.sleep(0.6)
        finally:
            await doctor.close()

        await caller.send(json.dumps({"message_type": "end_session"}))
        await asyncio.sleep(1.0)
        reader.cancel()
    return session_id


def fetch_recording(session_id: str, attempts: int = 12) -> bytes | None:
    """Poll S3 for the call's WAV; the upload happens during call teardown."""
    import boto3

    s3 = boto3.client("s3", region_name="us-east-1")
    for attempt in range(attempts):
        response = s3.list_objects_v2(Bucket=BUCKET, Prefix=PREFIX)
        for item in response.get("Contents", []):
            if session_id in item["Key"]:
                print(f"  ok    stored in S3: s3://{BUCKET}/{item['Key']}")
                print(f"        {item['Size'] / 1024:.0f} KB")
                body: bytes = s3.get_object(Bucket=BUCKET, Key=item["Key"])["Body"].read()
                return body
        time.sleep(5)
        print(f"        waiting for the upload ({attempt + 1}/{attempts})")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    session_id = asyncio.run(place_call())
    if session_id is None:
        return 1

    wav = fetch_recording(session_id)
    if wav is None:
        print("  FAIL  no recording appeared in S3 for this call")
        return 1

    channels, rate, left, right = split_channels(wav)
    print(f"  ok    {channels} channels at {rate} Hz")
    if channels != 2:
        print("  FAIL  not stereo, so the two sides cannot be told apart")
        return 1

    caller_rms = rms(left)
    clinic_rms = rms(right)
    print(f"        caller channel (left)  RMS {caller_rms:.4f}")
    print(f"        clinic channel (right) RMS {clinic_rms:.4f}")

    failures: list[str] = []
    if caller_rms > 0.01:
        print("  ok    the patient's voice is on the recording")
    else:
        print("  FAIL  the patient's channel is silent")
        failures.append("caller channel silent")

    if clinic_rms > 0.01:
        print("  ok    the human agent's voice is on the recording")
    else:
        print(
            "  FAIL  the clinic's channel is silent — the doctor's voice was not stored"
        )
        failures.append("clinic channel silent")

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("PASS — the patient and the human agent are both stored in AWS.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
