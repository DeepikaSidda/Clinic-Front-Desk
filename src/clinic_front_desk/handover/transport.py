"""Delivering a human handover, as opposed to merely recording one.

``flag_for_human`` writes an :class:`~clinic_front_desk.models.Escalation` and the
dashboard shows it. That is a record, not a handover: nothing reaches a person
until something carries it to them. This module is that boundary.

Two rules are baked into the interface rather than left to each implementation.

**Record first, deliver second.** The escalation is persisted before any transport
is attempted, so a handover is never lost because a network was down or a contact
centre rejected the request. A delivery failure degrades the promise made to the
caller; it must never lose the fact that they asked.

**Delivery is reported, never assumed.** Every attempt returns an outcome the
agent can read, because the one thing it must not do is tell a caller that
somebody will ring them back when nothing was actually dispatched. Silently
claiming a callback is the failure mode the whole system exists to avoid, and it
would be a strange place to give up on it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from clinic_front_desk.models import Escalation


@dataclass(frozen=True)
class HandoverRequest:
    """Everything a human needs to pick a call up without starting over.

    Deliberately a flat, transport-agnostic bag of strings. Amazon Connect carries
    these as contact attributes, an email transport would render them as a body,
    and both get the same fields — so what the human receives does not depend on
    which transport happened to be configured.
    """

    escalation: Escalation
    #: Short, human-readable summary — the subject line of the handover.
    summary: str
    #: The caller's name, when the call got that far.
    patient_name: str | None = None
    #: Where to call them back. The single most important field here: without it
    #: a handover is a notification that somebody wanted help, and no way to help.
    callback_phone: str | None = None
    #: The last few turns, so the human starts informed rather than asking the
    #: caller to repeat what they already said to a machine.
    transcript_tail: str = ""
    #: Anything transport-specific the caller wants passed through.
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class HandoverDelivered:
    """A human was actually reached, or a work item was actually created."""

    #: The transport's own identifier — a Connect contact id, a message id. Written
    #: back onto the escalation so the dashboard can follow what happened to it
    #: rather than showing a row that only ever says "open".
    reference: str
    #: What the agent may truthfully say to the caller.
    spoken_detail: str = "I've passed this to a member of our team."
    kind: str = "delivered"


@dataclass(frozen=True)
class HandoverFailed:
    """Nothing was dispatched. The escalation is still recorded."""

    detail: str
    #: What the agent must say instead. Never a promised callback.
    spoken_detail: str = (
        "I've recorded this for the clinic, but I could not reach anyone right now. "
        "Would you like to leave a message, or try calling back during clinic hours?"
    )
    kind: str = "failed"


#: Outcome of an attempted delivery.
HandoverOutcome = HandoverDelivered | HandoverFailed


class HandoverTransport(ABC):
    """Carries a recorded escalation to a human.

    Implementations must not raise. A transport that throws on a network blip
    would propagate out of the escalation tool and turn "we could not reach a
    person" into "the call failed", which is strictly worse for the caller.
    Failures come back as :class:`HandoverFailed`.
    """

    @abstractmethod
    def deliver(self, request: HandoverRequest) -> HandoverOutcome:
        """Attempt delivery. Never raises; returns the outcome either way."""
        raise NotImplementedError

    @property
    @abstractmethod
    def name(self) -> str:
        """Short transport name, for logs and for the dashboard."""
        raise NotImplementedError


__all__ = [
    "HandoverDelivered",
    "HandoverFailed",
    "HandoverOutcome",
    "HandoverRequest",
    "HandoverTransport",
]
