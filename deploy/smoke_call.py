"""Place a real call through the public URL and check how far it gets.

Everything short of this can pass while the demo is still broken. The page can
render, the WebSocket can upgrade, and the call can still die the moment Nova
Sonic is constructed — which is exactly what happened twice during this deploy
(a missing ``dynamodb:Scan`` grant, then a missing ``[voice]`` extra).

What the two frames actually prove, which is the whole point of this script:

``session_started``
    Sent *after* ``await session.start()`` in deployment/server.py, and that call
    is what opens the Bedrock bidirectional stream. So this frame arriving means
    Nova Sonic accepted the connection under the instance's IAM role. This is
    where the missing ``[voice]`` extra used to raise ModuleNotFoundError.

``clinic_card``
    Built from the knowledge base and the uploaded documents, so this frame means
    DynamoDB reads and the S3 briefing fetch both succeeded. This is where the
    missing ``s3:GetObject`` on ``clinic-documents/*`` used to fail.

``agent_audio`` is deliberately *not* required to pass. The agent waits for real
speech before it takes a turn, and digital silence is not speech — a local server
known to work behaves identically when fed zeros. Treating silent audio as a
failure would report a working deployment as broken.

    python deploy/smoke_call.py            # the public URL
    python deploy/smoke_call.py --local    # control: compare against localhost
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import json

import websockets

URL = "wss://d21u7cmj563imv.cloudfront.net/ws"
#: Point --url at a local server to tell "the deployment is broken" apart from
#: "the agent is waiting for real speech before it says anything".
LOCAL_URL = "ws://127.0.0.1:8080/ws"

CAPTURE_RATE = 16_000
FRAME_SAMPLES = 512  # ~32 ms, matching voice_client.js
SILENCE = base64.b64encode(b"\x00\x00" * FRAME_SAMPLES).decode()

FRAME = json.dumps(
    {
        "message_type": "user_audio",
        "audio": SILENCE,
        "format": "pcm",
        "sample_rate": CAPTURE_RATE,
        "channels": 1,
    }
)

#: Frames that must arrive for the call path to be considered working.
REQUIRED = ("session_started", "clinic_card")


async def place_call(
    seconds: float, send_audio: bool = True, url: str = URL
) -> int:
    counts: collections.Counter[str] = collections.Counter()
    transcript: list[str] = []
    audio_bytes = 0

    print(f"  dialling {url}")
    async with websockets.connect(url, open_timeout=30, max_size=None) as socket:
        print("  connected")

        async def talk() -> None:
            """Stream silence, so the session behaves like an open microphone."""
            try:
                while True:
                    await socket.send(FRAME)
                    await asyncio.sleep(0.032)
            except Exception:  # noqa: BLE001 - the socket closing ends this
                return

        sender = asyncio.create_task(talk()) if send_audio else None
        try:
            async with asyncio.timeout(seconds):
                async for raw in socket:
                    try:
                        message = json.loads(raw)
                    except (TypeError, ValueError):
                        counts["<non-json>"] += 1
                        continue
                    kind = str(message.get("message_type", "<none>"))
                    counts[kind] += 1
                    if kind == "agent_audio":
                        audio_bytes += len(base64.b64decode(message.get("audio") or ""))
                    text = message.get("text") or message.get("content")
                    if text and kind != "agent_audio":
                        transcript.append(f"    [{kind}] {text}")
        except (TimeoutError, asyncio.TimeoutError):
            pass
        finally:
            if sender is not None:
                sender.cancel()
            try:
                await socket.send(json.dumps({"message_type": "end_session"}))
            except Exception:  # noqa: BLE001
                pass

    print("  frames received:")
    for kind, count in counts.most_common():
        print(f"    {count:>5}  {kind}")
    if transcript:
        print("  text seen:")
        for line in transcript[:12]:
            print(line)

    missing = [frame for frame in REQUIRED if not counts.get(frame)]
    print()
    if missing:
        print(f"  FAIL  never received: {', '.join(missing)}")
        print("  Read the server log: python deploy/remote_exec.py --logs")
        return 1

    print("  PASS  Nova Sonic stream opened (session_started after session.start)")
    print("  PASS  clinic briefing built (DynamoDB read + S3 documents read)")
    if counts.get("agent_audio"):
        print(
            f"  PASS  the agent spoke: {counts['agent_audio']} frames, "
            f"{audio_bytes / 1024:.0f} KB of 24 kHz PCM"
        )
    else:
        print("  note  no agent_audio, which is expected: silence is not speech.")
        print("        Speech recognition needs a real microphone. Open the page")
        print("        in a browser and say something to confirm that last mile.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seconds", type=float, default=25.0, help="how long to stay on the call"
    )
    parser.add_argument(
        "--no-mic",
        action="store_true",
        help="listen only; do not stream silence as microphone input",
    )
    parser.add_argument("--url", default=URL, help=f"WebSocket URL (local: {LOCAL_URL})")
    parser.add_argument(
        "--local", action="store_true", help=f"shorthand for --url {LOCAL_URL}"
    )
    args = parser.parse_args()
    url = LOCAL_URL if args.local else args.url
    raise SystemExit(
        asyncio.run(place_call(args.seconds, send_audio=not args.no_mic, url=url))
    )


if __name__ == "__main__":
    main()
