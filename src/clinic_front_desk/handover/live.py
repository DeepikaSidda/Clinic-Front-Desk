"""Live human takeover: the caller stays on the line and talks to a person.

Everything else in this package *notifies* a human. This hands the call over while
the caller is still holding, which is the only version that does not ask them to
wait for a callback.

How it works, and why each piece is where it is:

**A registry of calls in progress.** Each WebSocket handler owns its own session
locally, so nothing outside it could reach a call. :class:`LiveCallRegistry` is the
one place that knows which calls are open and how to speak into them. In-process on
purpose: one instance serves every call, and a call cannot outlive the process
holding its socket, so a shared store would add coordination for nothing.

**The agent goes quiet, but keeps listening.** On takeover the agent's audio is
dropped while its speech-to-text keeps running, so the doctor reads what the caller
says in real time without the caller hearing two voices. The existing barge-in
suppression is not reused for this: that flag clears on the next model response,
which is correct for an interruption and wrong for a handover that must persist
until the human hangs up.

**The human's words arrive as ordinary agent audio.** Amazon Polly synthesises what
the doctor types, and it is pushed down the same ``agent_audio`` channel the caller's
browser is already playing. Polly emits PCM at 16 kHz rather than the 24 kHz Nova
Sonic streams, which turned out not to matter: the client reads the sample rate off
each frame and lets the browser resample, so the honest label works and no
resampling happens on the call path.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

#: Polly emits PCM at 8 kHz or 16 kHz only. 16 kHz is the better of the two, and the
#: browser resamples it to the playback context's rate.
POLLY_SAMPLE_RATE = 16_000

#: An Indian-English neural voice, so a human stepping in does not sound like a
#: different clinic to the caller mid-call.
DEFAULT_VOICE_ID = "Kajal"
DEFAULT_ENGINE = "neural"

#: ~32 ms per frame at 16 kHz, matching the cadence the client already expects.
FRAME_SAMPLES = 512

#: How long a caller waits for a person before the agent comes back to them.
#:
#: This is someone holding a phone in silence, not a support queue, so it cannot be
#: generous. But the first value here was twenty seconds, and that was measured
#: against the wrong thing: it is the time a *caller* will wait, not the time a
#: doctor needs to pick up. Actually answering means noticing the console, finding
#: the call, clicking, and clearing the browser's microphone prompt — the permission
#: dialog alone eats several seconds the first time. Twenty seconds meant the agent
#: apologised while the doctor was mid-pickup, which is worse than either outcome on
#: its own: the caller is told nobody is coming, and then someone arrives.
#:
#: Forty-five is long enough to be answerable and is not silent — see
#: :data:`HOLDING_MESSAGE`, which fills it.
UNATTENDED_AFTER_SECONDS = float(
    os.environ.get("CLINIC_UNATTENDED_AFTER_SECONDS") or 45.0
)

#: How long before the caller is reassured that someone is still being fetched.
#:
#: Lengthening the deadline above without this would have traded a premature apology
#: for a longer silence, which is the very thing the watcher exists to prevent.
HOLDING_AFTER_SECONDS = float(os.environ.get("CLINIC_HOLDING_AFTER_SECONDS") or 12.0)

#: Said once while the caller is still waiting and someone may yet pick up.
#:
#: Deliberately does not promise anyone is coming — it may turn out nobody does, and
#: :data:`UNATTENDED_MESSAGE` then has to be able to follow it honestly.
HOLDING_MESSAGE = (
    "Thanks for holding — I'm still trying to get someone at the clinic for you. "
    "Please stay on the line a moment longer."
)

#: What the caller hears when nobody picked up. Says what is true, and gives them
#: somewhere to go: an apology with no route is just a longer dead end.
UNATTENDED_MESSAGE = (
    "I'm sorry — nobody at the clinic has been able to pick up just now. "
    "I've written your request down for them. I can take a message with your name "
    "and number so they can call you back, or you can ring the clinic during "
    "opening hours. Which would you prefer?"
)

#: A send callable bound to one caller's WebSocket.
Sender = Callable[[dict[str, Any]], Awaitable[None]]

#: Escalation reasons, in words. Keyed by
#: :class:`~clinic_front_desk.models.EscalationReason` values.
#:
#: Written from the doctor's point of view rather than the system's: she is deciding
#: in a second or two whether to pick this call up, and "patient_request" makes her
#: translate before she can decide.
REASON_LABELS: dict[str, str] = {
    "patient_request": "Asked to speak to a person",
    "patient_distress": "Caller is upset",
    "clinical_content": "Asked something clinical",
    "outside_admin_rules": "Outside what the agent can do",
}


@dataclass
class LiveCall:
    """One call in progress, and the handles needed to step into it."""

    session_id: str
    send: Sender
    started_at: str
    #: Set when the agent has asked for a human, so the doctor's console can show
    #: which calls are actually waiting rather than listing every call.
    needs_human: bool = False
    #: Why a human is wanted, taken from the escalation.
    reason: str = ""
    #: True once a human is on the call. While true the agent is silent.
    taken_over: bool = False
    #: Who the caller is, as far as the call has established.
    patient_name: str = ""
    callback_phone: str = ""
    #: Rolling transcript, so a doctor joining late can read what they missed.
    transcript: list[dict[str, str]] = field(default_factory=list)
    #: When a human was asked for, as a monotonic timestamp. Used to notice that
    #: nobody picked up.
    needs_human_since: float | None = None
    #: Set once the caller has been told nobody was able to pick up, so they are not
    #: told repeatedly.
    unattended_notified: bool = False
    #: Set once the caller has been reassured that someone is still being fetched.
    holding_notified: bool = False
    #: The doctor's own socket, once she has joined with a microphone.
    #:
    #: Present means a real two-way call: her voice reaches the caller and the
    #: caller's reaches her. Typing is still there for when she would rather not
    #: speak, or is somewhere she cannot.
    doctor_send: Sender | None = None

    def summary(self) -> dict[str, Any]:
        """The shape the doctor's console renders."""
        return {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "needs_human": self.needs_human,
            "reason": self.reason,
            # The raw reason is an EscalationReason value like "patient_request".
            # Correct as an API field and wrong on a screen a doctor reads at a
            # glance while deciding whether to pick the call up.
            "reason_label": REASON_LABELS.get(self.reason, self.reason),
            "taken_over": self.taken_over,
            "patient_name": self.patient_name,
            "callback_phone": self.callback_phone,
            "turns": len(self.transcript),
        }


class LiveCallRegistry:
    """The calls currently open, and the only way to speak into one.

    Deliberately tiny and synchronous apart from the sends. It is touched on the
    call path, so it holds no locks that a slow doctor console could contend on.
    """

    def __init__(self) -> None:
        self._calls: dict[str, LiveCall] = {}

    def register(self, session_id: str, send: Sender) -> LiveCall:
        call = LiveCall(
            session_id=session_id,
            send=send,
            started_at=datetime.now(UTC).isoformat(),
        )
        self._calls[session_id] = call
        return call

    def unregister(self, session_id: str) -> None:
        self._calls.pop(session_id, None)

    def get(self, session_id: str) -> LiveCall | None:
        return self._calls.get(session_id)

    def list_calls(self) -> list[dict[str, Any]]:
        """Waiting calls first — that is what the doctor is looking for."""
        calls = sorted(
            self._calls.values(),
            key=lambda c: (not c.needs_human, c.started_at),
        )
        return [call.summary() for call in calls]

    def record_turn(self, session_id: str, role: str, text: str) -> None:
        call = self._calls.get(session_id)
        if call is not None:
            call.transcript.append({"role": role, "text": text})

    def mark_needs_human(self, session_id: str, reason: str) -> None:
        call = self._calls.get(session_id)
        if call is not None:
            call.needs_human = True
            call.reason = reason
            if call.needs_human_since is None:
                call.needs_human_since = time.monotonic()

    def identify(
        self, session_id: str, *, name: str = "", phone: str = ""
    ) -> None:
        call = self._calls.get(session_id)
        if call is None:
            return
        if name:
            call.patient_name = name
        if phone:
            call.callback_phone = phone

    def transcript_of(self, session_id: str) -> list[dict[str, str]]:
        call = self._calls.get(session_id)
        return list(call.transcript) if call is not None else []

    def attach_doctor(self, session_id: str, send: Sender) -> LiveCall | None:
        """Join the doctor's own socket to a call so she can speak and listen."""
        call = self._calls.get(session_id)
        if call is None:
            return None
        call.doctor_send = send
        call.taken_over = True
        return call

    def detach_doctor(self, session_id: str) -> None:
        """The doctor's socket closed. The agent takes the call back.

        Deliberately hands the call back rather than leaving it silent: if her
        browser tab dies mid-call, the caller should get the agent again, not dead
        air.
        """
        call = self._calls.get(session_id)
        if call is not None:
            call.doctor_send = None
            call.taken_over = False


class LiveHandoverService:
    """Takes a call over, and speaks the doctor's words into it."""

    def __init__(
        self,
        registry: LiveCallRegistry,
        *,
        polly: Any | None = None,
        region: str | None = None,
        voice_id: str = DEFAULT_VOICE_ID,
        engine: str = DEFAULT_ENGINE,
    ) -> None:
        self._registry = registry
        self._polly = polly
        self._region = region
        self._voice_id = voice_id
        self._engine = engine

    def _client(self) -> Any:
        if self._polly is None:
            # Lazy so importing this costs nothing and needs no credentials.
            import boto3  # type: ignore[import-untyped]

            self._polly = boto3.client("polly", region_name=self._region)
        return self._polly

    async def take_over(self, session_id: str) -> bool:
        """Silence the agent and tell the caller a person is joining.

        The caller is told explicitly. Going quiet and then speaking in a different
        voice with no explanation is disorienting on a phone call.
        """
        call = self._registry.get(session_id)
        if call is None:
            return False
        call.taken_over = True
        await call.send(
            {
                "message_type": "human_joined",
                "session_id": session_id,
                "text": "A member of the clinic team has joined the call.",
            }
        )
        logger.info("live handover: a human took over %s", session_id)
        return True

    async def watch_unattended(
        self,
        session_id: str,
        *,
        after_seconds: float = UNATTENDED_AFTER_SECONDS,
        holding_seconds: float = HOLDING_AFTER_SECONDS,
        poll_seconds: float = 1.0,
    ) -> bool:
        """Come back to a caller nobody picked up, instead of leaving them in silence.

        Observed on a real call: the agent said it was connecting someone, the call
        was flagged on the console, and nobody was watching. The agent had stopped
        talking because it believed it had handed over, so the caller sat in dead air
        with no way forward. Saying nothing is worse than saying nobody is available.

        Returns ``True`` if the caller was told, ``False`` if the call ended, a human
        arrived, or nobody was ever asked for. Told at most once per call — repeating
        it would be its own kind of unhelpful.
        """
        while True:
            call = self._registry.get(session_id)
            if call is None:
                return False  # the caller hung up
            if call.taken_over:
                return False  # a person arrived; nothing to apologise for
            if call.unattended_notified:
                # Already told. Return rather than keep looping: a watcher that never
                # finishes leaves one live task per call for the life of the process.
                return False
            if call.needs_human_since is not None:
                waited = time.monotonic() - call.needs_human_since
                # Fill the wait before judging it. Without this the longer deadline
                # is just a longer silence.
                if (
                    not call.holding_notified
                    and holding_seconds < after_seconds
                    and waited >= holding_seconds
                ):
                    call.holding_notified = True
                    await self.speak(session_id, HOLDING_MESSAGE, role="agent")
                if waited >= after_seconds:
                    call.unattended_notified = True
                    # Spoken, not just written to the transcript. The caller is
                    # holding a phone, not watching a screen — a line of text they
                    # cannot hear leaves them in exactly the silence this exists to
                    # break.
                    await self.speak(session_id, UNATTENDED_MESSAGE, role="agent")
                    logger.info(
                        "live handover: nobody picked up %s after %.0fs",
                        session_id,
                        waited,
                    )
                    return True
            await asyncio.sleep(poll_seconds)

    async def relay_doctor_audio(
        self,
        session_id: str,
        audio: str,
        *,
        sample_rate: int = 16_000,
        channels: int = 1,
    ) -> bool:
        """Put the doctor's own voice on the caller's audio channel.

        Sent as ``agent_audio`` because that is the frame the caller's browser
        already knows how to play — there is no second audio path to build on the
        patient side, and inventing one would mean shipping a new client.

        The rate is passed through rather than assumed. The doctor's browser may hand
        us 16 kHz or whatever its hardware prefers, and the caller's client reads the
        rate off each frame, so forwarding the true value is both simpler and
        correct.
        """
        call = self._registry.get(session_id)
        if call is None or not audio:
            return False
        await call.send(
            {
                "message_type": "agent_audio",
                "session_id": session_id,
                "audio": audio,
                "format": "pcm",
                "sample_rate": sample_rate,
                "channels": channels,
            }
        )
        return True

    async def relay_caller_audio(
        self,
        session_id: str,
        audio: str,
        *,
        sample_rate: int = 16_000,
        channels: int = 1,
    ) -> bool:
        """Let the doctor hear the caller, when she is on the call.

        A no-op when no doctor is attached, which is the normal case — this runs on
        every inbound audio frame of every call, so it has to cost nothing when
        nobody is listening.
        """
        call = self._registry.get(session_id)
        if call is None or call.doctor_send is None or not audio:
            return False
        try:
            await call.doctor_send(
                {
                    "message_type": "caller_audio",
                    "session_id": session_id,
                    "audio": audio,
                    "format": "pcm",
                    "sample_rate": sample_rate,
                    "channels": channels,
                }
            )
        except Exception as exc:  # noqa: BLE001 - her socket dying must not end the call
            logger.warning("doctor socket send failed for %s: %s", session_id, exc)
            self._registry.detach_doctor(session_id)
            return False
        return True

    async def release(self, session_id: str) -> bool:
        """Hand the call back to the agent."""
        call = self._registry.get(session_id)
        if call is None:
            return False
        call.taken_over = False
        await call.send(
            {"message_type": "human_left", "session_id": session_id}
        )
        return True

    def synthesize(self, text: str) -> bytes:
        """The doctor's words as 16 kHz mono 16-bit PCM."""
        response = self._client().synthesize_speech(
            Text=text,
            OutputFormat="pcm",
            VoiceId=self._voice_id,
            Engine=self._engine,
            SampleRate=str(POLLY_SAMPLE_RATE),
        )
        audio: bytes = response["AudioStream"].read()
        return audio

    async def say(self, session_id: str, text: str) -> bool:
        """Speak ``text`` to the caller as the human on the call."""
        return await self.speak(session_id, text, role="human")

    async def speak(self, session_id: str, text: str, *, role: str = "human") -> bool:
        """Say ``text`` down the caller's audio channel, attributed to ``role``.

        Framed and paced rather than sent as one blob: the client plays frames as
        they arrive, and a single large buffer would arrive late and all at once.
        The transcript is written first so the doctor sees the line immediately, even
        if synthesis is slow.

        ``role`` exists because two different speakers use this. A doctor who has
        taken the call is ``human``; the agent apologising that nobody picked up is
        ``agent``. Labelling the apology as a human would misattribute it in the
        transcript the doctor later reads back.
        """
        call = self._registry.get(session_id)
        if call is None:
            return False

        self._registry.record_turn(session_id, role, text)
        await call.send(
            {
                "message_type": "transcript",
                "session_id": session_id,
                "role": role,
                "text": text,
            }
        )

        try:
            pcm = await asyncio.to_thread(self.synthesize, text)
        except Exception as exc:  # noqa: BLE001 - a failed voice must not drop the call
            logger.warning("Polly synthesis failed for %s: %s", session_id, exc)
            return False

        frame_bytes = FRAME_SAMPLES * 2
        for offset in range(0, len(pcm), frame_bytes):
            chunk = pcm[offset : offset + frame_bytes]
            await call.send(
                {
                    "message_type": "agent_audio",
                    "session_id": session_id,
                    "audio": base64.b64encode(chunk).decode(),
                    "format": "pcm",
                    # Labelled honestly at Polly's rate; the browser resamples.
                    "sample_rate": POLLY_SAMPLE_RATE,
                    "channels": 1,
                }
            )
        return True


__all__ = [
    "DEFAULT_ENGINE",
    "DEFAULT_VOICE_ID",
    "FRAME_SAMPLES",
    "POLLY_SAMPLE_RATE",
    "REASON_LABELS",
    "HOLDING_AFTER_SECONDS",
    "HOLDING_MESSAGE",
    "UNATTENDED_AFTER_SECONDS",
    "UNATTENDED_MESSAGE",
    "LiveCall",
    "LiveCallRegistry",
    "LiveHandoverService",
]
