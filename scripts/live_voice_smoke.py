"""Live end-to-end smoke against real Amazon Nova Sonic on Bedrock.

Genuine AWS voice pipeline, all with your ambient credentials (makes real,
billable calls):

    Amazon Polly (TTS)  ->  spoken PCM audio for a patient prompt
        -> streamed as audio input into Amazon Nova Sonic (speech-to-speech)
        -> Nova Sonic streams back a transcript + synthesized audio response

This drives the real ``BidiNovaSonicModel`` through the Voice_Front_Desk's
``NovaSonicVoiceStream`` adapter, proving the integration works end to end
against Bedrock (not a fake).

Run:  python scripts/live_voice_smoke.py
"""

from __future__ import annotations

import asyncio
import base64
import contextlib

import boto3

from clinic_front_desk.voice.stream import AudioOutput, NovaSonicVoiceStream

REGION = "us-east-1"
PROMPT_TEXT = "Hi, what are your clinic hours and where are you located?"
SYSTEM_PROMPT = (
    "You are the front desk assistant for an ENT clinic. Be brief and friendly. "
    "The clinic is open Monday to Friday, 9am to 5pm, at 123 Main Street. "
    "Only handle administrative questions."
)
INPUT_RATE = 16000  # Nova Sonic expects 16 kHz mono PCM input
FRAME_BYTES = 1024  # ~32 ms of 16-bit mono @ 16 kHz
OVERALL_DEADLINE = 60.0


def synthesize_prompt_pcm() -> bytes:
    """Use Amazon Polly to synthesize the prompt as 16 kHz mono PCM (real TTS)."""
    polly = boto3.client("polly", region_name=REGION)
    resp = polly.synthesize_speech(
        Text=PROMPT_TEXT,
        OutputFormat="pcm",
        VoiceId="Joanna",
        SampleRate=str(INPUT_RATE),
    )
    return resp["AudioStream"].read()


async def main() -> int:
    print("1/4  Synthesizing the patient prompt with Amazon Polly...")
    pcm = synthesize_prompt_pcm()
    # Append ~1s of silence so Nova Sonic's turn detector sees end-of-speech.
    pcm += b"\x00" * (INPUT_RATE * 2)
    print(f"     Polly returned {len(pcm)} PCM bytes (~{len(pcm) / (INPUT_RATE * 2):.1f}s).")

    stream = NovaSonicVoiceStream(
        region=REGION,
        voice_id="matthew",
        system_prompt=SYSTEM_PROMPT,
    )

    print("2/4  Opening the real Nova Sonic bidirectional stream on Bedrock...")
    await stream.start()

    transcripts: list[str] = []
    audio_chunks = 0
    got_response = asyncio.Event()
    last_audio_at = 0.0

    async def pump_audio() -> None:
        for i in range(0, len(pcm), FRAME_BYTES):
            frame = pcm[i : i + FRAME_BYTES]
            await stream.send_audio(
                base64.b64encode(frame).decode("ascii"),
                format="pcm",
                sample_rate=INPUT_RATE,
                channels=1,
            )
            await asyncio.sleep(0.03)  # roughly real-time pacing

    async def read_events() -> None:
        nonlocal audio_chunks, last_audio_at
        loop = asyncio.get_running_loop()
        async for evt in stream.events():
            kind = getattr(evt, "kind", None)
            if kind == "connected":
                print(f"     [connected] model={evt.model}")
            elif kind == "interpreted_turn":
                transcripts.append(f"{evt.role}: {evt.text}")
                print(f"     [transcript:{evt.role}] {evt.text}")
            elif isinstance(evt, AudioOutput) or kind == "audio_output":
                audio_chunks += 1
                last_audio_at = loop.time()
            elif kind == "response_completed":
                print("     [response complete]")
                got_response.set()
                return
            elif kind == "error":
                print(f"     [error] {evt.message}")

    async def idle_monitor() -> None:
        # End the turn gracefully once the assistant's audio has stopped
        # arriving for a short grace period (Nova keeps the connection open).
        loop = asyncio.get_running_loop()
        while not got_response.is_set():
            await asyncio.sleep(0.5)
            if audio_chunks > 0 and (loop.time() - last_audio_at) > 3.0:
                print("     [assistant reply finished streaming]")
                got_response.set()
                return

    print("3/4  Streaming the spoken prompt in and reading Nova Sonic's reply...")
    reader = asyncio.create_task(read_events())
    pumper = asyncio.create_task(pump_audio())
    monitor = asyncio.create_task(idle_monitor())
    try:
        await asyncio.wait_for(got_response.wait(), timeout=OVERALL_DEADLINE)
    except asyncio.TimeoutError:
        print("     [deadline reached]")
    finally:
        # Let the input finish writing before closing so awscrt does not tear
        # down a stream mid-write (which produces noisy background errors).
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pumper
        for task in (reader, monitor):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            await stream.close()
        # Give awscrt's background loop a moment to settle its callbacks.
        await asyncio.sleep(0.3)

    print("4/4  Result")
    print(f"     transcripts        = {transcripts or '<none>'}")
    print(f"     audio chunks back  = {audio_chunks}")
    ok = bool(transcripts) or audio_chunks > 0
    print(f"     LIVE_NOVA_SONIC_OK = {ok}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
