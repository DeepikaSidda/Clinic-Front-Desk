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


#: A question the agent demonstrably answers — this is the shape of turn that caused
#: the reported bug, where it offered appointment slots over the top of the doctor.
PROBE_QUESTION = "What are your opening hours on Monday?"


def speech(text: str) -> bytes:
    """``text`` as 16 kHz mono PCM, via Polly.

    Needed because a text turn is not a probe the model reliably answers, and a
    synthetic tone is not speech. An earlier version of this script sent
    ``user_text`` and reported a pass with the fix reverted — it proved nothing. The
    only probe worth trusting is one confirmed to provoke a reply.
    """
    import boto3

    client = boto3.client("polly", region_name="us-east-1")
    response = client.synthesize_speech(
        Text=text,
        OutputFormat="pcm",
        VoiceId="Joanna",
        Engine="neural",
        SampleRate="16000",
    )
    audio: bytes = response["AudioStream"].read()
    return audio


#: 16 kHz, 16-bit mono: one second of audio is 32000 bytes.
BYTES_PER_SECOND = 32_000


async def say_as_caller(
    socket: Any,
    pcm: bytes,
    *,
    frame: int = 1024,
    lead: float = 0.4,
    tail: float = 1.8,
) -> None:
    """Stream PCM in at roughly real time, as a browser microphone would.

    Two details matter, and both were wrong first time:

    * **Paced, not burst.** Delivered all at once, Nova Sonic's voice-activity
      detection does not see a turn the way it does from a live microphone.
    * **Padded with silence.** A real microphone keeps streaming after the speaker
      stops, and that trailing silence is what marks the end of the turn. Cutting the
      stream dead at the last word meant the model never decided the caller had
      finished, so it never replied at all.
    """
    padded = (
        bytes(int(BYTES_PER_SECOND * lead)) + pcm + bytes(int(BYTES_PER_SECOND * tail))
    )
    for offset in range(0, len(padded), frame):
        chunk = padded[offset : offset + frame]
        await socket.send(
            json.dumps(
                {
                    "message_type": "user_audio",
                    "audio": base64.b64encode(chunk).decode("ascii"),
                    "format": "pcm",
                    "sample_rate": 16000,
                    "channels": 1,
                }
            )
        )
        await asyncio.sleep(frame / 2 / 16000)


async def _unused(socket: Any, pcm: bytes, *, frame: int = 1024) -> None:
    for offset in range(0, len(pcm), frame):
        chunk = pcm[offset : offset + frame]
        await socket.send(
            json.dumps(
                {
                    "message_type": "user_audio",
                    "audio": base64.b64encode(chunk).decode("ascii"),
                    "format": "pcm",
                    "sample_rate": 16000,
                    "channels": 1,
                }
            )
        )
        await asyncio.sleep(frame / 2 / 16000)


#: What the caller's own transcribed speech is labelled. Nova Sonic says "user"; the
#: handover paths say "patient". Getting this wrong made an earlier run of this script
#: report the caller's own question back as proof the agent had answered.
CALLER_ROLES = ("user", "patient")


def agent_lines(messages: list[dict[str, Any]]) -> list[str]:
    """Anything the model said, as opposed to the caller or the human."""
    return [
        str(m.get("text", ""))
        for m in messages
        if m.get("message_type") == "transcript"
        and m.get("role") not in CALLER_ROLES
        and m.get("role") not in ("doctor", "human")
    ]


def agent_frames(messages: list[dict[str, Any]]) -> int:
    """Count of assistant audio frames — what the caller actually *hears*.

    The strongest available signal. The reported bug was hearing the agent, and its
    reply arrives as audio well before any text turn does, so counting frames catches
    it where counting transcript lines can miss it entirely.
    """
    return sum(1 for m in messages if m.get("message_type") == "agent_audio")


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

        # 0. Prove the probe provokes a reply *before* anyone takes the call.
        #
        # Without this the silence checked later is worthless: a probe the agent
        # would ignore anyway passes whether the fix is present or not. That exact
        # mistake was made here once already.
        try:
            probe_pcm = speech(PROBE_QUESTION)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  could not synthesise the probe: {type(exc).__name__}: {exc}")
            caller_reader.cancel()
            return 1

        mark = len(heard_by_caller)
        await say_as_caller(caller, probe_pcm)
        for _ in range(160):
            # Audio, not text: the agent's reply is heard well before any transcript
            # line for it arrives, and on a short question none may arrive at all.
            if agent_frames(heard_by_caller[mark:]):
                break
            await asyncio.sleep(0.25)
        await asyncio.sleep(2.0)

        baseline = agent_frames(heard_by_caller[mark:])
        if baseline:
            print(f"  ok    probe is valid: the agent answered with {baseline} frames")
        else:
            print(
                "  FAIL  the agent never answered the probe, so this script cannot "
                "tell silence-because-fixed from silence-because-ignored"
            )
            caller_reader.cancel()
            return 1

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

        # 5. the agent stays out of it
        #
        # The bug this catches, reported from a real call: the caller was talking to
        # the doctor and the agent kept answering over the top — offering slots,
        # asking for a mobile number, and printing "interrupted — playback stopped"
        # every time the caller spoke. Muting its audio was not enough; it was still
        # being fed the conversation and still replying through every other channel.
        # The same question that just worked, asked again with the doctor on the line.
        mark = len(heard_by_caller)
        await say_as_caller(caller, probe_pcm)
        await asyncio.sleep(6.0)  # generous: it answered inside this window above
        after = heard_by_caller[mark:]

        # The doctor sends nothing during this window, so any assistant audio on the
        # caller's channel is the agent talking over her — which is precisely what
        # the caller complained of hearing.
        spoke = agent_frames(after)
        intruded = agent_lines(after)
        if spoke or intruded:
            detail = f"{spoke} audio frames"
            if intruded:
                detail += f', text: "{intruded[0][:60]}"'
            print(f"  FAIL  the agent talked over the doctor: {detail}")
            failures.append("agent interjected")
        else:
            print(
                "  ok    the agent stayed silent on the same question it just answered"
            )

        # Feeding the model silence also stops it transcribing, so the written record
        # pauses for the human-held stretch — the conversation lives on the call
        # recording instead. The gap has to be *marked*, or a reader of the transcript
        # would take it for a fault.
        code, body = http("GET", f"/dashboard/live/{session_id}/transcript" + ROLE)
        turns = (body or {}).get("turns", []) if code == 200 else []
        if any("handed to a member of the clinic team" in t.get("text", "") for t in turns):
            print("  ok    the transcript records where the human took over")
        else:
            print(f"  FAIL  nothing marks the handover in the transcript ({code})")
            failures.append("handover not marked")

        barged = [m for m in after if m.get("message_type") == "barge_in"]
        if barged:
            print(f"  FAIL  {len(barged)} barge-in notices reached the caller")
            failures.append("barge_in leaked")
        else:
            print("  ok    no spurious 'interrupted' notices on the caller's screen")

        # And the agent is genuinely still there for the hand-back.
        # 6. her tab dying hands the call back rather than leaving dead air
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
