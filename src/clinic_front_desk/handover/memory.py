"""In-memory handover transports, for tests and for local runs.

Held to the same contract as the Amazon Connect transport, so a test that proves
"the escalation is recorded even when delivery fails" proves it about the real one
too.
"""

from __future__ import annotations

from clinic_front_desk.handover.transport import (
    HandoverDelivered,
    HandoverFailed,
    HandoverOutcome,
    HandoverRequest,
    HandoverTransport,
)


class MemoryHandoverTransport(HandoverTransport):
    """Records what would have been delivered, and says it succeeded."""

    def __init__(self, *, reference_prefix: str = "memory-contact") -> None:
        self.delivered: list[HandoverRequest] = []
        self._prefix = reference_prefix

    @property
    def name(self) -> str:
        return "memory"

    def deliver(self, request: HandoverRequest) -> HandoverOutcome:
        self.delivered.append(request)
        return HandoverDelivered(
            reference=f"{self._prefix}-{len(self.delivered)}",
            spoken_detail="I've passed this to a member of our team.",
        )


class FailingHandoverTransport(HandoverTransport):
    """Always fails, without raising.

    Exists because the interesting property is not that delivery works — it is that
    a *failed* delivery still leaves the escalation recorded and makes the agent say
    something honest instead of promising a callback nobody will make.
    """

    def __init__(self, detail: str = "transport unavailable") -> None:
        self.attempts: list[HandoverRequest] = []
        self._detail = detail

    @property
    def name(self) -> str:
        return "failing"

    def deliver(self, request: HandoverRequest) -> HandoverOutcome:
        self.attempts.append(request)
        return HandoverFailed(detail=self._detail)


class RaisingHandoverTransport(HandoverTransport):
    """Raises on delivery, to prove the caller survives a badly behaved transport.

    The contract says a transport must not raise. This one violates it deliberately,
    so the code around it is shown to hold a misbehaving implementation at arm's
    length rather than letting an exception end the call.
    """

    @property
    def name(self) -> str:
        return "raising"

    def deliver(self, request: HandoverRequest) -> HandoverOutcome:
        raise RuntimeError("this transport is broken")


__all__ = [
    "FailingHandoverTransport",
    "MemoryHandoverTransport",
    "RaisingHandoverTransport",
]
