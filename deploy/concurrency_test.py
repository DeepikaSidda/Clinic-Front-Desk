"""Can several people call the agent at the same time?

Judging means simultaneous callers, and there are three separate ways that could
fail — worth telling apart rather than discovering as one vague "it broke":

1. **Bedrock concurrency.** Each call opens its own Nova Sonic bidirectional
   stream. If the account has a low concurrent-stream quota, the second or third
   caller fails at ``session.start()`` while the first is fine.
2. **Sessions bleeding into each other.** Every call must get its own session id,
   its own agent and its own transcript. Two callers sharing state would be far
   worse than a failed call: one caller's details could reach another.
3. **The instance running out of room.** A t4g.small has 2 GB, and audio buffers
   are the hungry part.

Each connection opens, waits for ``session_started``, holds briefly, then hangs up
cleanly. Timings are reported per call so a slow third caller is visible even when
nothing errors.

    python deploy/concurrency_test.py --callers 3
    python deploy/concurrency_test.py --callers 5 --hold 8
    python deploy/concurrency_test.py --callers 3 --local

These are real Bedrock streams and cost real money, so keep the numbers small.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any

import websockets

PUBLIC_WS = "wss://d21u7cmj563imv.cloudfront.net/ws"
LOCAL_WS = "ws://127.0.0.1:8080/ws"


@dataclass
class CallResult:
    index: int
    session_id: str | None = None
    connected_at: float | None = None
    started_at: float | None = None
    clinic_card: bool = False
    error: str | None = None
    messages: list[str] = field(default_factory=list)

    @property
    def start_seconds(self) -> float | None:
        if self.connected_at is None or self.started_at is None:
            return None
        return self.started_at - self.connected_at

    @property
    def ok(self) -> bool:
        return self.error is None and self.session_id is not None


#: One 32 ms frame of 16 kHz mono silence, base64'd once rather than per send.
_SILENT_FRAME = base64.b64encode(bytes(1024)).decode("ascii")


async def hold_the_line(socket: Any, seconds: float) -> None:
    """Stay on the call the way a browser does: streaming, not silent on the wire.

    This used to be a bare ``asyncio.sleep``, which models something no real client
    does. A browser's AudioWorklet posts a frame roughly every 32 ms for the whole
    call whether or not anyone is speaking, and the server now relies on that: a
    socket with no frames for a minute is treated as dead and hung up. A sleeping
    test would have started failing above ``--hold 60`` and looked like a
    concurrency limit rather than a test that stopped resembling a caller.

    Sending silence also makes the load honest, since per-frame work on the server —
    decode, record, relay — is real work that a sleeping socket never asks for.
    """
    frame = json.dumps(
        {
            "message_type": "user_audio",
            "audio": _SILENT_FRAME,
            "format": "pcm",
            "sample_rate": 16000,
            "channels": 1,
        }
    )
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        try:
            await socket.send(frame)
        except Exception:  # noqa: BLE001 - the hang-up below reports it
            return
        await asyncio.sleep(0.032)


async def one_call(index: int, url: str, hold: float, timeout: float) -> CallResult:
    """One caller: connect, wait to be greeted, hold, hang up."""
    result = CallResult(index=index)
    began = time.perf_counter()
    try:
        async with websockets.connect(url, open_timeout=timeout, max_size=None) as socket:
            result.connected_at = time.perf_counter()

            deadline = time.perf_counter() + timeout
            while time.perf_counter() < deadline:
                remaining = deadline - time.perf_counter()
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
                except (TimeoutError, asyncio.TimeoutError):
                    break
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                kind = str(message.get("message_type", ""))
                result.messages.append(kind)
                if kind == "session_started":
                    result.session_id = str(message.get("session_id") or "")
                    result.started_at = time.perf_counter()
                elif kind == "clinic_card":
                    result.clinic_card = True
                if result.session_id and result.clinic_card:
                    break

            if result.session_id is None:
                result.error = "never received session_started"
            else:
                await hold_the_line(socket, hold)

            try:
                await socket.send(json.dumps({"message_type": "end_session"}))
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        result.error = f"{type(exc).__name__}: {exc}"
        if result.connected_at is None:
            result.connected_at = began
    return result


async def run(callers: int, url: str, hold: float, timeout: float) -> int:
    print(f"  {callers} simultaneous callers -> {url}")
    print(f"  holding each call {hold:.0f}s\n")

    began = time.perf_counter()
    results = await asyncio.gather(
        *(one_call(i + 1, url, hold, timeout) for i in range(callers))
    )
    elapsed = time.perf_counter() - began

    for result in sorted(results, key=lambda r: r.index):
        start = result.start_seconds
        timing = f"{start:5.2f}s" if start is not None else "    -"
        if result.ok:
            card = "card" if result.clinic_card else "NO CARD"
            print(
                f"  ok    caller {result.index}  start {timing}  "
                f"{card}  session {(result.session_id or '')[:12]}"
            )
        else:
            print(f"  FAIL  caller {result.index}  start {timing}  {result.error}")

    succeeded = [r for r in results if r.ok]
    session_ids = {r.session_id for r in succeeded}
    starts = [r.start_seconds for r in succeeded if r.start_seconds is not None]

    print()
    print(f"  succeeded        {len(succeeded)}/{callers}")
    print(f"  distinct ids     {len(session_ids)}")
    if starts:
        print(
            f"  time to greeting min {min(starts):.2f}s  "
            f"mean {sum(starts) / len(starts):.2f}s  max {max(starts):.2f}s"
        )
    print(f"  wall clock       {elapsed:.1f}s")
    print()

    problems: list[str] = []
    if len(succeeded) != callers:
        problems.append(f"{callers - len(succeeded)} of {callers} calls failed")
    # The one that would matter most: two callers sharing a session means one
    # caller's details could surface in another's call.
    if len(session_ids) != len(succeeded):
        problems.append("two calls shared a session id")
    missing_card = [r.index for r in succeeded if not r.clinic_card]
    if missing_card:
        problems.append(f"no clinic card for caller(s) {missing_card}")

    if problems:
        for problem in problems:
            print(f"  PROBLEM  {problem}")
        return 1
    print(f"  {callers} callers were served at once, each with its own session.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--callers", type=int, default=3)
    parser.add_argument("--hold", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--local", action="store_true", help=f"use {LOCAL_WS}")
    parser.add_argument("--url", default=None)
    args = parser.parse_args()

    url = args.url or (LOCAL_WS if args.local else PUBLIC_WS)
    raise SystemExit(asyncio.run(run(args.callers, url, args.hold, args.timeout)))


if __name__ == "__main__":
    main()
