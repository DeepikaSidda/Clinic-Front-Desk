"""Decision approve/dismiss execution service (task 12.2, Req 14.3–14.6, 8.2–8.5).

The dashboard's Decisions feed offers an *approve* and a *dismiss* control on
every open Decision (Req 14.2). This module implements the server-side execution
those controls trigger. It is deliberately isolated from the BFF and the
real-time channel: it depends only on the Data_Layer store *interfaces*
(``DecisionStore``, ``AppointmentStore``, ``WaitlistStore``) and the existing
``fill_gap_from_waitlist`` tool, so approving/dismissing reads and writes go
through the Data_Layer exactly like every other agent/dashboard operation
(Req 16.1). The ``ChangeEvent`` fan-out that removes a resolved Decision from the
open feed within 2 s (Req 14.5) is driven by ``DecisionStore.set_status`` — this
service just calls it; the store emits the event.

Locating a Decision by id
--------------------------
``DecisionStore`` intentionally exposes no get-by-id operation (its read surface
is ``list_open`` + ``find_open_by_finding_key``). Both approve and dismiss act on
an *open* Decision, so we locate the target by scanning ``list_open()`` for a
matching id. This keeps the service reading purely through the interface and
naturally rejects any id that is not currently open (already resolved, or never
existed) with a ``NOT_FOUND`` outcome.

Approve semantics (Req 14.3, 14.5, 14.6)
----------------------------------------
On approve we execute the Decision's associated action *first*, then record the
approval — the ordering is what makes the failure path correct:

- ``gap_fill`` Decisions carry ``action_payload["slot_id"]``; approving runs
  ``fill_gap_from_waitlist`` for that slot, which books the earliest matching
  waitlisted patient and removes their entry (Req 8.2, 8.3, 8.4). ``gap_fill`` is
  the only Decision kind with an automated Data_Layer action in this system.
- Every other Decision kind (no-show trend, schedule gap, unmet/unoffered-service
  demand) is advisory — its recommended action is an operational change the
  doctor makes off-system — so approving simply records the approval.

If the action fails to persist, we leave the Decision **open** and return an
``ACTION_FAILED`` outcome carrying an error indication (Req 14.6, 8.5). We do
*not* transition the stored status: ``DecisionStore.list_open`` returns only
``open`` Decisions, so retaining the ``open`` status is precisely what keeps the
Decision in the feed as the requirement demands, while the returned result gives
the dashboard the error indication to display. (The ``fill_gap_from_waitlist``
tool already compensates its own partial writes, so no slot/entry change leaks —
"no partial effect".) Only after the action succeeds do we call
``set_status(APPROVED)``, which drops the Decision from the open feed.

Dismiss semantics (Req 14.4)
----------------------------
Dismiss records ``DISMISSED`` through the Data_Layer and executes no action.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Callable

from clinic_front_desk.data_layer.interfaces import (
    AppointmentStore,
    DecisionStore,
    WaitlistStore,
)
from clinic_front_desk.models import (
    Decision,
    DecisionKind,
    DecisionStatus,
    is_err,
)
from clinic_front_desk.tools.waitlist import GapFillResult, fill_gap_from_waitlist

#: A clock returning the current time as an ISO-8601 UTC string. Injectable so
#: tests can pin the ``resolved_at`` timestamp deterministically.
Clock = Callable[[], str]

#: An id generator returning a fresh unique string, threaded through to the
#: gap-fill tool for the created appointment id. Injectable for tests.
IdGen = Callable[[], str]


def _default_clock() -> str:
    return datetime.now(UTC).isoformat()


class DecisionActionOutcome(StrEnum):
    """The outcome of an approve/dismiss request, for the dashboard to render.

    - ``APPROVED`` — approval recorded and the associated action (if any)
      completed; the Decision has left the open feed (Req 14.3, 14.5).
    - ``DISMISSED`` — dismissal recorded, no action executed (Req 14.4).
    - ``ACTION_FAILED`` — the approved Decision's action failed to persist; the
      Decision stays open with no partial effect and an ``error`` indication
      (Req 14.6, 8.5).
    - ``NOT_FOUND`` — no open Decision has the given id (already resolved or
      never existed).
    - ``STORE_ERROR`` — a Data_Layer read/write failed while resolving the
      Decision.
    """

    APPROVED = "approved"
    DISMISSED = "dismissed"
    ACTION_FAILED = "action_failed"
    NOT_FOUND = "not_found"
    STORE_ERROR = "store_error"


@dataclass(frozen=True)
class DecisionActionResult:
    """Structured result of an approve/dismiss request the dashboard renders.

    Attributes:
        outcome: The classified outcome (see :class:`DecisionActionOutcome`).
        decision_id: The id the request targeted.
        decision: The resolved Decision (with its updated status) on
            ``APPROVED``/``DISMISSED``; ``None`` otherwise.
        gap_fill: The gap-fill result (booked appointment + removed waitlist
            entry id) when a ``gap_fill`` Decision was approved successfully.
        error: A human-readable error indication to display on ``ACTION_FAILED``
            / ``STORE_ERROR`` / ``NOT_FOUND`` (Req 14.6); ``None`` on success.
    """

    outcome: DecisionActionOutcome
    decision_id: str
    decision: Decision | None = None
    gap_fill: GapFillResult | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True when the request resolved the Decision (approved or dismissed)."""
        return self.outcome in (
            DecisionActionOutcome.APPROVED,
            DecisionActionOutcome.DISMISSED,
        )


class DecisionActionService:
    """Executes approve/dismiss on open Decisions through the Data_Layer.

    Holds only store *interfaces* (never concrete storage) plus injectable
    clock/id generators. All reads and writes go through the Data_Layer, so the
    store's ``ChangeEvent`` emission (on a successful ``set_status``) is what
    removes a resolved Decision from every connected feed within 2 s (Req 14.5).
    """

    def __init__(
        self,
        *,
        decision_store: DecisionStore,
        appointment_store: AppointmentStore,
        waitlist_store: WaitlistStore,
        clock: Clock = _default_clock,
        id_gen: IdGen | None = None,
    ) -> None:
        self._decision_store = decision_store
        self._appointment_store = appointment_store
        self._waitlist_store = waitlist_store
        self._clock = clock
        self._id_gen = id_gen

    # -- public API --------------------------------------------------------

    def approve(self, decision_id: str) -> DecisionActionResult:
        """Approve a Decision: execute its action, then record the approval.

        Reads the target open Decision, executes its associated Data_Layer action
        (gap-fill for ``gap_fill`` Decisions; none for advisory kinds), and — only
        if the action succeeds — records ``APPROVED`` so the Decision leaves the
        open feed (Req 14.3, 14.5). On action-persistence failure the Decision is
        left open with an error indication and no partial effect (Req 14.6, 8.5).

        Args:
            decision_id: The id of the open Decision to approve.

        Returns:
            A :class:`DecisionActionResult` classifying the outcome.
        """
        located = self._find_open(decision_id)
        if isinstance(located, DecisionActionResult):
            return located  # NOT_FOUND / STORE_ERROR
        decision = located

        # Execute the associated action first so a failure leaves the Decision
        # open (Req 14.6): recording APPROVED before the action would drop it
        # from the open feed prematurely.
        gap_fill: GapFillResult | None = None
        if decision.kind == DecisionKind.GAP_FILL:
            action = self._execute_gap_fill(decision)
            if isinstance(action, DecisionActionResult):
                return action  # ACTION_FAILED, Decision left open
            gap_fill = action

        # Action succeeded (or none was required): record the approval.
        updated = self._decision_store.set_status(
            decision.id, DecisionStatus.APPROVED, self._clock()
        )
        if is_err(updated):
            return DecisionActionResult(
                outcome=DecisionActionOutcome.STORE_ERROR,
                decision_id=decision_id,
                gap_fill=gap_fill,
                error=f"failed to record approval: {updated.error.detail}",
            )

        return DecisionActionResult(
            outcome=DecisionActionOutcome.APPROVED,
            decision_id=decision_id,
            decision=updated.value,
            gap_fill=gap_fill,
        )

    def dismiss(self, decision_id: str) -> DecisionActionResult:
        """Dismiss a Decision: record ``DISMISSED`` and execute no action (Req 14.4).

        Args:
            decision_id: The id of the open Decision to dismiss.

        Returns:
            A :class:`DecisionActionResult`; ``DISMISSED`` on success, else
            ``NOT_FOUND`` / ``STORE_ERROR``.
        """
        located = self._find_open(decision_id)
        if isinstance(located, DecisionActionResult):
            return located
        decision = located

        updated = self._decision_store.set_status(
            decision.id, DecisionStatus.DISMISSED, self._clock()
        )
        if is_err(updated):
            return DecisionActionResult(
                outcome=DecisionActionOutcome.STORE_ERROR,
                decision_id=decision_id,
                error=f"failed to record dismissal: {updated.error.detail}",
            )

        return DecisionActionResult(
            outcome=DecisionActionOutcome.DISMISSED,
            decision_id=decision_id,
            decision=updated.value,
        )

    # -- internals ---------------------------------------------------------

    def _find_open(self, decision_id: str) -> Decision | DecisionActionResult:
        """Locate an open Decision by id via ``list_open`` (no get-by-id exists).

        Returns the matching :class:`Decision`, or a terminal
        :class:`DecisionActionResult` (``STORE_ERROR`` if the read fails,
        ``NOT_FOUND`` if no open Decision has this id).
        """
        listed = self._decision_store.list_open()
        if is_err(listed):
            return DecisionActionResult(
                outcome=DecisionActionOutcome.STORE_ERROR,
                decision_id=decision_id,
                error=f"failed to read open decisions: {listed.error.detail}",
            )
        for decision in listed.value:
            if decision.id == decision_id:
                return decision
        return DecisionActionResult(
            outcome=DecisionActionOutcome.NOT_FOUND,
            decision_id=decision_id,
            error=f"no open decision {decision_id!r}",
        )

    def _execute_gap_fill(
        self, decision: Decision
    ) -> GapFillResult | DecisionActionResult:
        """Run ``fill_gap_from_waitlist`` for a ``gap_fill`` Decision (Req 8.2–8.5).

        Reads the target slot id from ``action_payload["slot_id"]`` and invokes
        the tool. On any failure (missing slot id, or a tool ``Err``) returns an
        ``ACTION_FAILED`` result so the caller leaves the Decision open; the tool
        compensates its own partial writes, so no slot/entry change leaks
        (Req 14.6, 8.5).
        """
        slot_id = decision.action_payload.get("slot_id")
        if not slot_id:
            return DecisionActionResult(
                outcome=DecisionActionOutcome.ACTION_FAILED,
                decision_id=decision.id,
                error="gap_fill decision is missing action_payload['slot_id']",
            )

        kwargs: dict[str, object] = {"slot_id": slot_id, "clock": self._clock}
        if self._id_gen is not None:
            kwargs["id_gen"] = self._id_gen
        filled = fill_gap_from_waitlist(
            self._waitlist_store,
            self._appointment_store,
            **kwargs,  # type: ignore[arg-type]
        )
        if is_err(filled):
            return DecisionActionResult(
                outcome=DecisionActionOutcome.ACTION_FAILED,
                decision_id=decision.id,
                error=f"gap fill did not complete: {filled.error.kind}",
            )
        return filled.value


__all__ = [
    "Clock",
    "IdGen",
    "DecisionActionOutcome",
    "DecisionActionResult",
    "DecisionActionService",
]
