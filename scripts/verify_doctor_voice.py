"""Prove the doctor's microphone and the caller's reach each other, for real.

``verify_live_handover.py`` covers the typed handover: the doctor sends text and
Polly speaks it. This covers the other half — her actual voice — which is a
different path entirely: a second WebSocket at
``/dashboard/live/{id}/talk`` carrying raw PCM in both directions.

The assertions match **exact byte payloads**, not message types. That matters here:
the caller's channel already carries ``agent_audio`` frames from Nova Sonic, so
"an agent_audio arrived" proves nothing. Only finding the specific bytes the doctor
sent proves they were relayed.

What it checks, in the doctor's terms:

1. her socket is accepted while a call is open;
2. the caller is told a human joined, same as the typed takeover;
3. audio she speaks arrives on the caller's channel, byte for byte;
4. the caller's own speech comes back to her, byte for byte;
5. hanging up her tab hands the call back to the agent rather than going silent.

Needs a local server with the dashboard mounted (not the voice-only build):

    python scripts/serve_aws.py --host 127.0.0.1 --port 8080
    python scripts/verify_doctor_voice.py
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
TALK_URL = "ws://127.0.0.1:8080/dashboard/live/{session_id}/talk?role=doctor"
ROLE = "?role=doctor"


def marker(seed: int, samples: int = 256) -> bytes:
    """A PCM block no speech synthesiser would produce by chance.

    A hard square wave at an unmistakable amplitude. Used as a fingerprint: if these
    exact bytes come out the other end, they were relayed rather than generated.
    """
    out = bytearray()
    for index in range(samples):
        value = 20_000 if (index // 8) % 2 == 0 else -20_000
        value += seed  # distinguishes the doctor's block from the caller's
        out += int(value).to_bytes(2, "little", signed=True)
    return bytes(out)


DOCTOR_PCM = marker(seed=11)
CALLER_PCM = marker(seed=77)


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


def collect(messages: list[dict[str, Any]], kind: str) -> bytes:
    """All audio of one message type, concatenated, so a payload can be searched for."""
    out = bytearray()
    for message in messages:
        if message.get("message_type") != kind:
            continue
        try:
            out += base64.b64decode(message.get("audio") or "")
        except Exception:  # noqa: BLE001
            continue
    return bytes(out)


async def run(seconds: float) -> int:
    failures: list[str] = []

    print(f"  patient dialling {WS_URL}")
    async with websockets.connect(WS_URL, open_timeout=30, max_size=None) as caller:
        heard_by_caller: list[dict[str, Any]] = []

        async def drain_caller() -> None:
            try:
                async for raw in caller:
                    try:
                        heard_by_caller.append(json.loads(raw))
                    except (TypeError, ValueError):
                        pass
            except Exception:  # noqa: BLE001
                return

        caller_reader = asyncio.create_task(drain_caller())

        for _ in range(int(seconds * 10)):
            if any(
                m.get("message_type") == "session_started" for m in heard_by_caller
            ):
                break
            await asyncio.sleep(0.1)

        session_id = next(
            (
                m.get("session_id")
                for m in heard_by_caller
                if m.get("message_type") == "session_started"
            ),
            None,
        )
        if not session_id:
            print("  FAIL  the call never started")
            caller_reader.cancel()
            return 1
        print(f"  ok    call open: {session_id}")

        # 1. the doctor picks up with her microphone
        talk = TALK_URL.format(session_id=session_id)
        try:
            doctor = await websockets.connect(talk, open_timeout=30, max_size=None)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  doctor's talk socket refused: {type(exc).__name__}: {exc}")
            caller_reader.cancel()
            return 1
        print("  ok    doctor's talk socket accepted")

        heard_by_doctor: list[dict[str, Any]] = []

        async def drain_doctor() -> None:
            try:
                async for raw in doctor:
                    try:
                        heard_by_doctor.append(json.loads(raw))
                    except (TypeError, ValueError):
                        pass
            except Exception:  # noqa: BLE001
                return

        doctor_reader = asyncio.create_task(drain_doctor())
        await asyncio.sleep(0.6)

        # 2. the caller is told, exactly as the typed takeover does
        if any(m.get("message_type") == "human_joined" for m in heard_by_caller):
            print("  ok    caller was told a human joined")
        else:
            print("  FAIL  caller was never told a human joined")
            failures.append("human_joined")

        if any(m.get("message_type") == "joined" for m in heard_by_doctor):
            print("  ok    doctor's socket confirmed the join")
        else:
            print("  FAIL  doctor never got a join confirmation")
            failures.append("joined")

        # 3. her voice reaches the caller — searched for by payload, because the
        #    caller's channel already carries agent_audio from the model.
        for _ in range(6):
            await doctor.send(
                json.dumps(
                    {
                        "message_type": "doctor_audio",
                        "audio": base64.b64encode(DOCTOR_PCM).decode("ascii"),
                        "format": "pcm",
                        "sample_rate": 16000,
                        "channels": 1,
                    }
                )
            )
            await asyncio.sleep(0.05)
        await asyncio.sleep(1.0)

        on_caller_channel = collect(heard_by_caller, "agent_audio")
        if DOCTOR_PCM in on_caller_channel:
            hits = on_caller_channel.count(DOCTOR_PCM)
            print(
                f"  ok    the doctor's own voice reached the caller "
                f"({hits}/6 blocks found, {len(on_caller_channel) / 1024:.0f} KB "
                f"on that channel)"
            )
        else:
            print("  FAIL  the doctor's audio never reached the caller")
            failures.append("doctor -> caller")

        rates = {
            m.get("sample_rate")
            for m in heard_by_caller
            if m.get("message_type") == "agent_audio"
        }
        print(f"  note  sample rates on the caller's channel: {sorted(map(str, rates))}")

        # 4. the caller's voice reaches her
        await caller.send(
            json.dumps(
                {
                    "message_type": "user_audio",
                    "audio": base64.b64encode(CALLER_PCM).decode("ascii"),
                    "format": "pcm",
                    "sample_rate": 16000,
                    "channels": 1,
                }
            )
        )
        await asyncio.sleep(1.0)

        to_doctor = collect(heard_by_doctor, "caller_audio")
        if CALLER_PCM in to_doctor:
            print(
                f"  ok    the caller's voice reached the doctor "
                f"({len(to_doctor) / 1024:.1f} KB)"
            )
        else:
            print("  FAIL  the caller's audio never reached the doctor")
            failures.append("caller -> doctor")

        # 5. her tab dying hands the call back rather than leaving dead air
        await doctor.close()
        await asyncio.sleep(0.8)
        code, body = http("GET", "/dashboard/live" + ROLE)
        calls = (body or {}).get("calls", []) if code == 200 else []
        this_call = next((c for c in calls if c["session_id"] == session_id), None)
        if this_call is None:
            print(f"  FAIL  call vanished from the console after she left ({code})")
            failures.append("call lost on detach")
        elif this_call.get("taken_over") is False:
            print("  ok    the agent took the call back when she dropped off")
        else:
            print(f"  FAIL  call still marked taken over: {this_call}")
            failures.append("stuck taken_over")

        doctor_reader.cancel()
        await caller.send(json.dumps({"message_type": "end_session"}))
        await asyncio.sleep(0.4)
        caller_reader.cancel()

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("PASS — the doctor and the caller can hear each other.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seconds",
        type=float,
        default=20.0,
        help="how long to wait for the call to open",
    )
    args = parser.parse_args()
    return asyncio.run(run(args.seconds))


if __name__ == "__main__":
    raise SystemExit(main())
