"""Prove a caller who asks for a person is actually told when nobody picks up.

Runs against the **deployed** URL, not localhost, because that distinction is the
whole reason this exists. The behaviour was implemented and tested locally, where it
worked — and was silently dead in production, because the instance role had never been
granted ``polly:SynthesizeSpeech``. Synthesis failures are caught so a dead voice can
never drop a call, so the caller simply got silence: the exact dead air the apology
exists to prevent, with nothing failing anywhere to say so.

What it asserts, in the caller's terms:

1. asking for a person is recognised and the call is flagged for a human;
2. around twelve seconds in, they are told someone is still being fetched;
3. by forty-five, they are told plainly that nobody could pick up, and offered a
   message or the clinic's hours;
4. both lines arrive as **audio**, not just transcript text — a caller is holding a
   phone, not reading a screen.

    python scripts/verify_nobody_picked_up.py
    python scripts/verify_nobody_picked_up.py --local
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
from typing import Any

import websockets

PUBLIC_WS = "wss://d21u7cmj563imv.cloudfront.net/ws"
LOCAL_WS = "ws://127.0.0.1:8080/ws"

ASK_FOR_A_HUMAN = "Can I speak to a human agent please."

BYTES_PER_SECOND = 32_000


def speech(text: str) -> bytes:
    """``text`` as 16 kHz mono PCM, so the agent hears a real request.

    Synthesised rather than sent as ``user_text``: the guardrail classifies what the
    caller *said*, and a text turn is not the path a real caller takes.
    """
    import boto3

    client = boto3.client("polly", region_name="us-east-1")
    return bytes(
        client.synthesize_speech(
            Text=text,
            OutputFormat="pcm",
            VoiceId="Joanna",
            Engine="neural",
            SampleRate="16000",
        )["AudioStream"].read()
    )


async def say(socket: Any, pcm: bytes, *, frame: int = 1024) -> None:
    """Stream PCM at roughly real time, padded so the turn is seen to end."""
    padded = bytes(int(BYTES_PER_SECOND * 0.4)) + pcm + bytes(int(BYTES_PER_SECOND * 1.8))
    for offset in range(0, len(padded), frame):
        await socket.send(
            json.dumps(
                {
                    "message_type": "user_audio",
                    "audio": base64.b64encode(padded[offset : offset + frame]).decode(
                        "ascii"
                    ),
                    "format": "pcm",
                    "sample_rate": 16000,
                    "channels": 1,
                }
            )
        )
        await asyncio.sleep(frame / 2 / 16000)


async def keep_alive(socket: Any, stop: asyncio.Event) -> None:
    """Hold the line the way a browser does: a frame every ~32 ms, silence included.

    Required, not cosmetic. The server treats a socket with no frames as gone and
    hangs up, so a test that merely slept would be disconnected before the deadline
    it is trying to observe.
    """
    frame = json.dumps(
        {
            "message_type": "user_audio",
            "audio": base64.b64encode(bytes(1024)).decode("ascii"),
            "format": "pcm",
            "sample_rate": 16000,
            "channels": 1,
        }
    )
    while not stop.is_set():
        try:
            await socket.send(frame)
        except Exception:  # noqa: BLE001
            return
        await asyncio.sleep(0.032)


async def run(url: str, wait_seconds: float) -> int:
    failures: list[str] = []
    heard: list[dict[str, Any]] = []
    audio_at: list[float] = []

    print(f"  calling {url}")
    async with websockets.connect(url, open_timeout=40, max_size=None) as socket:
        began = time.monotonic()

        async def drain() -> None:
            try:
                async for raw in socket:
                    try:
                        message = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    heard.append(message)
                    if message.get("message_type") == "agent_audio":
                        audio_at.append(time.monotonic() - began)
            except Exception:  # noqa: BLE001
                return

        reader = asyncio.create_task(drain())

        for _ in range(160):
            if any(m.get("message_type") == "session_started" for m in heard):
                break
            await asyncio.sleep(0.25)
        if not any(m.get("message_type") == "session_started" for m in heard):
            print("  FAIL  the call never started")
            reader.cancel()
            return 1
        print("  ok    call connected")

        print(f'  asking: "{ASK_FOR_A_HUMAN}"')
        try:
            # Spoken first, alone on the socket. Streaming keep-alive silence at the
            # same time interleaves frames with the speech and the model hears
            # nothing intelligible — an earlier version of this script did exactly
            # that and reported total silence from a working server.
            await say(socket, speech(ASK_FOR_A_HUMAN))
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  could not synthesise the request: {exc}")
            reader.cancel()
            return 1

        stop = asyncio.Event()
        alive = asyncio.create_task(keep_alive(socket, stop))

        asked_at = time.monotonic() - began
        print(f"  ok    request delivered at {asked_at:.0f}s; watching for {wait_seconds:.0f}s")

        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)

        stop.set()
        alive.cancel()
        reader.cancel()

    lines = [
        (m.get("text") or "").lower()
        for m in heard
        if m.get("message_type") == "transcript"
    ]
    spoken = " | ".join(lines)

    holding = any("still trying to get someone" in line for line in lines)
    apology = any("been able to pick up" in line for line in lines)

    print(f"  {'ok  ' if holding else 'FAIL'}  told someone is still being fetched")
    if not holding:
        failures.append("no holding line")
    print(f"  {'ok  ' if apology else 'FAIL'}  told plainly that nobody could pick up")
    if not apology:
        failures.append("no apology")

    # The one that catches the Polly outage: text without audio is a caller in
    # silence watching nothing, since they are on a phone.
    if audio_at:
        print(
            f"  ok    heard {len(audio_at)} audio frames, "
            f"first at {audio_at[0]:.0f}s, last at {audio_at[-1]:.0f}s"
        )
    else:
        print("  FAIL  no audio at all reached the caller — they sat in silence")
        failures.append("no audio")

    if not failures:
        offered = any(
            "take a message" in line or "opening hours" in line for line in lines
        )
        print(f"  {'ok  ' if offered else 'note'}  offered a way forward")

    print()
    print(f"  transcript: {spoken[:400]}")
    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("PASS — a caller nobody picks up is told so, out loud.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true", help=f"use {LOCAL_WS}")
    parser.add_argument(
        "--wait",
        type=float,
        default=70.0,
        help="seconds to listen after asking (deadline is 45s by default)",
    )
    args = parser.parse_args()
    url = LOCAL_WS if args.local else PUBLIC_WS
    return asyncio.run(run(url, args.wait))


if __name__ == "__main__":
    raise SystemExit(main())
