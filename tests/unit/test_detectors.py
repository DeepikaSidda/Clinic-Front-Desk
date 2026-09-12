"""Unit tests for ``PatternDetectors`` and ``analyze_patterns`` (task 10.1).

Focused example/edge tests covering each detector's trigger, its stable
``findingKey`` construction, and its ``supporting_record_count`` / ``actionable``
reporting (Req 13.2, 8.1). The exhaustive property tests (decision gates,
gap-fill selection) live in tasks 10.4/10.6 and are out of scope here.
"""

from __future__ import annotations

from clinic_front_desk.intelligence import (
    NO_SHOW_BASELINE_RATE,
    PatternInput,
    analyze_patterns,
    detect_all,
    detect_gap_fill,
    detect_no_show_trend,
    detect_schedule_gaps,
    detect_unmet_demand,
    detect_unoffered_service_demand,
)
from clinic_front_desk.models import (
    Appointment,
    AppointmentStatus,
    DecisionKind,
    Slot,
    SlotStatus,
    WaitlistEntry,
    is_ok,
)


def _appt(id: str, status: AppointmentStatus, date: str = "2024-05-10") -> Appointment:
    return Appointment(
        id=id,
        provider_id="prov-1",
        patient_id=f"pat-{id}",
        service="cleaning",
        slot_id=f"slot-{id}",
        date=date,
        time="09:00",
        status=status,
    )


def _slot(id: str, status: SlotStatus, start: str, service: str = "cleaning") -> Slot:
    return Slot(
        id=id,
        provider_id="prov-1",
        service=service,
        start=start,
        end=start,
        status=status,
    )


def _entry(id: str, service: str, seq: int, active: bool = True) -> WaitlistEntry:
    return WaitlistEntry(
        id=id,
        patient_id=f"pat-{id}",
        service=service,
        preferred_slot_type="any",
        added_at="2024-05-01T09:00:00+00:00",
        seq=seq,
        active=active,
    )


# --- No-show trend ---------------------------------------------------------


def test_no_show_trend_fires_above_baseline() -> None:
    # 3 no-shows of 6 terminal appointments = 50% > baseline.
    appts = [_appt(f"n{i}", AppointmentStatus.NO_SHOW) for i in range(3)]
    appts += [_appt(f"c{i}", AppointmentStatus.COMPLETED) for i in range(3)]
    findings = detect_no_show_trend(PatternInput(appointments=appts, window_days=30))
    assert len(findings) == 1
    f = findings[0]
    assert f.key == "no_show_trend#30d"
    assert f.kind == DecisionKind.NO_SHOW_TREND
    assert f.supporting_record_count == 6
    assert f.actionable is True


def test_no_show_trend_silent_at_or_below_baseline() -> None:
    # 1 no-show of 20 = 5% <= 15% baseline.
    appts = [_appt("n0", AppointmentStatus.NO_SHOW)]
    appts += [_appt(f"c{i}", AppointmentStatus.COMPLETED) for i in range(19)]
    assert NO_SHOW_BASELINE_RATE > 1 / 20
    assert detect_no_show_trend(PatternInput(appointments=appts)) == []


def test_no_show_trend_windowing_excludes_old_records() -> None:
    appts = [_appt(f"n{i}", AppointmentStatus.NO_SHOW, date="2024-01-01") for i in range(3)]
    appts += [_appt(f"c{i}", AppointmentStatus.COMPLETED, date="2024-01-01") for i in range(3)]
    # now is far after the appointment dates -> outside a 30-day window.
    findings = detect_no_show_trend(
        PatternInput(appointments=appts, window_days=30, now="2024-05-10")
    )
    assert findings == []


# --- Schedule gap ----------------------------------------------------------


def test_schedule_gap_fires_for_recurring_open_weekday() -> None:
    # 2024-05-06 and 2024-05-13 are Mondays; low utilization (all open).
    slots = [
        _slot("s1", SlotStatus.OPEN, "2024-05-06T09:00:00+00:00"),
        _slot("s2", SlotStatus.OPEN, "2024-05-13T09:00:00+00:00"),
    ]
    findings = detect_schedule_gaps(PatternInput(slots=slots, now="2024-05-20", window_days=30))
    assert len(findings) == 1
    f = findings[0]
    assert f.key == "schedule_gap#prov-1#dow0"  # Monday == 0
    assert f.kind == DecisionKind.SCHEDULE_GAP
    assert f.supporting_record_count == 2


def test_schedule_gap_silent_when_well_utilized() -> None:
    slots = [
        _slot("s1", SlotStatus.BOOKED, "2024-05-06T09:00:00+00:00"),
        _slot("s2", SlotStatus.BOOKED, "2024-05-13T09:00:00+00:00"),
        _slot("s3", SlotStatus.OPEN, "2024-05-20T09:00:00+00:00"),
    ]
    # utilization 2/3 = 67% > 50% threshold -> no finding.
    assert detect_schedule_gaps(PatternInput(slots=slots, now="2024-05-27")) == []


# --- Unmet demand ----------------------------------------------------------


def test_unmet_demand_fires_at_threshold() -> None:
    waitlist = [_entry(f"w{i}", "implant", seq=i) for i in range(3)]
    findings = detect_unmet_demand(PatternInput(waitlist=waitlist))
    assert len(findings) == 1
    f = findings[0]
    assert f.key == "unmet_demand#implant"
    assert f.supporting_record_count == 3
    assert f.kind == DecisionKind.UNMET_DEMAND


def test_unmet_demand_ignores_inactive_and_below_threshold() -> None:
    waitlist = [
        _entry("w0", "implant", seq=0, active=True),
        _entry("w1", "implant", seq=1, active=False),
        _entry("w2", "cleaning", seq=2, active=True),
    ]
    assert detect_unmet_demand(PatternInput(waitlist=waitlist)) == []


# --- Unoffered-service demand ---------------------------------------------


def test_unoffered_service_demand_counts_non_offered_names() -> None:
    inp = PatternInput(
        offered_services=frozenset({"cleaning", "filling"}),
        named_service_requests=["orthodontics", "orthodontics", "Cleaning", "whitening"],
    )
    findings = detect_unoffered_service_demand(inp)
    keys = {f.key: f.supporting_record_count for f in findings}
    assert keys == {
        "unoffered_service_demand#orthodontics": 2,
        "unoffered_service_demand#whitening": 1,
    }
    # "Cleaning" is offered (case-insensitive) so it is excluded.


# --- Gap fill --------------------------------------------------------------


def test_gap_fill_matches_open_slot_to_waitlist() -> None:
    slots = [
        _slot("open-1", SlotStatus.OPEN, "2024-05-06T09:00:00+00:00", service="implant"),
        _slot("booked-1", SlotStatus.BOOKED, "2024-05-06T10:00:00+00:00", service="implant"),
        _slot("open-2", SlotStatus.OPEN, "2024-05-06T11:00:00+00:00", service="cleaning"),
    ]
    waitlist = [_entry("w0", "implant", seq=0), _entry("w1", "implant", seq=1)]
    findings = detect_gap_fill(PatternInput(slots=slots, waitlist=waitlist))
    assert len(findings) == 1
    f = findings[0]
    assert f.key == "gap_fill#open-1"
    assert f.kind == DecisionKind.GAP_FILL
    assert f.supporting_record_count == 2
    assert f.action_payload["slot_id"] == "open-1"


def test_gap_fill_silent_without_matching_waitlist() -> None:
    slots = [_slot("open-1", SlotStatus.OPEN, "2024-05-06T09:00:00+00:00", service="implant")]
    assert detect_gap_fill(PatternInput(slots=slots, waitlist=[])) == []


# --- Aggregation + tool ----------------------------------------------------


def test_detect_all_aggregates_multiple_detectors() -> None:
    appts = [_appt(f"n{i}", AppointmentStatus.NO_SHOW) for i in range(3)]
    appts += [_appt(f"c{i}", AppointmentStatus.COMPLETED) for i in range(3)]
    waitlist = [_entry(f"w{i}", "implant", seq=i) for i in range(3)]
    slots = [_slot("open-1", SlotStatus.OPEN, "2024-05-06T09:00:00+00:00", service="implant")]
    inp = PatternInput(appointments=appts, slots=slots, waitlist=waitlist, window_days=30)
    kinds = {f.kind for f in detect_all(inp)}
    assert DecisionKind.NO_SHOW_TREND in kinds
    assert DecisionKind.UNMET_DEMAND in kinds
    assert DecisionKind.GAP_FILL in kinds


def test_analyze_patterns_returns_ok_with_findings() -> None:
    waitlist = [_entry(f"w{i}", "implant", seq=i) for i in range(3)]
    result = analyze_patterns(PatternInput(waitlist=waitlist))
    assert is_ok(result)
    assert any(f.key == "unmet_demand#implant" for f in result.value)


def test_analyze_patterns_empty_snapshot_ok_empty() -> None:
    result = analyze_patterns(PatternInput())
    assert is_ok(result)
    assert result.value == []
