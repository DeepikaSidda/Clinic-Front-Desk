"""Talk to the Voice_Front_Desk agent over the running server's /ws endpoint.

This is the end-to-end agent test: unlike ``live_voice_smoke.py`` (which drives
Nova Sonic in isolation with a hardcoded prompt), this connects to the real
``/ws`` transport, so the session has the **nine Strands tools bound to the live
Data_Layer**, the administrative-only guardrail prompt, the barge-in/turn
controller, and Call_Session persistence. A booking made here lands in the same
stores the dashboard reads, so you can watch the schedule update live.

Prerequisites
-------------
1. A server running with a real voice stream and a configured clinic::

       python scripts/demo_dashboard.py

2. AWS credentials with Bedrock Nova Sonic access (check with
   ``python scripts/check_aws.py``). This makes real, billable Bedrock calls.

How your input reaches the model
-------------------------------
Nova Sonic is a **speech-to-speech** model: it responds to streamed audio, not to
text turns. Sending a bare text turn opens the stream and is accepted, but the
model produces no reply (verified against Bedrock). So everything you type here is
synthesized to 16 kHz PCM with **Amazon Polly** and streamed in as real audio,
which exercises the whole pipeline — ASR, turn detection, tool-calling reasoning,
TTS — exactly as a phone call would.

**Interactive (default)** — type a line, hear what the agent does with it::

    python scripts/talk_to_agent.py

**One-shot** — useful for scripted checks::

    python scripts/talk_to_agent.py --speak "Hi, I'd like to book a hearing test"

Add ``--save-audio reply.wav`` to write the agent's spoken reply so you can listen
to it. ``--text-only`` sends raw text turns instead of audio; it is kept for
exercising that code path, and is expected to produce no model reply.

Things worth trying
-------------------
    "What are your hours?"                      -> answer_faq
    "How much is a hearing test?"               -> answer_faq (pricing)
    "I'd like to book a hearing test"           -> check_availability
    "Do you treat tinnitus?"                    -> not an offered service
    "My ear hurts, what's wrong with me?"       -> must refuse and escalate,
                                                   never infer a service (Req 10)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import sys
import wave

DEFAULT_URL = "ws://127.0.0.1:8080/ws"
INPUT_RATE = 16000  # Nova Sonic expects 16 kHz mono PCM in
OUTPUT_RATE = 24000  # and streams 24 kHz mono PCM back
FRAME_BYTES = 1024  # ~32 ms per frame at 16 kHz 16-bit mono


def synthesize_pcm(text: str, region: str) -> bytes:
    """Synthesize ``text`` to 16 kHz mono PCM with Amazon Polly."""
    import boto3

    polly = boto3.client("polly", region_name=region)
    response = polly.synthesize_speech(
        Text=text, OutputFormat="pcm", VoiceId="Joanna", SampleRate=str(INPUT_RATE)
    )
    pcm: bytes = response["AudioStream"].read()
    # Trailing silence so Nova Sonic's turn detector sees end-of-speech. Two
    # seconds rather than one: the endpointing sensitivity is MEDIUM, and a
    # too-short tail leaves the model waiting for more speech instead of taking
    # its turn — which looks exactly like "the agent never replied".
    return pcm + b"\x00" * (INPUT_RATE * 2 * 2)


def write_wav(path: str, pcm: bytes, rate: int) -> None:
    """Write mono 16-bit PCM to a playable WAV file."""
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)


class AgentClient:
    """One conversation with the Voice_Front_Desk over ``/ws``."""

    def __init__(
        self, socket: object, *, save_audio: str | None, debug: bool = False
    ) -> None:
        self._socket = socket
        self._save_audio = save_audio
        self._debug = debug
        self._reply_pcm = bytearray()
        self.session_id: str | None = None
        self.outcome: str | None = None
        self.transcripts: list[tuple[str, str]] = []
        self.audio_chunks = 0
        self._idle = asyncio.Event()

    async def send_text(self, text: str) -> None:
        await self._socket.send(  # type: ignore[attr-defined]
            json.dumps({"message_type": "user_text", "text": text})
        )

    async def send_audio(self, pcm: bytes) -> None:
        """Stream PCM in at roughly real time, as a phone line would."""
        for offset in range(0, len(pcm), FRAME_BYTES):
            frame = pcm[offset : offset + FRAME_BYTES]
            await self._socket.send(  # type: ignore[attr-defined]
                json.dumps(
                    {
                        "message_type": "user_audio",
                        "audio": base64.b64encode(frame).decode("ascii"),
                        "format": "pcm",
                        "sample_rate": INPUT_RATE,
                        "channels": 1,
                    }
                )
            )
            await asyncio.sleep(0.03)

    async def end(self) -> None:
        with contextlib.suppress(Exception):
            await self._socket.send(  # type: ignore[attr-defined]
                json.dumps({"message_type": "end_session"})
            )

    async def read_forever(self) -> None:
        """Print every server message until the session ends."""
        async for raw in self._socket:  # type: ignore[attr-defined]
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            kind = message.get("message_type")
            if self._debug and kind != "agent_audio":
                print(f"    [raw] {message}")

            if kind == "session_started":
                self.session_id = message.get("session_id")
                print(f"  [session {self.session_id} started]\n")
            elif kind == "transcript":
                role = message.get("role", "?")
                text = message.get("text", "")
                self.transcripts.append((role, text))
                who = "you" if role == "user" else "agent"
                print(f"  {who}: {text}")
                self._idle.set()
            elif kind == "agent_audio":
                self.audio_chunks += 1
                if self._save_audio:
                    self._reply_pcm.extend(base64.b64decode(message.get("audio", "")))
                self._idle.set()
            elif kind == "session_ended":
                self.outcome = message.get("outcome")
                print(f"\n  [session ended, outcome = {self.outcome}]")
                return

    async def wait_for_reply(
        self,
        *,
        first_output: float = 25.0,
        quiet_for: float = 3.0,
        deadline: float = 90.0,
    ) -> None:
        """Wait for the agent's reply to arrive and then finish.

        Two phases, because the two waits are not the same length. Before any
        output there is a real gap: ASR has to finalize the turn, the model reasons
        and may call a tool that reads the Data_Layer, and only then does speech
        start streaming — comfortably more than a few seconds. Once audio *is*
        flowing, chunks arrive continuously, so a short silence means the reply is
        over.

        Using one short window for both was the bug that made replies look absent:
        the client hung up before the agent had said anything.
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        self._idle.clear()

        # Phase 1: wait for the first sign of a reply.
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=first_output)
        except TimeoutError:
            print("  [no reply within "
                  f"{first_output:.0f}s — the model may not have taken the turn]")
            return

        # Phase 2: drain until the output goes quiet.
        while loop.time() - started < deadline:
            self._idle.clear()
            try:
                await asyncio.wait_for(self._idle.wait(), timeout=quiet_for)
            except TimeoutError:
                return

    def finish(self) -> None:
        if self._save_audio and self._reply_pcm:
            write_wav(self._save_audio, bytes(self._reply_pcm), OUTPUT_RATE)
            seconds = len(self._reply_pcm) / (OUTPUT_RATE * 2)
            print(f"  [wrote {self._save_audio} — {seconds:.1f}s of agent speech]")


async def run(args: argparse.Namespace) -> int:
    try:
        import websockets
    except ModuleNotFoundError:
        print("This script needs the websockets package:  pip install websockets")
        return 2

    print(f"Connecting to {args.url} ...")
    # AgentCore sets this header per runtime session; the server uses it as the
    # Call_Session id. Passing it explicitly makes a call easy to look up
    # afterwards (an `interrupted` call is filtered out of the activity log by
    # design, so the id is the only handle on it).
    headers = (
        {"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": args.session_id}
        if args.session_id
        else None
    )
    try:
        socket = await websockets.connect(
            args.url, max_size=None, additional_headers=headers
        )
    except OSError as exc:
        print(f"\nCould not connect: {exc}")
        print("Start the server first:  python scripts/demo_dashboard.py")
        return 2

    client = AgentClient(socket, save_audio=args.save_audio, debug=args.debug)
    reader = asyncio.create_task(client.read_forever())

    try:
        # Wait for session_started before sending anything.
        for _ in range(100):
            if client.session_id:
                break
            await asyncio.sleep(0.05)

        async def turn(line: str) -> None:
            """Send one patient turn and wait for the agent to finish replying."""
            if args.text_only:
                # Kept for exercising the code path; Nova Sonic is
                # speech-to-speech and will not reply to a bare text turn.
                await client.send_text(line)
            else:
                pcm = await asyncio.to_thread(synthesize_pcm, line, args.region)
                await client.send_audio(pcm)
            await client.wait_for_reply()

        if args.speak:
            print(f"  you: {args.speak}")
            await turn(args.speak)
        elif args.say:
            for line in args.say:
                print(f"  you: {line}")
                await turn(line)
        else:
            how = "as text" if args.text_only else "spoken via Polly"
            print(f"Type a line and press Enter — it is sent {how}.")
            print("Blank line or Ctrl-C ends the call.\n")
            while True:
                line = await asyncio.to_thread(input, "  you: ")
                if not line.strip():
                    break
                await turn(line)
    except (KeyboardInterrupt, EOFError):
        print("\n  [hanging up]")
    finally:
        await client.end()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(reader, timeout=10)
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await reader
        with contextlib.suppress(Exception):
            await socket.close()

    client.finish()
    print(f"\n  transcript turns   = {len(client.transcripts)}")
    print(f"  agent audio chunks = {client.audio_chunks}")
    print(f"  call outcome       = {client.outcome}")
    if client.session_id:
        print(
            "\n  The Call_Session was persisted — it should now appear in the "
            "dashboard's call activity log."
        )
    return 0 if (client.transcripts or client.audio_chunks) else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="the /ws endpoint")
    parser.add_argument(
        "--speak", metavar="TEXT", help="send a single turn, then hang up"
    )
    parser.add_argument(
        "--say",
        metavar="TEXT",
        action="append",
        help="send TEXT as a turn (repeatable, non-interactive)",
    )
    parser.add_argument(
        "--text-only",
        action="store_true",
        help=(
            "send raw text turns instead of Polly audio; exercises that path but "
            "Nova Sonic is speech-to-speech and will not reply"
        ),
    )
    parser.add_argument(
        "--save-audio",
        metavar="PATH",
        help="write the agent's spoken reply to a WAV file",
    )
    parser.add_argument(
        "--session-id",
        metavar="ID",
        help="use this Call_Session id, so the call is easy to look up afterwards",
    )
    parser.add_argument("--region", default="us-east-1", help="region for Polly")
    parser.add_argument(
        "--debug", action="store_true", help="print every server message"
    )
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
