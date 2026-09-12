"""Unit tests for ``BargeInHandler`` (task 7.9, Req 12.3).

These are example/edge-case unit tests. The barge-in preserve-and-resume
*property* test is task 7.10 (Property 17) and is intentionally not written here.
"""

from __future__ import annotations

import pytest

from clinic_front_desk.voice.barge_in import (
    BargeInCheckpoint,
    BargeInHandler,
    BargeInResumeScope,
)
from clinic_front_desk.voice.session_context import SessionContext


def _ctx(session_id: str = "call-1") -> SessionContext:
    return SessionContext(session_id=session_id)


def test_resume_restores_pre_interruption_step() -> None:
    """A barge-in that moves the step index still resumes from where it started (Req 12.3)."""
    ctx = _ctx()
    ctx.set_step(3)
    handler = BargeInHandler()

    handler.on_barge_in(ctx)
    # Interruption processing moves the task step around.
    ctx.set_step(0)
    ctx.advance_step()
    assert ctx.current_step_index == 1

    handler.resume(ctx)
    assert ctx.current_step_index == 3


def test_on_barge_in_returns_checkpoint_and_marks_interrupted() -> None:
    ctx = _ctx()
    ctx.set_step(2)
    handler = BargeInHandler()

    assert handler.interrupted is False
    assert handler.pending == 0

    checkpoint = handler.on_barge_in(ctx)

    assert isinstance(checkpoint, BargeInCheckpoint)
    assert checkpoint.step_index == 2
    assert handler.interrupted is True
    assert handler.pending == 1
    assert handler.peek() == checkpoint


def test_on_barge_in_does_not_mutate_context() -> None:
    """Capturing a barge-in leaves the context (facts + step) untouched."""
    ctx = _ctx()
    ctx.set_identity(name="Ada", callback_phone="555-0100")
    ctx.set_requested_service("hearing test")
    ctx.set_step(4)
    handler = BargeInHandler()

    handler.on_barge_in(ctx)

    assert ctx.current_step_index == 4
    assert ctx.name == "Ada"
    assert ctx.callback_phone == "555-0100"
    assert ctx.requested_service == "hearing test"


def test_accumulated_facts_are_preserved_across_barge_in() -> None:
    """Facts gathered before and during the interruption both survive (Req 12.3)."""
    ctx = _ctx()
    ctx.set_identity(name="Ada")
    ctx.set_step(2)
    handler = BargeInHandler()

    checkpoint = handler.on_barge_in(ctx)
    # New facts supplied while the interruption is handled accumulate.
    ctx.set_identity(callback_phone="555-0199")
    ctx.set_requested_service("wax removal")

    handler.resume(ctx)

    # Pre-interruption fact snapshot only captured what was known then.
    assert checkpoint.facts == {"name": "Ada"}
    # The live context retains the original AND the newly gathered facts.
    assert ctx.name == "Ada"
    assert ctx.callback_phone == "555-0199"
    assert ctx.requested_service == "wax removal"
    assert ctx.current_step_index == 2


def test_resume_without_barge_in_raises() -> None:
    handler = BargeInHandler()
    with pytest.raises(RuntimeError):
        handler.resume(_ctx())


def test_resume_clears_pending_state() -> None:
    ctx = _ctx()
    ctx.set_step(1)
    handler = BargeInHandler()

    handler.on_barge_in(ctx)
    handler.resume(ctx)

    assert handler.interrupted is False
    assert handler.pending == 0
    assert handler.peek() is None


def test_nested_barge_ins_resume_lifo() -> None:
    """Nested interruptions resume innermost-first, restoring each captured step."""
    ctx = _ctx()
    ctx.set_step(5)
    handler = BargeInHandler()

    handler.on_barge_in(ctx)  # captures step 5
    ctx.set_step(8)
    handler.on_barge_in(ctx)  # captures step 8
    ctx.set_step(0)

    assert handler.pending == 2

    handler.resume(ctx)  # restore innermost (8)
    assert ctx.current_step_index == 8

    handler.resume(ctx)  # restore outermost (5)
    assert ctx.current_step_index == 5
    assert handler.interrupted is False


def test_step_zero_is_preserved() -> None:
    """A task interrupted at its very first step resumes at step 0, not a default."""
    ctx = _ctx()
    assert ctx.current_step_index == 0
    handler = BargeInHandler()

    handler.on_barge_in(ctx)
    ctx.set_step(7)
    handler.resume(ctx)

    assert ctx.current_step_index == 0


def test_scope_context_manager_restores_step() -> None:
    ctx = _ctx()
    ctx.set_step(3)
    handler = BargeInHandler()

    with handler.scope(ctx) as checkpoint:
        assert isinstance(checkpoint, BargeInCheckpoint)
        assert checkpoint.step_index == 3
        assert handler.interrupted is True
        ctx.advance_step()  # move during interruption

    assert ctx.current_step_index == 3
    assert handler.interrupted is False


def test_scope_restores_step_even_on_error() -> None:
    ctx = _ctx()
    ctx.set_step(2)
    handler = BargeInHandler()

    with pytest.raises(ValueError):
        with handler.scope(ctx):
            ctx.set_step(9)
            raise ValueError("interruption blew up")

    assert ctx.current_step_index == 2
    assert handler.interrupted is False


def test_scope_returns_resume_scope() -> None:
    handler = BargeInHandler()
    scope = handler.scope(_ctx())
    assert isinstance(scope, BargeInResumeScope)
    assert scope.checkpoint is None
