"""The ``AnalysisScheduler`` (task 10.3, Req 13.1).

Practice_Intelligence must "start an analysis run on a recurring interval not
exceeding 24 hours" (Req 13.1). This module models that trigger as a small,
fully deterministic scheduler so the recurring-interval guarantee can be
verified without any real wall-clock sleeping:

* The interval is a **configurable value with a documented maximum of 24 h**
  (:data:`MAX_INTERVAL`). Intervals above the maximum are **clamped** to 24 h by
  default (so :attr:`AnalysisScheduler.interval` is *always* ``<= 24 h``); pass
  ``strict=True`` to instead **reject** an over-long interval with
  ``ValueError``. Either way Req 13.1 holds.
* Time is supplied by an **injectable clock** (any ``() -> datetime`` callable),
  and the scheduler is driven by an explicit :meth:`~AnalysisScheduler.tick`
  method rather than a background thread or ``time.sleep``. Tests advance a fake
  clock and call ``tick`` to observe exactly when runs fire.
* The **run callback is provided by the caller** — the scheduler never imports
  the ``analyze_patterns`` tool or the ``DecisionSynthesizer`` (task 10.2), so
  it stays decoupled from what a run actually does. A production wiring passes a
  callback that invokes ``analyze_patterns`` + ``DecisionSynthesizer.synthesize``
  (see task 14.1).

The scheduler assumes the run callback handles its own failures (the
synthesizer already takes the Req 13.7 "generate no Decision and record the
failure" path internally); a callback that raises will propagate out of
``tick``/``trigger`` and leave the next-run time unchanged so the caller can
decide how to recover.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

# Documented maximum recurring interval (Req 13.1). The effective interval is
# always <= this value.
MAX_INTERVAL: timedelta = timedelta(hours=24)

# Default to the maximum allowed cadence (once per 24 h).
DEFAULT_INTERVAL: timedelta = MAX_INTERVAL

# The scheduler only cares that a run *happens*; any return value is ignored,
# so the callback is typed as returning ``object`` to accept e.g. a wiring that
# returns a ``SynthesisResult`` (task 14.1) without an adapter lambda.
RunCallback = Callable[[], object]
Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    """Default clock: timezone-aware UTC now."""
    return datetime.now(timezone.utc)


class AnalysisScheduler:
    """Fires analysis runs on a recurring interval not exceeding 24 h (Req 13.1).

    The scheduler holds no threads and never sleeps. The owner drives it by
    calling :meth:`tick` (e.g. from an external timer, an AgentCore scheduled
    entrypoint, or a test) — each ``tick`` runs the callback at most once, and
    only when the interval has elapsed.

    Args:
        run: The zero-argument callback invoked for each analysis run. Typically
            wraps ``analyze_patterns`` + ``DecisionSynthesizer.synthesize``. It
            is expected to handle its own failures.
        interval: Desired recurring interval. Must be positive. Values greater
            than :data:`MAX_INTERVAL` are clamped to 24 h unless ``strict`` is
            set, in which case they raise ``ValueError``.
        clock: Injectable time source returning the current time. Defaults to
            timezone-aware UTC now.
        start_at: Optional time of the first scheduled run. Defaults to
            ``clock() + interval`` (the first run fires one interval from
            construction).
        strict: When ``True``, reject an interval above :data:`MAX_INTERVAL`
            instead of clamping it.

    Raises:
        ValueError: If ``interval`` is not positive, or ``strict`` is set and
            ``interval`` exceeds :data:`MAX_INTERVAL`.
    """

    def __init__(
        self,
        run: RunCallback,
        interval: timedelta = DEFAULT_INTERVAL,
        *,
        clock: Clock = _utcnow,
        start_at: datetime | None = None,
        strict: bool = False,
    ) -> None:
        if interval <= timedelta(0):
            raise ValueError("interval must be positive")
        if interval > MAX_INTERVAL:
            if strict:
                raise ValueError(
                    f"interval {interval} exceeds the maximum of {MAX_INTERVAL} (Req 13.1)"
                )
            interval = MAX_INTERVAL  # clamp to the documented maximum

        self._run = run
        self._clock = clock
        # Invariant (Req 13.1): 0 < interval <= MAX_INTERVAL.
        self.interval: timedelta = interval
        self.run_count: int = 0

        now = self._clock()
        self._next_run_at: datetime = start_at if start_at is not None else now + interval

    @property
    def next_run_at(self) -> datetime:
        """The time at or after which the next run is due."""
        return self._next_run_at

    def is_due(self, now: datetime | None = None) -> bool:
        """Return whether a run is due at ``now`` (defaults to ``clock()``)."""
        current = now if now is not None else self._clock()
        return current >= self._next_run_at

    def tick(self, now: datetime | None = None) -> bool:
        """Run the callback iff the interval has elapsed, then reschedule.

        Args:
            now: Optional explicit current time; defaults to ``clock()``.

        Returns:
            ``True`` if a run fired (and the next run was scheduled for
            ``now + interval``), ``False`` if it was not yet due.
        """
        current = now if now is not None else self._clock()
        if current < self._next_run_at:
            return False
        self._invoke(current)
        return True

    def trigger(self, now: datetime | None = None) -> None:
        """Force a run immediately and reschedule the next run from ``now``.

        Lets an owner kick off an out-of-band analysis run (e.g. right after
        onboarding) without waiting for the interval to elapse.
        """
        current = now if now is not None else self._clock()
        self._invoke(current)

    def _invoke(self, now: datetime) -> None:
        """Run the callback and schedule the next run one interval from ``now``.

        The next-run time is only advanced after the callback returns, so a
        raising callback leaves the schedule unchanged.
        """
        self._run()
        self.run_count += 1
        self._next_run_at = now + self.interval


__all__ = ["AnalysisScheduler", "MAX_INTERVAL", "DEFAULT_INTERVAL"]
