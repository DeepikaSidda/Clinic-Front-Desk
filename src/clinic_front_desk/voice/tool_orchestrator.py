"""``ToolOrchestrator`` — multi-step tool chaining and confirm-before-mutate.

Task 7.5 (Req 11.2, 11.3, 4.5, 5.6). See design "Voice_Front_Desk"
sub-components: *"``ToolOrchestrator`` — Chains tool calls; on mid-chain failure
retains context and offers to take a message (Req 11.3)."*

Like the other Voice_Front_Desk components (``TurnController``,
``GuardrailPolicy``), the orchestrator is a **deterministic, side-effect-free
coordinator**: it decides *what to do next* and returns a discriminated action
rather than owning any audio/store side effects itself. The Strands tools it
chains are supplied as plain thunks (``Callable[[], ToolResult[T]]``) already
bound to their Data_Layer stores by the caller, which keeps the orchestrator
decoupled from concrete stores and trivially testable with fakes.

Responsibilities:

- **Chain tools so each output feeds the next (Req 11.2).** :meth:`run_chain`
  runs an ordered list of :class:`ChainStep`\\ s. Each step receives a
  :class:`ChainContext` exposing the live :class:`SessionContext` and the
  outputs of every previously completed step, so a step can consume its
  predecessor's result (e.g. ``lookup_patient`` → ``check_availability`` →
  ``book_appointment``). Every successful step's value is threaded forward.
- **Mid-chain failure retention (Req 11.3).** If any step returns ``Err`` the
  chain stops immediately, the facts gathered so far are *retained* on the
  :class:`SessionContext` (they are never cleared), and the orchestrator returns
  an :class:`OfferToTakeMessage` action so the voice layer can tell the patient
  the request could not be completed and offer to take a message.
- **Confirm-before-mutate for reschedule/cancel (Req 4.5, 5.6).**
  :meth:`cancel` and :meth:`reschedule` never invoke their mutating tool until
  the patient has confirmed. A declined confirmation returns
  :class:`MutationDeclined` *without calling the tool*, so a declined
  cancellation leaves the appointment unchanged (Req 5.6). A reschedule offered
  no alternative slots returns :class:`NoAlternativeSlots` *without calling the
  tool*, so the original appointment is left unchanged and the patient is
  offered the waitlist (Req 4.5). Because no store write is issued on these
  paths, state is unchanged by construction.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Union

from clinic_front_desk.models import CallOutcome, ToolError, ToolResult, is_err

from clinic_front_desk.voice.session_context import SessionContext

# ---------------------------------------------------------------------------
# Chain plumbing
# ---------------------------------------------------------------------------

#: A tool thunk: a zero-argument callable already bound to its Data_Layer
#: store(s) that returns a discriminated :data:`~clinic_front_desk.models.ToolResult`.
ToolThunk = Callable[[], "ToolResult[Any]"]


@dataclass
class ChainContext:
    """The state handed to each :class:`ChainStep` while a chain runs (Req 11.2).

    Exposes the live :class:`SessionContext` (so a step can read facts gathered
    earlier in the call and record new ones) and ``outputs`` — the ``Ok`` value
    of every previously completed step keyed by step name — so each tool's
    output can inform the next.
    """

    session: SessionContext
    outputs: dict[str, Any] = field(default_factory=dict)

    @property
    def last(self) -> Any:
        """The value produced by the most recently completed step (or ``None``)."""
        if not self.outputs:
            return None
        # dicts preserve insertion order, so the last-inserted value is newest.
        last_key = next(reversed(self.outputs))
        return self.outputs[last_key]

    def output(self, step_name: str) -> Any:
        """Return a named earlier step's output value (``None`` if absent)."""
        return self.outputs.get(step_name)


@dataclass(frozen=True)
class ChainStep:
    """One step in a chained multi-tool task (Req 11.2).

    ``run`` receives the :class:`ChainContext` and returns a
    :data:`~clinic_front_desk.models.ToolResult`. On ``Ok`` its value is threaded
    forward under ``name``; on ``Err`` the chain stops and the orchestrator
    offers to take a message (Req 11.3).
    """

    name: str
    run: Callable[[ChainContext], "ToolResult[Any]"]


# ---------------------------------------------------------------------------
# Actions — a discriminated union describing the orchestrator's decision.
# Mirrors the ``ToolError`` / ``TurnAction`` union style: one frozen dataclass
# per ``kind``.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChainCompleted:
    """Every step in the chain completed successfully (Req 11.2).

    ``outputs`` maps each step name to its ``Ok`` value; ``value`` is the final
    step's value for convenience.
    """

    outputs: dict[str, Any]
    value: Any
    kind: Literal["chain_completed"] = "chain_completed"


@dataclass(frozen=True)
class OfferToTakeMessage:
    """A chained task could not be completed; offer to take a message (Req 11.3).

    Emitted when a step fails mid-chain (or a confirmed mutation fails to
    persist). ``retained_facts`` is a snapshot of the Call_Session facts
    gathered so far — proof they were *not* discarded — which the voice layer
    reuses without re-asking the patient (Req 11.3, 11.4).
    """

    failed_step: str
    error: ToolError
    retained_facts: dict[str, Any]
    kind: Literal["offer_to_take_message"] = "offer_to_take_message"


@dataclass(frozen=True)
class MutationCommitted:
    """A confirmed reschedule/cancel persisted successfully.

    ``outcome`` is the Call_Session outcome recorded on the
    :class:`SessionContext` (``rescheduled`` or ``cancelled``).
    """

    value: Any
    outcome: CallOutcome
    kind: Literal["mutation_committed"] = "mutation_committed"


@dataclass(frozen=True)
class MutationDeclined:
    """The patient declined to confirm a mutation, so no action was taken.

    Returned by :meth:`cancel` when the patient declines the cancellation
    (Req 5.6) and by :meth:`reschedule` when the patient declines the new slot.
    The mutating tool is never invoked on this path, so the appointment is left
    unchanged.
    """

    action: Literal["cancel", "reschedule"]
    kind: Literal["mutation_declined"] = "mutation_declined"


@dataclass(frozen=True)
class NoAlternativeSlots:
    """A reschedule found no alternative slots (Req 4.5).

    The original appointment is left unchanged (the reschedule tool is never
    invoked) and the patient is offered the waitlist.
    """

    offer_waitlist: bool = True
    kind: Literal["no_alternative_slots"] = "no_alternative_slots"


#: Result of a chained task run.
ChainOutcome = Union[ChainCompleted, OfferToTakeMessage]

#: Result of a confirm-before-mutate reschedule/cancel.
MutationOutcome = Union[
    MutationCommitted, MutationDeclined, NoAlternativeSlots, OfferToTakeMessage
]


# ---------------------------------------------------------------------------
# ToolOrchestrator
# ---------------------------------------------------------------------------


class ToolOrchestrator:
    """Chains Strands tools and enforces confirm-before-mutate (Req 11.2, 11.3, 4.5, 5.6).

    A single instance coordinates the tool calls for one active Call_Session and
    carries facts across steps through the injected :class:`SessionContext`. It
    performs no side effects of its own: tools are supplied as bound thunks and
    every method returns a discriminated action for the voice layer to render.
    """

    def __init__(self, session: SessionContext) -> None:
        self.session = session

    # ------------------------------------------------------------- chaining
    def run_chain(self, steps: Sequence[ChainStep]) -> ChainOutcome:
        """Run an ordered chain of tool steps, threading each output forward (Req 11.2).

        Each step receives a :class:`ChainContext` carrying the session and the
        outputs of all previously completed steps, so a completed tool's output
        informs the next tool invocation until the task completes.

        Returns:
            :class:`ChainCompleted` when every step succeeds. On the first step
            that returns ``Err`` the chain stops, the facts gathered so far are
            retained on the session (never cleared), and
            :class:`OfferToTakeMessage` is returned so the patient can be told
            the request could not be completed and offered a message (Req 11.3).
        """
        context = ChainContext(session=self.session)
        last_value: Any = None
        for step in steps:
            result = step.run(context)
            if is_err(result):
                # Mid-chain failure: retain everything gathered so far (the
                # session is left intact) and offer to take a message (Req 11.3).
                return OfferToTakeMessage(
                    failed_step=step.name,
                    error=result.error,
                    retained_facts=self.session.known_facts(),
                )
            last_value = result.value
            context.outputs[step.name] = last_value

        return ChainCompleted(outputs=dict(context.outputs), value=last_value)

    # ------------------------------------------------ confirm-before-mutate
    def cancel(
        self,
        *,
        confirmed: bool,
        cancel_tool: ToolThunk,
    ) -> MutationOutcome:
        """Cancel an appointment only after the patient confirms (Req 5.6).

        Args:
            confirmed: Whether the patient confirmed the cancellation. When
                ``False`` the ``cancel_tool`` is **not** invoked, so the
                appointment is retained and no cancellation action is taken
                (Req 5.6).
            cancel_tool: A bound thunk invoking the ``cancel`` Strands tool.

        Returns:
            :class:`MutationDeclined` when unconfirmed (state unchanged, Req 5.6);
            :class:`MutationCommitted` (outcome ``cancelled``) on a successful
            persist; :class:`OfferToTakeMessage` if the confirmed cancel fails to
            persist — the appointment is left unchanged and a message is offered
            (Req 5.8, 11.3).
        """
        if not confirmed:
            # Declined confirmation: take no cancellation action (Req 5.6).
            return MutationDeclined(action="cancel")

        result = cancel_tool()
        if is_err(result):
            return OfferToTakeMessage(
                failed_step="cancel",
                error=result.error,
                retained_facts=self.session.known_facts(),
            )

        self.session.record_outcome(CallOutcome.CANCELLED)
        return MutationCommitted(value=result.value, outcome=CallOutcome.CANCELLED)

    def reschedule(
        self,
        *,
        alternative_slots: Sequence[Any],
        confirmed: bool,
        reschedule_tool: ToolThunk,
    ) -> MutationOutcome:
        """Reschedule an appointment only after confirmation, guarding no-slots (Req 4.5).

        Args:
            alternative_slots: The alternative open slots returned by
                ``check_availability`` for the located appointment's service.
                When empty the ``reschedule_tool`` is **not** invoked, so the
                original appointment is left unchanged and the patient is offered
                the waitlist (Req 4.5).
            confirmed: Whether the patient confirmed the selected new slot. When
                ``False`` the ``reschedule_tool`` is **not** invoked, leaving the
                appointment unchanged.
            reschedule_tool: A bound thunk invoking the ``reschedule`` Strands
                tool.

        Returns:
            :class:`NoAlternativeSlots` when there are no slots to offer (state
            unchanged, Req 4.5); :class:`MutationDeclined` when the patient
            declines (state unchanged); :class:`MutationCommitted` (outcome
            ``rescheduled``) on a successful persist; :class:`OfferToTakeMessage`
            if the confirmed reschedule fails to persist — the original
            appointment is left unchanged and a message is offered (Req 4.9,
            11.3).
        """
        if not alternative_slots:
            # No alternative slots: leave the existing appointment unchanged and
            # offer the waitlist (Req 4.5). The reschedule tool is never called.
            return NoAlternativeSlots()

        if not confirmed:
            # Declined confirmation: leave the appointment unchanged.
            return MutationDeclined(action="reschedule")

        result = reschedule_tool()
        if is_err(result):
            return OfferToTakeMessage(
                failed_step="reschedule",
                error=result.error,
                retained_facts=self.session.known_facts(),
            )

        self.session.record_outcome(CallOutcome.RESCHEDULED)
        return MutationCommitted(value=result.value, outcome=CallOutcome.RESCHEDULED)


__all__ = [
    "ToolThunk",
    "ChainContext",
    "ChainStep",
    "ChainCompleted",
    "OfferToTakeMessage",
    "MutationCommitted",
    "MutationDeclined",
    "NoAlternativeSlots",
    "ChainOutcome",
    "MutationOutcome",
    "ToolOrchestrator",
]
