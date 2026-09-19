"""Can Amazon Polly produce audio in the exact format the voice client plays?

The live human-handover path works by synthesising what the doctor types and pushing
it down the patient's existing audio channel. That only works if Polly can emit
**24 kHz mono 16-bit PCM**, which is precisely what ``voice_client.js`` already
decodes for ``agent_audio`` frames. Anything else would mean resampling in Python on
the call path, which is the last place to add work.

    python scripts/check_polly_voice.py
"""

from __future__ import annotations

import boto3

#: Polly's PCM output supports 8 kHz and 16 kHz only — 24 kHz is rejected outright.
#: That turned out not to matter: ``voice_client.js`` reads ``sample_rate`` off each
#: ``agent_audio`` frame and hands it to ``createBuffer``, so the browser resamples
#: on playback. Sending 16 kHz and labelling it honestly beats resampling in Python
#: on the call path.
SAMPLE_RATE = "16000"
OUTPUT_FORMAT = "pcm"

#: Indian-English neural voices, so a human stepping in does not sound like a
#: different clinic to the caller.
CANDIDATES = (
    ("Kajal", "neural"),
    ("Kajal", "standard"),
    ("Aditi", "standard"),
    ("Raveena", "standard"),
    ("Joanna", "neural"),
)

SAMPLE_TEXT = "Hello, this is the clinic. I can help you with that."


def main() -> None:
    polly = boto3.client("polly", region_name="us-east-1")
    print(f"  format {OUTPUT_FORMAT} @ {SAMPLE_RATE} Hz mono\n")

    working: list[str] = []
    for voice, engine in CANDIDATES:
        try:
            response = polly.synthesize_speech(
                Text=SAMPLE_TEXT,
                OutputFormat=OUTPUT_FORMAT,
                VoiceId=voice,
                Engine=engine,
                SampleRate=SAMPLE_RATE,
            )
            audio = response["AudioStream"].read()
            seconds = len(audio) / (int(SAMPLE_RATE) * 2)
            print(f"  ok    {voice:<9} {engine:<9} {len(audio):>7} bytes  ~{seconds:.1f}s")
            working.append(f"{voice}/{engine}")
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {voice:<9} {engine:<9} {type(exc).__name__}: {str(exc)[:70]}")

    print()
    if working:
        print(f"  usable: {', '.join(working)}")
        print("  Polly can voice the human side of a handover.")
    else:
        print("  No usable voice. The typed-to-speech handover is not available.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
