"""Property-based tests for Practice_Intelligence (tasks 10.4, 10.5, 10.6).

Hypothesis property tests validating three design correctness properties over
the intelligence subsystem, run against the in-memory fake stores:

- **Property 18** — Decision generation gates (threshold, actionability, dedup)
  in the :class:`~clinic_front_desk.intelligence.synthesizer.DecisionSynthesizer`
  (task 10.4, Req 13.3, 13.4, 13.6).
- **Property 28** — Analysis-failure produces no Decisions and records the
  failure (task 10.5, Req 13.7).
- **Property 12** — Gap-fill generation, earliest-selection, and assignment,
  spanning :func:`~clinic_front_desk.intelligence.detectors.detect_gap_fill`
  (generation) and :func:`~clinic_front_desk.tools.waitlist.fill_gap_from_waitlist`
  (execution) (task 10.6, Req 8.1, 8.2, 8.3, 8.4, 8.6).

Per the design's Testing Strategy each property is a single test running >= 100
iterations. Generators follow the design's "Generators" note: findings with
``supporting_record_count`` straddling 5, mixed ``actionable`` flags, and
duplicate ``findingKey``s alongside pre-existing open Decisions; and for
gap-fill, random open slots with waitlist states including duplicate
services and equal ``added_at`` timestamps to exercise the tiebreak.
"""

from __future__ import annotations

from itertools import count

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from clinic_front_desk.data_layer.memory import (
    MemoryAppointmentStore,
    MemoryDecisionStore,
    MemoryWaitlistStore,
)
from clinic_front_desk.intelligence.detectors import PatternInput, detect_gap_fill
from clinic_front_desk.intelligence.synthesizer import (
    MIN_SUPPORTING_RECORDS,
    AnalysisFailure,
    DecisionSynthesizer,
)
from clinic_front_desk.models import (
    Ambiguous,
    Appointment,
    Decision,
    DecisionKind,
    DecisionStatus,
    Err,
    Finding,
    NotFound,
    Ok,
    Slot,
    SlotStatus,
    StoreFailure,
    Validation,
    WaitlistEntry,
    is_err,
    is_ok,
)
from clinic_front_desk.tools.waitlist import fill_gap_from_waitlist

PINNED_CLOCK = "2025-06-01T00:00:00+00:00"

# A small pool of finding keys so generated findings and pre-existing Decisions
# collide, exercising the dedupe gate (design "Generators": duplicate findingKeys
# alongside pre-existing open Decisions).
_KEY_POOL = [f"unmet_demand#svc-{i}" for i in range(5)]

# A small pool of service names so slots and waitlist entries collide on service
# (and miss), exercising both the match and no-match gap-fill branches.
_SERVICE_POOL = ["cleaning", "implant", "filling"]


def _make_synthesizer(store: object) -> DecisionSynthesizer:
    """A synthesizer with deterministic ids and a pinned clock."""
    ids = (f"dec-{n}" for n in count(1))
    return DecisionSynthesizer(
        store,  # type: ignore[arg-type]
        clock=lambda: PINNED_CLOCK,
        id_gen=lambda: next(ids),
    )


# ---------------------------------------------------------------------------
# Property 18 — Decision generation gates (task 10.4, Req 13.3, 13.4, 13.6).
# ---------------------------------------------------------------------------

# A single generated finding: key drawn from the pool, supporting count
# straddling the >= 5 threshold, and a mixed actionable flag.
_finding_strategy = st.builds(
    lambda key, supporting, actionable: Finding(
        key=key,
        kind=DecisionKind.UNMET_DEMAND,
        summary=f"summary for {key}",
        recommended_action=f"action for {key}",
        action_payload={"key": key},
        supporting_record_count=supporting,
        actionable=actionable,
    ),
    key=st.sampled_from(_KEY_POOL),
    supporting=st.integers(min_value=0, max_value=10),
    actionable=st.booleans(),
)


@pytest.mark.property
@settings(max_examples=200)
@given(
    findings=st.lists(_finding_strategy, min_size=0, max_size=12),
    # Pre-existing OPEN Decisions, one per (distinct) key drawn from the pool.
    preexisting_open_keys=st.lists(
        st.sampled_from(_KEY_POOL), min_size=0, max_size=5, unique=True
    ),
)
def test_property_18_decision_generation_gates(
    findings: list[Finding], preexisting_open_keys: list[str]
) -> None:
    # Feature: clinic-front-desk-agent, Property 18: Decision generation gates
    # (threshold, actionability, dedup) — a new Decision is generated for a
    # finding iff the finding is actionable, its supporting-record count is >= 5,
    # and no open Decision already shares its findingKey; consequently at most
    # one open Decision exists per findingKey.
    # **Validates: Requirements 13.3, 13.4, 13.6**
    store = MemoryDecisionStore()

    # Seed one pre-existing OPEN Decision per generated key (distinct keys, so
    # the seed itself never violates the at-most-one-open-per-key invariant).
    for i, key in enumerate(preexisting_open_keys):
        seeded = store.create(
            Decision(
                id=f"pre-{i}",
                kind=DecisionKind.UNMET_DEMAND,
                finding_key=key,
                summary="pre-existing",
                recommended_action="",
                supporting_record_count=MIN_SUPPORTING_RECORDS,
                status=DecisionStatus.OPEN,
                generated_at="2025-05-01T00:00:00+00:00",
            )
        )
        assert is_ok(seeded)

    # Reference computation of the "iff": a finding is generated exactly when it
    # passes all three gates, where the dedupe gate accounts for both the
    # pre-existing open keys and keys created earlier in this same run.
    blocked = set(preexisting_open_keys)
    created_this_run: set[str] = set()
    expected_created_keys: list[str] = []
    for f in findings:
        gate_pass = (
            f.actionable
            and f.supporting_record_count >= MIN_SUPPORTING_RECORDS
            and f.key not in blocked
            and f.key not in created_this_run
        )
        if gate_pass:
            expected_created_keys.append(f.key)
            created_this_run.add(f.key)

    result = _make_synthesizer(store).synthesize(Ok(findings))

    # Generation happened exactly for the findings passing the iff.
    assert [d.finding_key for d in result.created] == expected_created_keys
    for decision in result.created:
        assert decision.status == DecisionStatus.OPEN

    # Consequence: at most one open Decision exists per findingKey.
    open_feed = store.list_open().unwrap()
    open_key_counts: dict[str, int] = {}
    for d in open_feed:
        open_key_counts[d.finding_key] = open_key_counts.get(d.finding_key, 0) + 1
    assert all(n == 1 for n in open_key_counts.values())

    # Every key that had a pre-existing open Decision is still open exactly once,
    # and every newly created key is now open exactly once.
    for key in set(preexisting_open_keys) | set(expected_created_keys):
        assert open_key_counts.get(key) == 1


# ---------------------------------------------------------------------------
# Property 28 — Analysis-failure produces no Decisions (task 10.5, Req 13.7).
# ---------------------------------------------------------------------------

# Generate a variety of ToolError shapes an analysis run could fail with.
_tool_error_strategy = st.one_of(
    st.builds(StoreFailure, store=st.text(min_size=1, max_size=12), detail=st.text(max_size=40)),
    st.builds(NotFound, detail=st.text(max_size=40)),
    st.builds(Validation, field=st.text(min_size=1, max_size=12), detail=st.text(max_size=40)),
    st.builds(Ambiguous, candidates=st.lists(st.text(max_size=8), max_size=4)),
)


@pytest.mark.property
@settings(max_examples=200)
@given(
    error=_tool_error_strategy,
    # Some pre-existing decisions that must remain untouched by a failed run.
    preexisting_open_keys=st.lists(
        st.sampled_from(_KEY_POOL), min_size=0, max_size=5, unique=True
    ),
)
def test_property_28_analysis_failure_produces_no_decisions(
    error: object, preexisting_open_keys: list[str]
) -> None:
    # Feature: clinic-front-desk-agent, Property 28: Analysis-failure produces no
    # Decisions — for any analysis run in which analyze_patterns fails, no
    # Decision is created and the analysis failure is recorded.
    # **Validates: Requirements 13.7**
    store = MemoryDecisionStore()
    for i, key in enumerate(preexisting_open_keys):
        store.create(
            Decision(
                id=f"pre-{i}",
                kind=DecisionKind.UNMET_DEMAND,
                finding_key=key,
                summary="pre-existing",
                recommended_action="",
                supporting_record_count=MIN_SUPPORTING_RECORDS,
                status=DecisionStatus.OPEN,
                generated_at="2025-05-01T00:00:00+00:00",
            )
        )
    before = {d.id for d in store.list_open().unwrap()}

    recorded: list[AnalysisFailure] = []
    synth = DecisionSynthesizer(
        store,
        clock=lambda: PINNED_CLOCK,
        id_gen=lambda: "should-not-be-used",
        failure_recorder=recorded.append,
    )

    result = synth.synthesize(Err(error))  # type: ignore[arg-type]

    # No Decision generated on the analysis-failure path (Req 13.7).
    assert result.created == []
    # The failure is recorded — both on the result and through the sink.
    assert result.analysis_failed is True
    assert result.failure is not None
    assert result.failure.recorded_at == PINNED_CLOCK
    assert result.failure.error is error
    assert len(recorded) == 1
    assert recorded[0] is result.failure

    # Pre-existing Decisions are left entirely unchanged (no new, none removed).
    after = {d.id for d in store.list_open().unwrap()}
    assert after == before


# ---------------------------------------------------------------------------
# Property 12 — Gap-fill generation, earliest-selection, assignment
# (task 10.6, Req 8.1, 8.2, 8.3, 8.4, 8.6).
# ---------------------------------------------------------------------------


@st.composite
def _gap_fill_case(draw: st.DrawFn) -> tuple[Slot, list[WaitlistEntry]]:
    """A random open slot plus a random waitlist state.

    Entries draw services from a shared pool (so some match the slot's service
    and some miss), mix active/inactive flags, and draw ``added_at`` from a small
    timestamp pool so equal timestamps exercise the ``seq`` tiebreak (design
    "Generators": equal ``addedAt``).
    """
    slot_service = draw(st.sampled_from(_SERVICE_POOL))
    slot = Slot(
        id="slot-under-test",
        provider_id="prov-1",
        service=slot_service,
        start="2024-05-06T09:00:00+00:00",
        end="2024-05-06T09:30:00+00:00",
        status=SlotStatus.OPEN,
    )

    added_at_pool = [
        "2024-05-01T09:00:00+00:00",
        "2024-05-01T09:00:00+00:00",  # duplicate value on purpose (tiebreak)
        "2024-05-02T09:00:00+00:00",
        "2024-05-03T09:00:00+00:00",
    ]

    n = draw(st.integers(min_value=0, max_value=8))
    entries: list[WaitlistEntry] = []
    for i in range(n):
        entries.append(
            WaitlistEntry(
                id=f"wl-{i}",
                patient_id=f"pat-{i}",
                service=draw(st.sampled_from(_SERVICE_POOL)),
                preferred_slot_type="any",
                added_at=draw(st.sampled_from(added_at_pool)),
                seq=0,  # the store assigns its own monotonic seq on add
                active=draw(st.booleans()),
            )
        )
    return slot, entries


@pytest.mark.property
@settings(max_examples=200)
@given(case=_gap_fill_case())
def test_property_12_gap_fill_generation_selection_and_assignment(
    case: tuple[Slot, list[WaitlistEntry]],
) -> None:
    # Feature: clinic-front-desk-agent, Property 12: Gap-fill generation,
    # earliest-selection, and assignment — a gap-fill Decision is generated iff
    # at least one active waitlist entry requests the slot's service; when
    # executed, the earliest (addedAt, then seq) matching patient is booked into
    # the slot and their waitlist entry is removed; when nothing matches, no
    # appointment is created, the slot stays open, and no fill Decision exists.
    # **Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.6**
    slot, entries = case

    waitlist_store = MemoryWaitlistStore()
    appointment_store = MemoryAppointmentStore()
    appointment_store.seed_slot(slot)
    # Add in list order; the store assigns a strictly-increasing seq per add, so
    # for equal added_at the earlier-inserted entry sorts first.
    for entry in entries:
        assert is_ok(waitlist_store.add(entry))

    # Reference: the active entries whose service equals the slot's service, in
    # insertion order (the tiebreak the store uses for equal added_at).
    matching = [
        (idx, e)
        for idx, e in enumerate(entries)
        if e.active and e.service == slot.service
    ]
    has_match = len(matching) > 0

    # --- Generation (detect_gap_fill): a fill finding exists iff there is a match.
    findings = detect_gap_fill(PatternInput(slots=[slot], waitlist=entries))
    fill_findings = [f for f in findings if f.key == f"gap_fill#{slot.id}"]

    if not has_match:
        # No match: no fill Decision generated (Req 8.6).
        assert fill_findings == []

        # Execution takes no action: NotFound, slot stays open, no appointment.
        result = fill_gap_from_waitlist(
            waitlist_store,
            appointment_store,
            slot_id=slot.id,
            appointment_id="appt-1",
        )
        assert is_err(result)
        assert isinstance(result.error, NotFound)
        assert appointment_store.get_slot(slot.id).unwrap().status == SlotStatus.OPEN
        assert appointment_store.get("appt-1").unwrap() is None
        return

    # Match present: exactly one fill finding, backed by the active-match count.
    assert len(fill_findings) == 1
    assert fill_findings[0].kind == DecisionKind.GAP_FILL
    assert fill_findings[0].supporting_record_count == len(matching)
    assert fill_findings[0].actionable is True

    # Earliest position by (added_at, insertion index) is the expected selection.
    expected_idx, expected_entry = min(
        matching, key=lambda pair: (pair[1].added_at, pair[0])
    )

    # --- Execution (fill_gap_from_waitlist): earliest-selection + assignment.
    result = fill_gap_from_waitlist(
        waitlist_store,
        appointment_store,
        slot_id=slot.id,
        appointment_id="appt-1",
    )
    assert is_ok(result)
    appointment = result.value.appointment
    assert isinstance(appointment, Appointment)

    # The selected patient is the earliest matching waitlisted patient (Req 8.2).
    assert appointment.patient_id == expected_entry.patient_id
    assert result.value.removed_waitlist_entry_id == expected_entry.id

    # An appointment associates that patient with the slot (Req 8.3).
    assert appointment.slot_id == slot.id
    assert appointment.provider_id == slot.provider_id
    assert appointment.service == slot.service
    assert appointment_store.get_slot(slot.id).unwrap().status == SlotStatus.BOOKED

    # The selected patient's matching waitlist entry is removed (Req 8.4); it no
    # longer appears among the active entries for the service.
    remaining = waitlist_store.list_by_service_ordered(slot.service).unwrap()
    assert expected_entry.id not in {e.id for e in remaining}
