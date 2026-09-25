"""Ask the deployed agent about a symptom and read back what it actually told the caller.

The routing rules are covered by ``check_symptom_routing.py``, which tests the matcher
against the live configuration. This is the other half: whether the *agent on a real
call* relays the doctor's rule, and whether it stays inside the line when she has
written no rule for what the caller described.

Reads the stored call transcript rather than the socket, because the agent's own words
do not reliably arrive as transcript frames inside a short listening window.

    python scripts/verify_symptom_advice.py
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
LOCAL_WS = "ws://127.0.0.1:8081/ws"
TABLE = "clinic-front-desk"
REGION = "us-east-1"

#: (what the caller says, what the answer must contain, what it must NOT contain, label)
CASES: tuple[tuple[str, tuple[str, ...], tuple[str, ...], str], ...] = (
    (
        "I have severe itching inside my nose. Which service should I book?",
        ("ent consultation",),
        # It must not name a test or procedure off its own reasoning.
        ("endoscopy", "allergy testing", "surgery", "septoplasty"),
        "a routed symptom",
    ),
    (
        "My knee has been hurting for a week. Which service should I book?",
        # Two answers are within the rules here, and the model uses both: refuse to
        # advise and offer the generic consultation, or say the clinic does not treat
        # this and point elsewhere. Asserting one exact phrasing failed a correct
        # agent twice. What matters is that it answers, refuses to judge the symptom,
        # and does not invent a service for it.
        (
            "advise on symptoms",
            "don't treat",
            "do not treat",
            "specializes in",
            "specialises in",
            "orthopedic",
            "orthopaedic",
        ),
        # What must never appear: framings that imply the agent weighed the symptom
        # and reached a conclusion. Naming the consultation is correct here; calling
        # it the best option, or restating the symptom as the reason, is not.
        ("best option", "safest", "i recommend", "sounds like", "for your knee"),
        "an unrouted symptom",
    ),
)


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
        # Let the answer finish; hanging up early truncates it mid-sentence.
        await asyncio.sleep(listen)
        with contextlib.suppress(Exception):
            await socket.send(json.dumps({"message_type": "end_session"}))
        await asyncio.sleep(1.0)
        reader.cancel()
    return session_id


def agent_lines(session_id: str, attempts: int = 12, gap: float = 5.0) -> str:
    import boto3
    from boto3.dynamodb.conditions import Attr

    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    for _ in range(attempts):
        found = table.scan(
            FilterExpression=Attr("entity").eq("CallSession")
            & Attr("id").eq(session_id)
        ).get("Items", [])
        if found and found[0].get("transcript"):
            stored = str(found[0]["transcript"])
            return "\n".join(
                line for line in stored.splitlines() if "agent:" in line
            )
        time.sleep(gap)
    return ""


async def run(url: str, listen: float) -> int:
    failures: list[str] = []

    for question, must, must_not, label in CASES:
        print(f'\n  caller: "{question}"')
        session_id = await ask(url, question, listen)
        if not session_id:
            print("  FAIL  the call never started")
            failures.append(label)
            continue

        said = agent_lines(session_id)
        if not said:
            print("  FAIL  no agent lines in the stored transcript")
            failures.append(label)
            continue
        for line in said.splitlines():
            print(f"    {line}")

        lowered = said.lower()
        if any(token in lowered for token in must):
            print(f"  ok    handled {label} as configured")
        else:
            print(f"  FAIL  {label}: expected one of {must}")
            failures.append(label)

        overstepped = [token for token in must_not if token in lowered]
        if overstepped:
            print(f"  FAIL  {label}: went beyond the doctor's rule -> {overstepped}")
            failures.append(f"{label} overstepped")
        else:
            print("  ok    stayed inside the doctor's own wording")

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("PASS — the agent relays the doctor's routing and stops where it ends.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true")
    # Generous: the routed case involves a tool call mid-answer, and hanging up early
    # truncates the reply. A first run cut the agent off at "let me check what the
    # clinic sees that under" and recorded it as a failure to answer.
    parser.add_argument("--listen", type=float, default=35.0)
    args = parser.parse_args()
    return asyncio.run(run(LOCAL_WS if args.local else PUBLIC_WS, args.listen))


if __name__ == "__main__":
    raise SystemExit(main())
