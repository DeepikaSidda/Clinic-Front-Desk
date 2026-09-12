"""Unit tests for the ``DecisionSynthesizer`` (task 10.2).

Focused example/edge tests over the generation gates and persistence
(Req 13.3–13.8):

- actionability gate (Req 13.4) and ≥ 5-record support gate (Req 13.6);
- dedupe against open Decisions and within a run (Req 13.3), so at most one open
  Decision exists per ``findingKey``;
- persistence of qualifying Decisions through the ``DecisionStore`` (Req 13.5);
- analysis-failure path generating no Decision and recording the failure
  (Req 13.7);
- persistence-failure path retaining the finding with no duplicate (Req 13.8).

The exhaustive property tests (decision-generation gates, analysis-failure) live
in tasks 10.4 and 10.5 and are out of scope here.
"""

from __future__ import annotations

from itertools import count

from clinic_front_desk.data_layer.faults import fail_on, wrap
from clinic_front_desk.data_layer.memory import MemoryDecisionStore
from clinic_front_desk.intelligence.synthesizer import (
    MIN_SUPPORTING_RECORDS,
    AnalysisFailure,
    DecisionSynthesizer,
)
from clinic_front_desk.models import (
    Decision,
    DecisionKind,
    DecisionStatus,
    Err,
    Finding,
    Ok,
    StoreFailure,
    is_ok,
)


def _finding(
    key: str,
    *,
    kind: DecisionKind = DecisionKind.UNMET_DEMAND,
    supporting: int = MIN_SUPPORTING_RECORDS,
    actionable: bool = True,
) -> Finding:
    return Finding(
        key=key,
        kind=kind,
        summary=f"summary for {key}",
        recommended_action=f"do something about {key}",
        action_payload={"key": key},
        supporting_record_count=supporting,
        actionable=actionable,
    )


def _synth(store: MemoryDecisionStore | object) -> DecisionSynthesizer:
    """A synthesizer with deterministic ids and a pinned clock."""
    ids = (f"dec-{n}" for n in count(1))
    return DecisionSynthesizer(
        store,  # type: ignore[arg-type]
        clock=lambda: "2025-06-01T00:00:00+00:00",
        id_gen=lambda: next(ids),
    )


# --- happy path / persistence (Req 13.5) -----------------------------------


def test_qualifying_finding_is_persisted_as_open_decision() -> None:
    store = MemoryDecisionStore()
    result = _synth(store).synthesize(Ok([_finding("unmet_demand#implant")]))

    assert len(result.created) == 1
    decision = result.created[0]
    assert isinstance(decision, Decision)
    assert decision.finding_key == "unmet_demand#implant"
    assert decision.kind == DecisionKind.UNMET_DEMAND
    assert decision.status == DecisionStatus.OPEN
    assert decision.supporting_record_count == MIN_SUPPORTING_RECORDS
    assert decision.generated_at == "2025-06-01T00:00:00+00:00"
    # Persisted through the store and visible on the open feed (Req 13.5, 14.1).
    open_feed = store.list_open().unwrap()
    assert [d.finding_key for d in open_feed] == ["unmet_demand#implant"]


def test_multiple_qualifying_findings_each_persisted() -> None:
    store = MemoryDecisionStore()
    findings = [
        _finding("unmet_demand#implant"),
        _finding("no_show_trend#30d", kind=DecisionKind.NO_SHOW_TREND),
        _finding("gap_fill#slot-1", kind=DecisionKind.GAP_FILL),
    ]
    result = _synth(store).synthesize(Ok(findings))

    assert len(result.created) == 3
    assert {d.finding_key for d in store.list_open().unwrap()} == {
        "unmet_demand#implant",
        "no_show_trend#30d",
        "gap_fill#slot-1",
    }


# --- actionability gate (Req 13.4) -----------------------------------------


def test_non_actionable_finding_generates_no_decision() -> None:
    store = MemoryDecisionStore()
    result = _synth(store).synthesize(
        Ok([_finding("unmet_demand#implant", actionable=False)])
    )

    assert result.created == []
    assert len(result.skipped_non_actionable) == 1
    assert store.list_open().unwrap() == []


# --- support-threshold gate (Req 13.6) -------------------------------------


def test_finding_below_threshold_generates_no_decision() -> None:
    store = MemoryDecisionStore()
    result = _synth(store).synthesize(
        Ok([_finding("unmet_demand#implant", supporting=MIN_SUPPORTING_RECORDS - 1)])
    )

    assert result.created == []
    assert len(result.skipped_below_threshold) == 1
    assert store.list_open().unwrap() == []


def test_finding_exactly_at_threshold_is_generated() -> None:
    store = MemoryDecisionStore()
    result = _synth(store).synthesize(
        Ok([_finding("unmet_demand#implant", supporting=MIN_SUPPORTING_RECORDS)])
    )

    assert len(result.created) == 1


# --- dedupe (Req 13.3) -----------------------------------------------------


def test_dedupe_against_existing_open_decision() -> None:
    store = MemoryDecisionStore()
    # Seed an open Decision sharing the finding's key.
    seeded = store.create(
        Decision(
            id="pre-existing",
            kind=DecisionKind.UNMET_DEMAND,
            finding_key="unmet_demand#implant",
            summary="pre-existing",
            recommended_action="",
            supporting_record_count=MIN_SUPPORTING_RECORDS,
            status=DecisionStatus.OPEN,
            generated_at="2025-05-01T00:00:00+00:00",
        )
    )
    assert is_ok(seeded)

    result = _synth(store).synthesize(Ok([_finding("unmet_demand#implant")]))

    assert result.created == []
    assert len(result.skipped_duplicate) == 1
    # Still exactly one open Decision for the key (Property 18).
    open_feed = store.list_open().unwrap()
    assert [d.id for d in open_feed] == ["pre-existing"]


def test_resolved_decision_does_not_block_regeneration() -> None:
    store = MemoryDecisionStore()
    store.create(
        Decision(
            id="old",
            kind=DecisionKind.UNMET_DEMAND,
            finding_key="unmet_demand#implant",
            summary="old",
            recommended_action="",
            supporting_record_count=MIN_SUPPORTING_RECORDS,
            status=DecisionStatus.OPEN,
            generated_at="2025-05-01T00:00:00+00:00",
        )
    )
    # Resolve it (dismissed) -> no longer an *open* Decision for the key.
    store.set_status("old", DecisionStatus.DISMISSED, "2025-05-02T00:00:00+00:00")

    result = _synth(store).synthesize(Ok([_finding("unmet_demand#implant")]))

    assert len(result.created) == 1
    # The new open Decision plus the dismissed one coexist; only one is open.
    open_feed = store.list_open().unwrap()
    assert [d.id for d in open_feed] == ["dec-1"]


def test_duplicate_findings_in_same_run_produce_one_decision() -> None:
    store = MemoryDecisionStore()
    findings = [_finding("unmet_demand#implant"), _finding("unmet_demand#implant")]
    result = _synth(store).synthesize(Ok(findings))

    assert len(result.created) == 1
    assert len(result.skipped_duplicate) == 1
    assert len(store.list_open().unwrap()) == 1


# --- analysis failure (Req 13.7) -------------------------------------------


def test_analysis_failure_generates_no_decision_and_records_failure() -> None:
    store = MemoryDecisionStore()
    recorded: list[AnalysisFailure] = []
    synth = DecisionSynthesizer(
        store,
        clock=lambda: "2025-06-01T00:00:00+00:00",
        id_gen=lambda: "unused",
        failure_recorder=recorded.append,
    )

    result = synth.synthesize(
        Err(StoreFailure(store="analyze_patterns", detail="boom"))
    )

    # No Decision generated (Req 13.7).
    assert result.created == []
    assert store.list_open().unwrap() == []
    # Failure recorded on the result and through the injected recorder.
    assert result.analysis_failed is True
    assert result.failure is not None
    assert result.failure.detail == "boom"
    assert result.failure.recorded_at == "2025-06-01T00:00:00+00:00"
    assert len(recorded) == 1
    assert recorded[0].detail == "boom"


# --- persistence failure (Req 13.8) ----------------------------------------


def test_persistence_failure_retains_finding_and_creates_no_duplicate() -> None:
    base = MemoryDecisionStore()
    store = wrap(base, fail_on("create"))
    result = _synth(store).synthesize(Ok([_finding("unmet_demand#implant")]))

    # Nothing persisted; finding retained for the next run (Req 13.8).
    assert result.created == []
    assert len(result.retained) == 1
    assert result.retained[0].key == "unmet_demand#implant"
    assert base.list_open().unwrap() == []


def test_retained_finding_is_generated_cleanly_on_next_run() -> None:
    base = MemoryDecisionStore()
    controller_store = wrap(base, fail_on("create"))
    finding = _finding("unmet_demand#implant")

    # First run: persistence fails -> retained, no duplicate.
    first = _synth(controller_store).synthesize(Ok([finding]))
    assert first.created == []
    assert len(first.retained) == 1

    # Second run against the healthy base store: the retained finding is
    # generated with no leftover duplicate (Req 13.8).
    second = _synth(base).synthesize(Ok([finding]))
    assert len(second.created) == 1
    assert len(base.list_open().unwrap()) == 1


def test_dedupe_lookup_failure_retains_finding() -> None:
    base = MemoryDecisionStore()
    store = wrap(base, fail_on("find_open_by_finding_key"))
    result = _synth(store).synthesize(Ok([_finding("unmet_demand#implant")]))

    # Cannot confirm novelty -> retain rather than risk a duplicate (Req 13.8).
    assert result.created == []
    assert len(result.retained) == 1
    assert base.list_open().unwrap() == []


# --- empty input -----------------------------------------------------------


def test_empty_findings_creates_nothing() -> None:
    store = MemoryDecisionStore()
    result = _synth(store).synthesize(Ok([]))

    assert result.created == []
    assert result.analysis_failed is False
    assert store.list_open().unwrap() == []
