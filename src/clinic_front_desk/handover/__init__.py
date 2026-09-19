"""Delivering a human handover, not just recording one.

``flag_for_human`` persists an Escalation; that is a record. This package carries
it to a person. The split matters because the two can fail independently, and the
caller must never be told somebody will ring them back when nothing was dispatched.

- :mod:`~clinic_front_desk.handover.transport` — the interface and its outcomes.
- :mod:`~clinic_front_desk.handover.connect` — Amazon Connect, as a queued task or
  an outbound call.
- :mod:`~clinic_front_desk.handover.memory` — fakes, including ones that fail and
  raise on purpose.
"""

from clinic_front_desk.handover.connect import ConnectHandoverTransport
from clinic_front_desk.handover.memory import (
    FailingHandoverTransport,
    MemoryHandoverTransport,
    RaisingHandoverTransport,
)
from clinic_front_desk.handover.transport import (
    HandoverDelivered,
    HandoverFailed,
    HandoverOutcome,
    HandoverRequest,
    HandoverTransport,
)

__all__ = [
    "ConnectHandoverTransport",
    "FailingHandoverTransport",
    "HandoverDelivered",
    "HandoverFailed",
    "HandoverOutcome",
    "HandoverRequest",
    "HandoverTransport",
    "MemoryHandoverTransport",
    "RaisingHandoverTransport",
]
