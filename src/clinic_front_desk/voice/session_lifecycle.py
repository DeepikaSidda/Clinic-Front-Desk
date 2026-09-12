"""Call_Session lifecycle helpers — outcome persistence and empty-config intake.

Task 7.11 (Req 1.7, 11.5, 12.7). Two closely related pieces of Voice_Front_Desk
lifecycle behaviour that sit *between* the in-memory :class:`SessionContext`
(task 7.1) and the :class:`CallSessionStore` / :class:`ClinicKnowledgeBaseStore`
Data_Layer interfaces:

1. **Call_Session outcome persistence (Req 11.5, 12.7).** When a Call_Session
   ends, its terminal :class:`~clinic_front_desk.models.CallOutcome`
   (``booked``/``rescheduled``/``cancelled``/``waitlisted``/``escalated``/
   ``no_action``/``interrupted``) is persisted together with the patient-provided
   identifying information through :meth:`CallSessionStore.finalize`.
   :func:`finalize_session` reads both the outcome and the
   :class:`~clinic_front_desk.models.PatientRef` from the live
   :class:`SessionContext` so there is one source of truth.

2. **Empty-config voice behaviour (Req 1.7).** *While no clinic hours and no
   offered services are configured*, the Voice_Front_Desk must tell the patient
   the clinic is not yet accepting calls and offer to take a message — for both
   booking and FAQ requests. :func:`check_intake_availability` is the pure helper
   that inspects the :class:`~clinic_front_desk.models.ClinicKnowledgeBase` state
   and returns the appropriate :data:`IntakeSignal`; :func:`evaluate_intake`
   is a convenience that reads the config through the store first.

Both helpers are side-effect-light and return values (a ``StoreResult`` or an
``IntakeSignal``) rather than performing voice output, mirroring the
:class:`~clinic_front_desk.voice.turn_controller.TurnController` style so they
can be unit tested without a real audio stream.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Union

from clinic_front_desk.data_layer.interfaces import (
    CallSessionStore,
    ClinicKnowledgeBaseStore,
    is_err,
)
from clinic_front_desk.models import (
    CallOutcome,
    CallSession,
    ClinicKnowledgeBase,
    ISODateTime,
    StoreResult,
)

from .session_context import SessionContext


def _utc_now_iso() -> ISODateTime:
    """Current UTC time as an ISO-8601 string (the default call-end clock)."""
    return datetime.now(UTC).isoformat()

# ---------------------------------------------------------------------------
# 1. Call_Session outcome persistence (Req 11.5, 12.7).
# ---------------------------------------------------------------------------


def finalize_session(
    store: CallSessionStore,
    context: SessionContext,
    outcome: CallOutcome | None = None,
    *,
    transcript: str | None = None,
    recording_uri: str | None = None,
    ended_at: ISODateTime | None = None,
    clock: Callable[[], ISODateTime] = _utc_now_iso,
) -> StoreResult[CallSession]:
    """Persist the Call_Session outcome and patient identity on end (Req 11.5, 12.7).

    Writes the terminal :class:`~clinic_front_desk.models.CallOutcome` and the
    patient-provided identifying information (as the context's
    :class:`~clinic_front_desk.models.PatientRef`) through
    :meth:`CallSessionStore.finalize`.

    The outcome is resolved from, in order of precedence:

    1. the explicit ``outcome`` argument (e.g. the ``interrupted`` outcome the
       :class:`~clinic_front_desk.voice.turn_controller.TurnController` signals
       on voice-layer loss, Req 12.7);
    2. the outcome already recorded on the ``context`` via
       :meth:`SessionContext.record_outcome`;
    3. :attr:`~clinic_front_desk.models.CallOutcome.NO_ACTION` when a session
       ends without any task having been completed (Req 11.5).

    The resolved outcome is written back onto the ``context`` so the in-memory
    session and the persisted record agree. The store's ``Result`` is returned
    unchanged so the caller can branch on a persistence failure (Req 16.6): a
    failed ``finalize`` leaves prior sessions untouched.

    Args:
        transcript: The rendered call transcript, when the call was transcribed.
        recording_uri: Where the call audio was stored, when it was recorded.
            Both are passed straight through; ``None`` leaves any existing stored
            value alone, so a failed render or upload never erases one.
        ended_at: Explicit call-end timestamp; defaults to ``clock()``.
        clock: Injectable source of the end timestamp. This function is the single
            place the call-end time is decided — the stores deliberately do not
            default it, because two implementations reading their own clocks would
            disagree and break storage-swap equivalence (Req 16.5).
    """
    resolved = outcome if outcome is not None else context.outcome
    if resolved is None:
        resolved = CallOutcome.NO_ACTION

    # Keep the in-memory context and the persisted record in agreement.
    context.record_outcome(resolved)

    return store.finalize(
        context.session_id,
        resolved,
        context.patient_ref,
        ended_at=ended_at if ended_at is not None else clock(),
        transcript=transcript,
        recording_uri=recording_uri,
    )


# ---------------------------------------------------------------------------
# 2. Empty-config intake behaviour (Req 1.7).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AcceptingCalls:
    """The clinic is configured enough to handle booking/FAQ requests (Req 1.7).

    Returned when at least clinic hours *or* at least one offered service is
    configured; the orchestrator proceeds with normal handling.
    """

    kind: Literal["accepting_calls"] = "accepting_calls"


@dataclass(frozen=True)
class NotAcceptingCalls:
    """The clinic is not yet accepting calls; take a message instead (Req 1.7).

    Returned while *no* clinic hours **and** *no* offered services are
    configured. The orchestrator must tell the patient the clinic is not yet
    accepting calls and offer to take a message (``offer_message`` is ``True``).
    """

    offer_message: bool = True
    kind: Literal["not_accepting_calls"] = "not_accepting_calls"


#: What the intake check tells the orchestrator to do for a booking/FAQ request.
IntakeSignal = Union[AcceptingCalls, NotAcceptingCalls]


def _has_configured_hours(kb: ClinicKnowledgeBase) -> bool:
    """``True`` if at least one weekday has non-``None`` opening hours (Req 1.7)."""
    return any(day is not None for day in kb.hours.values())


def _has_offered_services(kb: ClinicKnowledgeBase) -> bool:
    """``True`` if at least one service is offered (Req 1.7)."""
    return len(kb.services) > 0


def check_intake_availability(kb: ClinicKnowledgeBase | None) -> IntakeSignal:
    """Decide whether the clinic can take booking/FAQ requests yet (Req 1.7).

    Returns :class:`NotAcceptingCalls` **iff** the clinic is unconfigured — no
    stored configuration at all (``kb is None``, the empty-init state, Req 16.4)
    or a configuration with neither clinic hours nor any offered service.
    Otherwise returns :class:`AcceptingCalls`.

    The condition mirrors Req 1.7 exactly: the clinic is "not yet accepting
    calls" only *while no clinic hours **and** no offered services are
    configured*; configuring either one flips the clinic into the accepting
    state.
    """
    if kb is None:
        return NotAcceptingCalls()
    if not _has_configured_hours(kb) and not _has_offered_services(kb):
        return NotAcceptingCalls()
    return AcceptingCalls()


def evaluate_intake(kb_store: ClinicKnowledgeBaseStore) -> IntakeSignal:
    """Read the clinic config through the store and evaluate intake (Req 1.7).

    Convenience wrapper over :func:`check_intake_availability` that first reads
    the singleton :class:`~clinic_front_desk.models.ClinicKnowledgeBase` from the
    store. If the config cannot be read (a store failure), the clinic's
    readiness cannot be confirmed, so the conservative :class:`NotAcceptingCalls`
    signal is returned — the patient is offered a message rather than being told
    the clinic is ready when it may not be.
    """
    result = kb_store.get()
    if is_err(result):
        return NotAcceptingCalls()
    return check_intake_availability(result.value)


__all__ = [
    "finalize_session",
    "AcceptingCalls",
    "NotAcceptingCalls",
    "IntakeSignal",
    "check_intake_availability",
    "evaluate_intake",
]
