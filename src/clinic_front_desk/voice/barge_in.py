"""``BargeInHandler`` — preserve-and-resume task step across a barge-in (task 7.9).

Task 7.9 (Req 12.3). See design "Voice_Front_Desk / SessionContext" and the
Key Design Decisions: barge-in stop is driven by the ``VoiceStreamManager``
(≤ 500 ms, Req 12.2), but *what happens to the task* when the patient interrupts
is a separate, purely-logical concern — and that is what this component owns.

Requirement 12.3 states:

    "WHEN a Barge_In interrupts a spoken response, THE Voice_Front_Desk SHALL
     retain the current task context and resume the task from its
     pre-interruption step after processing the interruption."

Modelling choice
----------------
Like :class:`~clinic_front_desk.voice.turn_controller.TurnController`, the
handler is a small **deterministic** component that operates against a
:class:`~clinic_front_desk.voice.session_context.SessionContext` and performs no
audio side effects. This lets it be unit- and property-tested without a real
Nova Sonic stream.

Two facts of Req 12.3 are handled distinctly:

- **Accumulated context is retained.** The :class:`SessionContext` object is
  shared and mutated in place, so identifying details, requested service,
  date/time and slot selection are never discarded by a barge-in. Any *new*
  facts the patient supplies while the interruption is processed simply
  accumulate on the same context. The handler snapshots the known facts at
  interruption time purely so a caller (or a test) can assert the pre-interruption
  facts still hold afterwards — it never rolls them back.

- **The task step is resumed from its pre-interruption position.** Processing an
  interruption may advance or change ``SessionContext.task_step_index`` (e.g. a
  quick FAQ detour runs its own steps). The handler captures the step index at
  the moment of the barge-in and, once the interruption has been processed,
  restores it via :meth:`SessionContext.set_step` so the original task continues
  exactly where it left off.

Nested barge-ins (an interruption interrupted by a further interruption) are
supported with last-in-first-out semantics: each :meth:`BargeInHandler.on_barge_in`
pushes a checkpoint and each :meth:`BargeInHandler.resume` pops and restores the
most recent one, so the innermost interruption resumes first and the outermost
task step is restored last.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

from clinic_front_desk.voice.session_context import SessionContext

__all__ = ["BargeInCheckpoint", "BargeInHandler", "BargeInResumeScope"]


@dataclass(frozen=True)
class BargeInCheckpoint:
    """An immutable snapshot of a task's position when a barge-in occurred.

    Captured by :meth:`BargeInHandler.on_barge_in` and consumed by
    :meth:`BargeInHandler.resume` to restore the pre-interruption task step
    (Req 12.3).

    Attributes:
        step_index: The ``SessionContext.current_step_index`` at interruption
            time — the step the task resumes from.
        facts: A snapshot copy of the facts already provided when the barge-in
            occurred. Used only to *verify* preservation; the live context keeps
            these facts and accumulates any gathered during the interruption.
    """

    step_index: int
    facts: dict[str, Any] = field(default_factory=dict)


class BargeInHandler:
    """Capture and restore the pre-interruption task step for one Call_Session.

    A single instance tracks the barge-in checkpoints for one
    :class:`SessionContext`. It is side-effect free apart from setting the step
    index back on the supplied context during :meth:`resume`.
    """

    def __init__(self) -> None:
        # LIFO stack of outstanding checkpoints (supports nested barge-ins).
        self._checkpoints: list[BargeInCheckpoint] = []

    # -- read-only state ----------------------------------------------------

    @property
    def interrupted(self) -> bool:
        """Whether at least one barge-in is awaiting resume."""
        return bool(self._checkpoints)

    @property
    def pending(self) -> int:
        """The number of outstanding (not-yet-resumed) barge-in checkpoints."""
        return len(self._checkpoints)

    def peek(self) -> BargeInCheckpoint | None:
        """The most recent outstanding checkpoint, or ``None`` if none is pending."""
        return self._checkpoints[-1] if self._checkpoints else None

    # -- events -------------------------------------------------------------

    def on_barge_in(self, context: SessionContext) -> BargeInCheckpoint:
        """Record the current task step at the moment a barge-in interrupts (Req 12.3).

        Snapshots ``context.current_step_index`` and the facts provided so far,
        pushing a :class:`BargeInCheckpoint`. The context itself is left
        untouched, so all accumulated facts remain available while the
        interruption is processed. Returns the captured checkpoint.
        """
        checkpoint = BargeInCheckpoint(
            step_index=context.current_step_index,
            facts=dict(context.known_facts()),
        )
        self._checkpoints.append(checkpoint)
        return checkpoint

    def resume(self, context: SessionContext) -> BargeInCheckpoint:
        """Resume the interrupted task from its pre-interruption step (Req 12.3).

        Pops the most recent checkpoint and restores its step index onto
        ``context`` via :meth:`SessionContext.set_step`, so the task continues
        from exactly where the barge-in interrupted it — regardless of how the
        step index moved while the interruption was processed. Accumulated facts
        on the context are preserved untouched. Returns the checkpoint that was
        restored.

        Raises:
            RuntimeError: If called when no barge-in is awaiting resume.
        """
        if not self._checkpoints:
            raise RuntimeError("resume() called with no barge-in in progress")
        checkpoint = self._checkpoints.pop()
        context.set_step(checkpoint.step_index)
        return checkpoint

    def scope(self, context: SessionContext) -> "BargeInResumeScope":
        """A context manager that captures on entry and resumes on exit.

        Ergonomic wrapper around :meth:`on_barge_in` / :meth:`resume` for the
        common case::

            with handler.scope(ctx):
                ...  # process the interruption (may move ctx's step)
            # ctx.current_step_index is now back at its pre-interruption value

        The captured :class:`BargeInCheckpoint` is exposed as the ``as`` target.
        Resume runs even if the interruption processing raises, so the original
        task step is always restored (Req 12.3).
        """
        return BargeInResumeScope(self, context)


class BargeInResumeScope:
    """Context-manager helper returned by :meth:`BargeInHandler.scope`."""

    def __init__(self, handler: BargeInHandler, context: SessionContext) -> None:
        self._handler = handler
        self._context = context
        self._checkpoint: BargeInCheckpoint | None = None

    @property
    def checkpoint(self) -> BargeInCheckpoint | None:
        """The checkpoint captured on entry (``None`` before entering)."""
        return self._checkpoint

    def __enter__(self) -> BargeInCheckpoint:
        self._checkpoint = self._handler.on_barge_in(self._context)
        return self._checkpoint

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Always restore the pre-interruption step, even on error, so the task
        # resumes from where it was interrupted (Req 12.3).
        self._handler.resume(self._context)
