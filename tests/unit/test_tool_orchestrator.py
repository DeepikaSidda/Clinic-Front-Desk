"""Unit tests for the ``ToolOrchestrator`` (task 7.5).

Covers the four behaviours the orchestrator coordinates:

- Tool chaining threads each output into the next step and completes the task
  (Req 11.2).
- A mid-chain tool failure stops the chain, retains the facts gathered so far,
  and offers to take a message (Req 11.3).
- Confirm-before-mutate for cancel: a declined cancellation never calls the
  tool, so the appointment is left unchanged (Req 5.6).
- Confirm-before-mutate for reschedule: no alternative slots leaves the original
  appointment unchanged and offers the waitlist (Req 4.5).

The property test for reschedule/cancel confirmation semantics (Property 8)
lives in task 7.6.
"""

from __future__ import annotations

from clinic_front_desk.models import (
    CallOutcome,
    Err,
    NotFound,
    Ok,
    StoreFailure,
)
from clinic_front_desk.voice.session_context import SessionContext
from clinic_front_desk.voice.tool_orchestrator import (
    ChainCompleted,
    ChainStep,
    MutationCommitted,
    MutationDeclined,
    NoAlternativeSlots,
    OfferToTakeMessage,
    ToolOrchestrator,
)


def _orchestrator() -> ToolOrchestrator:
    return ToolOrchestrator(SessionContext(session_id="call-1"))


# -- tool chaining (Req 11.2) ----------------------------------------------


def test_run_chain_threads_each_output_into_the_next_step() -> None:
    """Req 11.2: each completed tool's output informs the next invocation."""
    orch = _orchestrator()

    def step_one(_ctx) -> Ok:
        return Ok("patient-42")

    def step_two(ctx) -> Ok:
        # Consume the previous step's output by name and by `last`.
        assert ctx.output("lookup") == "patient-42"
        assert ctx.last == "patient-42"
        return Ok(["slot-a", "slot-b"])

    def step_three(ctx) -> Ok:
        assert ctx.output("availability") == ["slot-a", "slot-b"]
        return Ok("appt-7")

    outcome = orch.run_chain(
        [
            ChainStep("lookup", step_one),
            ChainStep("availability", step_two),
            ChainStep("book", step_three),
        ]
    )

    assert isinstance(outcome, ChainCompleted)
    assert outcome.value == "appt-7"
    assert outcome.outputs == {
        "lookup": "patient-42",
        "availability": ["slot-a", "slot-b"],
        "book": "appt-7",
    }


def test_run_chain_steps_can_read_session_facts() -> None:
    """Req 11.2/11.4: steps carry facts across the chain via the SessionContext."""
    orch = _orchestrator()

    def gather(ctx) -> Ok:
        ctx.session.set_requested_service("ear cleaning")
        return Ok(None)

    def use(ctx) -> Ok:
        # The service gathered by the earlier step is available to this one.
        assert ctx.session.recall("requested_service") == "ear cleaning"
        return Ok("done")

    outcome = orch.run_chain([ChainStep("gather", gather), ChainStep("use", use)])

    assert isinstance(outcome, ChainCompleted)
    assert orch.session.requested_service == "ear cleaning"


def test_empty_chain_completes_with_no_value() -> None:
    """An empty chain trivially completes."""
    outcome = _orchestrator().run_chain([])

    assert isinstance(outcome, ChainCompleted)
    assert outcome.value is None
    assert outcome.outputs == {}


# -- mid-chain failure retention (Req 11.3) --------------------------------


def test_mid_chain_failure_stops_and_offers_to_take_a_message() -> None:
    """Req 11.3: a failing step stops the chain and offers to take a message."""
    orch = _orchestrator()
    third_called = False

    def ok_step(ctx) -> Ok:
        ctx.session.set_identity(name="Jamie", callback_phone="555-0100")
        return Ok("ok")

    def failing_step(_ctx) -> Err:
        return Err(StoreFailure(store="AppointmentStore", detail="boom"))

    def never(ctx):  # pragma: no cover - must not run
        nonlocal third_called
        third_called = True
        return Ok("nope")

    outcome = orch.run_chain(
        [
            ChainStep("lookup", ok_step),
            ChainStep("book", failing_step),
            ChainStep("confirm", never),
        ]
    )

    assert isinstance(outcome, OfferToTakeMessage)
    assert outcome.failed_step == "book"
    assert isinstance(outcome.error, StoreFailure)
    assert third_called is False


def test_mid_chain_failure_retains_gathered_context() -> None:
    """Req 11.3: facts gathered before the failure are retained, not discarded."""
    orch = _orchestrator()

    def gather(ctx) -> Ok:
        ctx.session.set_identity(name="Jamie", callback_phone="555-0100")
        ctx.session.set_requested_service("hearing test")
        return Ok("patient-1")

    def fail(_ctx) -> Err:
        return Err(NotFound(detail="no slots"))

    outcome = orch.run_chain([ChainStep("lookup", gather), ChainStep("avail", fail)])

    assert isinstance(outcome, OfferToTakeMessage)
    # The retained snapshot proves the gathered facts survived the failure.
    assert outcome.retained_facts["name"] == "Jamie"
    assert outcome.retained_facts["callback_phone"] == "555-0100"
    assert outcome.retained_facts["requested_service"] == "hearing test"
    # And they are still live on the session for reuse without re-asking.
    assert orch.session.recall("requested_service") == "hearing test"


# -- confirm-before-cancel (Req 5.6) ---------------------------------------


def test_declined_cancellation_leaves_state_unchanged() -> None:
    """Req 5.6: declining the cancellation never invokes the cancel tool."""
    orch = _orchestrator()
    called = False

    def cancel_tool():  # pragma: no cover - must not run
        nonlocal called
        called = True
        return Ok("released-slot")

    outcome = orch.cancel(confirmed=False, cancel_tool=cancel_tool)

    assert isinstance(outcome, MutationDeclined)
    assert outcome.action == "cancel"
    assert called is False
    assert orch.session.outcome is None


def test_confirmed_cancellation_commits_and_records_outcome() -> None:
    """A confirmed cancellation invokes the tool and records the outcome."""
    orch = _orchestrator()

    outcome = orch.cancel(confirmed=True, cancel_tool=lambda: Ok("released-slot"))

    assert isinstance(outcome, MutationCommitted)
    assert outcome.outcome == CallOutcome.CANCELLED
    assert outcome.value == "released-slot"
    assert orch.session.outcome == CallOutcome.CANCELLED


def test_confirmed_cancellation_persist_failure_offers_message() -> None:
    """Req 5.8/11.3: a failed confirmed cancel offers to take a message and
    records no cancelled outcome (appointment left unchanged)."""
    orch = _orchestrator()

    outcome = orch.cancel(
        confirmed=True,
        cancel_tool=lambda: Err(StoreFailure(store="AppointmentStore", detail="down")),
    )

    assert isinstance(outcome, OfferToTakeMessage)
    assert outcome.failed_step == "cancel"
    assert orch.session.outcome is None


# -- confirm-before-reschedule (Req 4.5) -----------------------------------


def test_reschedule_with_no_alternative_slots_leaves_appointment_unchanged() -> None:
    """Req 4.5: no alternative slots leaves the original appointment unchanged
    and offers the waitlist; the reschedule tool is never invoked."""
    orch = _orchestrator()
    called = False

    def reschedule_tool():  # pragma: no cover - must not run
        nonlocal called
        called = True
        return Ok("moved")

    outcome = orch.reschedule(
        alternative_slots=[],
        confirmed=True,
        reschedule_tool=reschedule_tool,
    )

    assert isinstance(outcome, NoAlternativeSlots)
    assert outcome.offer_waitlist is True
    assert called is False
    assert orch.session.outcome is None


def test_declined_reschedule_leaves_appointment_unchanged() -> None:
    """A declined new slot never invokes the reschedule tool."""
    orch = _orchestrator()
    called = False

    def reschedule_tool():  # pragma: no cover - must not run
        nonlocal called
        called = True
        return Ok("moved")

    outcome = orch.reschedule(
        alternative_slots=["slot-a"],
        confirmed=False,
        reschedule_tool=reschedule_tool,
    )

    assert isinstance(outcome, MutationDeclined)
    assert outcome.action == "reschedule"
    assert called is False
    assert orch.session.outcome is None


def test_confirmed_reschedule_commits_and_records_outcome() -> None:
    """A confirmed reschedule with slots available invokes the tool."""
    orch = _orchestrator()

    outcome = orch.reschedule(
        alternative_slots=["slot-a", "slot-b"],
        confirmed=True,
        reschedule_tool=lambda: Ok("appt-moved"),
    )

    assert isinstance(outcome, MutationCommitted)
    assert outcome.outcome == CallOutcome.RESCHEDULED
    assert outcome.value == "appt-moved"
    assert orch.session.outcome == CallOutcome.RESCHEDULED


def test_confirmed_reschedule_persist_failure_offers_message() -> None:
    """Req 4.9/11.3: a failed confirmed reschedule offers to take a message and
    records no rescheduled outcome (original appointment left unchanged)."""
    orch = _orchestrator()

    outcome = orch.reschedule(
        alternative_slots=["slot-a"],
        confirmed=True,
        reschedule_tool=lambda: Err(StoreFailure(store="AppointmentStore", detail="x")),
    )

    assert isinstance(outcome, OfferToTakeMessage)
    assert outcome.failed_step == "reschedule"
    assert orch.session.outcome is None
