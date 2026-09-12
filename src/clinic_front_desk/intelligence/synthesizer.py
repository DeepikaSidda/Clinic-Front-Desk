"""``DecisionSynthesizer`` — generation gates + persistence (task 10.2, Req 13.3–13.8).

The synthesizer is the single place the design's *Decision generation flow* gates
live. Given the findings that :func:`~clinic_front_desk.intelligence.analyze.analyze_patterns`
produced, it turns a :class:`~clinic_front_desk.models.Finding` into a persisted
:class:`~clinic_front_desk.models.Decision` **iff** all three gates pass:

1. **Actionability (Req 13.4).** The finding must map to an action the doctor can
   approve or dismiss (``finding.actionable``).
2. **Support threshold (Req 13.6).** The finding must be backed by at least
   :data:`MIN_SUPPORTING_RECORDS` records (``supporting_record_count >= 5``).
3. **Dedupe (Req 13.3).** No open Decision may already share the finding's
   ``findingKey`` — checked against the :class:`~clinic_front_desk.data_layer.interfaces.DecisionStore`
   *and* against Decisions created earlier in the same run, so at most one open
   Decision ever exists per ``findingKey`` (Property 18).

Qualifying findings are persisted through the ``DecisionStore`` (Req 13.5).

Failure handling mirrors the design's *Practice-Intelligence errors* table:

- **Analysis failure (Req 13.7).** When ``analyze_patterns`` returns an
  :class:`~clinic_front_desk.models.Err`, the synthesizer generates **no**
  Decision and records the analysis failure (via the injected
  :data:`FailureRecorder` and on the returned :class:`SynthesisResult`).
- **Persistence failure (Req 13.8).** When a ``DecisionStore.create`` (or the
  dedupe lookup) fails, the finding is **retained** for a later run and, because
  nothing was persisted, no duplicate is created — the next run re-evaluates the
  same finding cleanly.

The synthesizer performs no analysis itself and mutates no schedule state; it
only reads findings and writes Decisions, so it is deterministic given the store
state and the injected clock / id generator, and trivial to unit- and
property-test against the in-memory fake store.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from clinic_front_desk.data_layer.interfaces import DecisionStore, NewDecision
from clinic_front_desk.models import (
    Decision,
    DecisionStatus,
    Finding,
    ISODateTime,
    ToolError,
    ToolResult,
    is_err,
)

#: Minimum number of supporting records a finding needs before it can become a
#: Decision (Req 13.6). Findings below this are dropped.
MIN_SUPPORTING_RECORDS: int = 5

#: A clock returning the current time as an ISO-8601 UTC string. Injectable so
#: tests can pin ``generated_at`` / failure timestamps deterministically.
Clock = Callable[[], str]

#: An id generator returning a fresh unique string. Injectable for deterministic
#: tests.
IdGen = Callable[[], str]

#: A sink that records an analysis failure "through the Data_Layer" (Req 13.7).
#: Injectable so the scheduler / deployment can wire it to whatever durable sink
#: is appropriate; defaults to a no-op (the failure is still reported on the
#: returned :class:`SynthesisResult`).
FailureRecorder = Callable[["AnalysisFailure"], None]


def _default_clock() -> str:
    return datetime.now(UTC).isoformat()


def _default_id() -> str:
    return uuid4().hex


def _noop_recorder(_failure: "AnalysisFailure") -> None:
    """Default failure sink: the failure is still surfaced on the result."""


@dataclass(frozen=True)
class AnalysisFailure:
    """A recorded ``analyze_patterns`` failure (Req 13.7).

    Attributes:
        detail: Human-readable detail extracted from the tool error.
        recorded_at: ISO-8601 UTC timestamp the failure was recorded at.
        error: The originating :class:`~clinic_front_desk.models.ToolError`.
    """

    detail: str
    recorded_at: ISODateTime
    error: ToolError


@dataclass(frozen=True)
class SynthesisResult:
    """The outcome of a single :meth:`DecisionSynthesizer.synthesize` run.

    Every finding the run saw lands in exactly one bucket (or, for a failed
    analysis run, all buckets are empty and :attr:`analysis_failed` is ``True``):

    - :attr:`created` — Decisions persisted this run (Req 13.5).
    - :attr:`skipped_non_actionable` — findings dropped by the actionability gate
      (Req 13.4).
    - :attr:`skipped_below_threshold` — findings dropped by the ≥ 5-record gate
      (Req 13.6).
    - :attr:`skipped_duplicate` — findings whose ``findingKey`` already has an
      open Decision (in the store or created earlier this run) (Req 13.3).
    - :attr:`retained` — findings retained for a later run because a store
      operation (dedupe lookup or persist) failed; no duplicate was created
      (Req 13.8).
    - :attr:`analysis_failed` / :attr:`failure` — set when the analysis itself
      failed (Req 13.7); no Decision is generated in that case.
    """

    created: list[Decision] = field(default_factory=list)
    skipped_non_actionable: list[Finding] = field(default_factory=list)
    skipped_below_threshold: list[Finding] = field(default_factory=list)
    skipped_duplicate: list[Finding] = field(default_factory=list)
    retained: list[Finding] = field(default_factory=list)
    analysis_failed: bool = False
    failure: AnalysisFailure | None = None


class DecisionSynthesizer:
    """Applies the generation gates and persists qualifying Decisions (Req 13.3–13.8)."""

    def __init__(
        self,
        store: DecisionStore,
        *,
        clock: Clock = _default_clock,
        id_gen: IdGen = _default_id,
        failure_recorder: FailureRecorder = _noop_recorder,
    ) -> None:
        """Create a synthesizer.

        Args:
            store: The :class:`DecisionStore` used for dedupe lookups and
                persistence.
            clock: Injectable clock for ``generated_at`` / failure timestamps.
            id_gen: Injectable id generator for new Decision ids.
            failure_recorder: Sink invoked to record an analysis failure
                (Req 13.7); defaults to a no-op.
        """
        self._store = store
        self._clock = clock
        self._id_gen = id_gen
        self._record_failure = failure_recorder

    def synthesize(self, analysis: ToolResult[list[Finding]]) -> SynthesisResult:
        """Turn the analysis result into persisted Decisions, applying every gate.

        Args:
            analysis: The :data:`~clinic_front_desk.models.ToolResult` returned by
                ``analyze_patterns``. On ``Err`` the analysis-failure path runs
                (Req 13.7); on ``Ok`` its findings are gated and persisted.

        Returns:
            A :class:`SynthesisResult` describing what happened to each finding.
        """
        # Req 13.7: a failed analysis run generates no Decision and records the
        # failure. Nothing is read from or written to the DecisionStore.
        if is_err(analysis):
            failure = self._build_failure(analysis.error)
            self._record_failure(failure)
            return SynthesisResult(analysis_failed=True, failure=failure)

        result = SynthesisResult()
        # Track findingKeys we persisted this run so two findings sharing a key
        # cannot both become open Decisions (Property 18: at most one per key).
        created_keys: set[str] = set()

        for finding in analysis.value:
            # Gate 1 — actionability (Req 13.4).
            if not finding.actionable:
                result.skipped_non_actionable.append(finding)
                continue

            # Gate 2 — support threshold (Req 13.6).
            if finding.supporting_record_count < MIN_SUPPORTING_RECORDS:
                result.skipped_below_threshold.append(finding)
                continue

            # Gate 3 — dedupe within this run (Req 13.3).
            if finding.key in created_keys:
                result.skipped_duplicate.append(finding)
                continue

            # Gate 3 — dedupe against open Decisions in the store (Req 13.3).
            existing = self._store.find_open_by_finding_key(finding.key)
            if is_err(existing):
                # Cannot confirm the finding is new; retain it for the next run
                # rather than risk a duplicate (Req 13.8).
                result.retained.append(finding)
                continue
            if existing.value is not None:
                result.skipped_duplicate.append(finding)
                continue

            # All gates passed: persist the Decision (Req 13.5).
            created = self._store.create(self._to_decision(finding))
            if is_err(created):
                # Persistence failed: retain the finding for a later run; nothing
                # was written, so the next run re-evaluates it with no duplicate
                # (Req 13.8).
                result.retained.append(finding)
                continue

            result.created.append(created.value)
            created_keys.add(finding.key)

        return result

    def _to_decision(self, finding: Finding) -> NewDecision:
        """Build an open :class:`~clinic_front_desk.models.Decision` from a finding."""
        return Decision(
            id=self._id_gen(),
            kind=finding.kind,
            finding_key=finding.key,
            summary=finding.summary,
            recommended_action=finding.recommended_action,
            action_payload=dict(finding.action_payload),
            supporting_record_count=finding.supporting_record_count,
            status=DecisionStatus.OPEN,
            generated_at=self._clock(),
            resolved_at=None,
        )

    def _build_failure(self, error: ToolError) -> AnalysisFailure:
        """Wrap a tool error as a timestamped :class:`AnalysisFailure`."""
        detail = getattr(error, "detail", None) or repr(error)
        return AnalysisFailure(detail=detail, recorded_at=self._clock(), error=error)


__all__ = [
    "MIN_SUPPORTING_RECORDS",
    "Clock",
    "IdGen",
    "FailureRecorder",
    "AnalysisFailure",
    "SynthesisResult",
    "DecisionSynthesizer",
]
