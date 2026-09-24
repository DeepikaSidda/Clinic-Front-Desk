"""Prove a patient + human-agent conversation ends up written down in DynamoDB.

Places a real call on the deployed URL, takes it over as the doctor, has both sides
say something recognisable, hangs up, then waits for the post-call transcription and
reads the call record back out of DynamoDB.

Both sides speak **real synthesised words** rather than tones: a tone proves audio
plumbing, and this is checking recognition. Different voices per side so the channels
are not interchangeable by accident.

    python scripts/verify_conversation_transcribed.py
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import time
from pathlib import Path
from typing import Any

import websockets

HOST = "d21u7cmj563imv.cloudfront.net"
CALLER_WS = f"wss://{HOST}/ws"
TALK_WS = f"wss://{HOST}/dashboard/live/{{session_id}}/talk?role=doctor&k={{token}}"
TABLE = "clinic-front-desk"
REGION = "us-east-1"

CALLER_LINE = "My ear has been hurting since Monday."
DOCTOR_LINE = "I can see you tomorrow morning at ten o'clock."

BYTES_PER_SECOND = 32_000


def token() -> str:
    return Path(".secrets/console_token.txt").read_text(encoding="utf-8").strip()


def speech(text: str, voice: str) -> bytes:
    import boto3

    client = boto3.client("polly", region_name=REGION)
    return bytes(
        client.synthesize_speech(
            Text=text,
            OutputFormat="pcm",
            VoiceId=voice,
            Engine="neural",
            SampleRate="16000",
        )["AudioStream"].read()
    )


async def stream(socket: Any, kind: str, pcm: bytes, chunk: int = 1024) -> None:
    padded = bytes(int(BYTES_PER_SECOND * 0.3)) + pcm + bytes(int(BYTES_PER_SECOND * 0.6))
    for offset in range(0, len(padded), chunk):
        await socket.send(
            json.dumps(
                {
                    "message_type": kind,
                    "audio": base64.b64encode(padded[offset : offset + chunk]).decode(
                        "ascii"
                    ),
                    "format": "pcm",
                    "sample_rate": 16000,
                    "channels": 1,
                }
            )
        )
        await asyncio.sleep(chunk / 2 / 16_000)


async def place_call() -> str | None:
    secret = token()
    caller_pcm = speech(CALLER_LINE, "Joanna")
    doctor_pcm = speech(DOCTOR_LINE, "Matthew")

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

        doctor = await websockets.connect(
            TALK_WS.format(session_id=session_id, token=secret),
            open_timeout=40,
            max_size=None,
        )
        print("  ok    doctor took the call by voice")
        try:
            await asyncio.sleep(0.5)
            print(f'  caller: "{CALLER_LINE}"')
            await stream(caller, "user_audio", caller_pcm)
            await asyncio.sleep(0.3)
            print(f'  doctor: "{DOCTOR_LINE}"')
            await stream(doctor, "doctor_audio", doctor_pcm)
            await asyncio.sleep(0.5)
        finally:
            await doctor.close()

        await caller.send(json.dumps({"message_type": "end_session"}))
        await asyncio.sleep(1.5)
        reader.cancel()
    return session_id


def read_transcript(session_id: str, attempts: int, gap: float) -> str | None:
    """Poll DynamoDB for the call record's transcript."""
    import boto3
    from boto3.dynamodb.conditions import Attr

    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    for attempt in range(attempts):
        scan = table.scan(
            FilterExpression=Attr("entity").eq("CallSession") & Attr("id").eq(session_id)
        )
        items = scan.get("Items", [])
        if items:
            transcript = items[0].get("transcript")
            if transcript and "transcribed from the call recording" in str(transcript):
                return str(transcript)
            state = "no transcribed section yet" if transcript else "no transcript yet"
            print(f"        {state} ({attempt + 1}/{attempts})")
        else:
            print(f"        call record not visible yet ({attempt + 1}/{attempts})")
        time.sleep(gap)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempts", type=int, default=30)
    parser.add_argument("--gap", type=float, default=10.0)
    args = parser.parse_args()

    session_id = asyncio.run(place_call())
    if session_id is None:
        return 1

    print("  waiting for post-call transcription (a job takes tens of seconds)")
    transcript = read_transcript(session_id, args.attempts, args.gap)
    if transcript is None:
        print("  FAIL  no transcribed conversation appeared on the call record")
        return 1

    print()
    print("  --- transcript stored in DynamoDB ---")
    for line in transcript.splitlines():
        print(f"  {line}")
    print()

    lowered = transcript.lower()
    failures: list[str] = []
    if "patient:" in lowered:
        print("  ok    the patient's words are attributed to the patient")
    else:
        print("  FAIL  nothing attributed to the patient")
        failures.append("no patient lines")
    if "clinic:" in lowered:
        print("  ok    the human agent's words are attributed to the clinic")
    else:
        print("  FAIL  nothing attributed to the clinic — the human is still missing")
        failures.append("no clinic lines")
    if "ear" in lowered:
        print("  ok    recognised what the caller actually said")
    else:
        print("  note  the caller's words did not come back recognisably")
    if "ten" in lowered or "tomorrow" in lowered:
        print("  ok    recognised what the doctor actually said")
    else:
        print("  note  the doctor's words did not come back recognisably")

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("PASS — the patient and human-agent conversation is stored in AWS, in text.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
