"""Ask the deployed agent for the clinic's number and the visit fee, and read it back.

Checks the **stored call transcript**, not the live socket. Two reasons, both learned
the hard way: the agent's words do not reliably arrive as transcript frames inside a
short listening window, and the transcript is what the doctor actually reads later. An
earlier version watched only the socket and reported that the agent had said nothing
while it was plainly speaking.

    python scripts/verify_number_and_fee.py
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import time
from typing import Any

import websockets

PUBLIC_WS = "wss://d21u7cmj563imv.cloudfront.net/ws"
LOCAL_WS = "ws://127.0.0.1:8080/ws"
TABLE = "clinic-front-desk"
REGION = "us-east-1"

#: (question, what a correct answer must contain, label)
QUESTIONS = (
    ("What is the clinic's phone number?", ("1234567890",), "the clinic's number"),
    (
        "How much does an ENT Consultation cost?",
        ("500",),
        "the consultation fee",
    ),
)

#: Must never appear: the fee is in rupees, and a caller quoted dollars is being told
#: a number roughly eighty times the real one.
FORBIDDEN = ("$", "dollar")


def speech(text: str) -> bytes:
    import boto3

    client = boto3.client("polly", region_name=REGION)
    return bytes(
        client.synthesize_speech(
            Text=text,
            OutputFormat="pcm",
            VoiceId="Joanna",
            Engine="neural",
            SampleRate="16000",
        )["AudioStream"].read()
    )


async def ask(url: str, question: str, listen: float) -> str:
    """Place a call, ask one question, let the agent finish, return the session id."""
    pcm = bytes(12_800) + speech(question) + bytes(57_600)
    heard: list[dict[str, Any]] = []

    async with websockets.connect(url, open_timeout=40, max_size=None) as socket:

        async def drain() -> None:
            with contextlib.suppress(Exception):
                async for raw in socket:
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
            "",
        )

        for offset in range(0, len(pcm), 1024):
            await socket.send(
                json.dumps(
                    {
                        "message_type": "user_audio",
                        "audio": base64.b64encode(pcm[offset : offset + 1024]).decode(
                            "ascii"
                        ),
                        "format": "pcm",
                        "sample_rate": 16000,
                        "channels": 1,
                    }
                )
            )
            await asyncio.sleep(0.032)

        # Let the answer finish. Hanging up early cuts it off mid-sentence and the
        # recording then holds only the first word — which an earlier version of this
        # script mistook for the agent saying nothing.
        await asyncio.sleep(listen)
        with contextlib.suppress(Exception):
            await socket.send(json.dumps({"message_type": "end_session"}))
        await asyncio.sleep(1.0)
        reader.cancel()
    return session_id


def transcript_of(session_id: str, attempts: int = 12, gap: float = 5.0) -> str:
    import boto3
    from boto3.dynamodb.conditions import Attr

    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    for _ in range(attempts):
        found = table.scan(
            FilterExpression=Attr("entity").eq("CallSession")
            & Attr("id").eq(session_id)
        ).get("Items", [])
        if found and found[0].get("transcript"):
            return str(found[0]["transcript"])
        time.sleep(gap)
    return ""


async def run(url: str, listen: float) -> int:
    failures: list[str] = []

    for question, expected, label in QUESTIONS:
        print(f'\n  asking: "{question}"')
        session_id = await ask(url, question, listen)
        if not session_id:
            print("  FAIL  the call never started")
            failures.append(label)
            continue

        stored = transcript_of(session_id)
        if not stored:
            print("  FAIL  no transcript was stored for the call")
            failures.append(label)
            continue

        for line in stored.splitlines():
            print(f"    {line}")

        agent_said = " ".join(
            line for line in stored.splitlines() if "agent:" in line
        ).lower()

        if not agent_said:
            print("  FAIL  the transcript has no agent lines at all")
            failures.append(f"{label} (one-sided transcript)")
            continue

        if any(token.lower() in agent_said for token in expected):
            print(f"  ok    the agent stated {label}")
        else:
            print(f"  FAIL  the agent never stated {label}")
            failures.append(label)

        wrong = [token for token in FORBIDDEN if token in agent_said]
        if wrong:
            print(f"  FAIL  quoted money as {wrong}")
            failures.append("dollars")

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("PASS — the agent gives the clinic's number, and the fee in rupees.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--listen", type=float, default=16.0)
    args = parser.parse_args()
    return asyncio.run(run(LOCAL_WS if args.local else PUBLIC_WS, args.listen))


if __name__ == "__main__":
    raise SystemExit(main())
