"""Prove a doctor can take over a live call and be heard by the caller.

Unit tests cover the service against a fake socket. This drives the real thing:
opens a WebSocket as a patient would, then acts as the doctor over HTTP and checks
the caller's socket actually receives the human's voice.

What it asserts, in the caller's terms:

1. the call shows up on the doctor's console while it is open;
2. taking it over tells the caller a person joined;
3. what the doctor types arrives as playable audio on the caller's own channel;
4. the frames are labelled at Polly's real sample rate, or they play at the
   wrong pitch;
5. the call disappears from the console once the caller hangs up.

Needs a local server with the dashboard mounted (not the voice-only build):

    python scripts/serve_aws.py --host 127.0.0.1 --port 8080
    python scripts/verify_live_handover.py
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
from typing import Any

import urllib.error
import urllib.request

import websockets

BASE = "http://127.0.0.1:8080"
WS_URL = "ws://127.0.0.1:8080/ws"
ROLE = "?role=doctor"

DOCTOR_LINE = "Hello, this is the clinic. I can help you with that myself."


def http(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            try:
                return response.status, json.loads(raw)
            except ValueError:
                return response.status, raw[:200]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:200]
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


async def run(seconds: float) -> int:
    failures: list[str] = []

    print(f"  patient dialling {WS_URL}")
    async with websockets.connect(WS_URL, open_timeout=30, max_size=None) as socket:
        received: list[dict[str, Any]] = []

        async def drain() -> None:
            try:
                async for raw in socket:
                    try:
                        received.append(json.loads(raw))
                    except (TypeError, ValueError):
                        pass
            except Exception:  # noqa: BLE001
                return

        reader = asyncio.create_task(drain())

        # Wait for the session to open so the registry has it.
        for _ in range(int(seconds * 10)):
            if any(m.get("message_type") == "session_started" for m in received):
                break
            await asyncio.sleep(0.1)

        session_id = next(
            (
                m.get("session_id")
                for m in received
                if m.get("message_type") == "session_started"
            ),
            None,
        )
        if not session_id:
            print("  FAIL  the call never started")
            reader.cancel()
            return 1
        print(f"  ok    call open: {session_id}")

        # 1. visible on the console
        code, body = http("GET", "/dashboard/live" + ROLE)
        ids = [c["session_id"] for c in (body or {}).get("calls", [])] if code == 200 else []
        if session_id in ids:
            print(f"  ok    doctor's console lists it ({code})")
        else:
            print(f"  FAIL  not on the console: {code} {body}")
            failures.append("console listing")

        # 2. take it over
        code, body = http("POST", f"/dashboard/live/{session_id}/takeover" + ROLE)
        print(f"  {'ok  ' if code == 200 else 'FAIL'}  takeover -> {code}")
        if code != 200:
            failures.append("takeover")

        await asyncio.sleep(0.4)
        if any(m.get("message_type") == "human_joined" for m in received):
            print("  ok    caller was told a human joined")
        else:
            print("  FAIL  caller was never told a human joined")
            failures.append("human_joined")

        # 3. the doctor speaks
        before = sum(1 for m in received if m.get("message_type") == "agent_audio")
        code, body = http(
            "POST", f"/dashboard/live/{session_id}/say" + ROLE, {"text": DOCTOR_LINE}
        )
        print(f"  {'ok  ' if code == 200 else 'FAIL'}  say -> {code}")
        if code != 200:
            failures.append("say")

        await asyncio.sleep(1.5)
        frames = [m for m in received if m.get("message_type") == "agent_audio"][before:]
        audio_bytes = sum(len(base64.b64decode(f.get("audio") or "")) for f in frames)
        if frames:
            print(
                f"  ok    caller received {len(frames)} audio frames "
                f"({audio_bytes / 1024:.0f} KB) of the human speaking"
            )
        else:
            print("  FAIL  caller heard nothing")
            failures.append("human audio")

        # 4. correctly labelled
        rates = {f.get("sample_rate") for f in frames}
        if frames and rates == {16000}:
            print("  ok    frames labelled 16000 Hz (Polly's real rate)")
        elif frames:
            print(f"  FAIL  unexpected sample rates: {rates}")
            failures.append("sample rate")

        # the doctor's line is on the shared transcript
        code, body = http("GET", f"/dashboard/live/{session_id}/transcript" + ROLE)
        turns = (body or {}).get("turns", []) if code == 200 else []
        if any(t.get("role") == "human" for t in turns):
            print("  ok    the doctor's line is on the transcript")
        else:
            print(f"  FAIL  transcript has no human turn: {turns}")
            failures.append("transcript")

        reader.cancel()

    # 5. gone once the caller hangs up.
    #
    # Polled rather than checked once: teardown finalises the Call_Session and may
    # flush a recording, and the server has to notice the disconnect first. "Gone
    # within a second" was an unfair assertion that failed intermittently while the
    # cleanup itself was working.
    removed = False
    for _ in range(20):
        await asyncio.sleep(0.5)
        code, body = http("GET", "/dashboard/live" + ROLE)
        ids = (
            [c["session_id"] for c in (body or {}).get("calls", [])]
            if code == 200
            else []
        )
        if session_id not in ids:
            removed = True
            break
    if removed:
        print("  ok    call left the console after hang-up")
    else:
        print("  FAIL  call still listed 10s after hang-up")
        failures.append("unregister")

    print()
    if failures:
        print(f"  FAILED: {', '.join(failures)}")
        return 1
    print("  A doctor can take over a live call and the caller hears them.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=20.0)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.seconds)))


if __name__ == "__main__":
    main()
