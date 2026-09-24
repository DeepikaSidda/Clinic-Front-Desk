"""Dashboard correctness properties (tasks 12.3, 12.5, 12.7, 12.9).

Four Hypothesis property tests, each a single design correctness property run
>= 100 iterations against the pure dashboard components and the in-memory fake
stores (fast, deterministic):

- Property 20: Decision resolution outcomes (Req 14.3, 14.4, 14.6) — uses the
  in-memory fakes plus the fault-injection wrapper (``data_layer/faults.py``).
- Property 23: Role-scoped access (Req 15.5, 15.7).
- Property 21: Impact metrics computation and trend (Req 15.3) — checked against
  an independent reference oracle built in this test.
- Property 22: Activity log content and ordering (Req 15.2).

All helpers are defined locally so this file does not touch any existing tests.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from clinic_front_desk.dashboard.activity_log import (
    InteractionType,
    build_activity_log,
)
from clinic_front_desk.dashboard.decisions import (
    DecisionActionOutcome,
    DecisionActionService,
)
from clinic_front_desk.dashboard.metrics import (
    AVERAGE_CALL_HANDLING_MINUTES,
    compute_impact_metrics,
)
from clinic_front_desk.dashboard.role_gate import (
    DashboardView,
    Role,
    RoleGate,
)
from clinic_front_desk.data_layer.faults import fail_on, wrap
from clinic_front_desk.data_layer.memory import (
    MemoryAppointmentStore,
    MemoryDecisionStore,
    MemoryWaitlistStore,
)
from clinic_front_desk.models import (
    Appointment,
    AppointmentStatus,
    CallOutcome,
    CallSession,
    Decision,
    DecisionKind,
    DecisionStatus,
    Escalation,
    EscalationReason,
    PatientRef,
    Slot,
    SlotStatus,
    WaitlistEntry,
)

pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

_IDENT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    min_size=1,
    max_size=8,
)


# ===========================================================================
# Property 20: Decision resolution outcomes (Req 14.3, 14.4, 14.6)
# ===========================================================================
# Feature: clinic-front-desk-agent, Property 20: For any open Decision, approval
# records status `approved` and executes its associated action, while dismissal
# records status `dismissed` and executes no action. If an approved Decision's
# action fails to persist, the Decision remains open (action-failed) and no
# partial action effect is retained.
# Validates: Requirements 14.3, 14.4, 14.6

_PROVIDER = "prov1"
_GAP_SERVICE = "ent"
_SLOT_DAY = "2025-06-01"
_GAP_FAULTS = st.sampled_from(
    [None, "appointment_create", "slot_claim", "waitlist_remove"]
)


@settings(max_examples=150)
@given(
    kind=st.sampled_from(list(DecisionKind)),
    action=st.sampled_from(["approve", "dismiss"]),
    waitlist_present=st.booleans(),
    fault_choice=_GAP_FAULTS,
    key=_IDENT,
)
def test_property_20_decision_resolution_outcomes(
    kind: DecisionKind,
    action: str,
    waitlist_present: bool,
    fault_choice: str | None,
    key: str,
) -> None:
    decision_store = MemoryDecisionStore()
    real_appt = MemoryAppointmentStore()
    real_wait = MemoryWaitlistStore()

    slot_id = f"slot-{key}"
    is_gap = kind is DecisionKind.GAP_FILL

    if is_gap:
        real_appt.seed_slot(
            Slot(
                id=slot_id,
                provider_id=_PROVIDER,
                service=_GAP_SERVICE,
                start=f"{_SLOT_DAY}T09:00:00Z",
                end=f"{_SLOT_DAY}T09:30:00Z",
                status=SlotStatus.OPEN,
            )
        )
        if waitlist_present:
            real_wait.add(
                WaitlistEntry(
                    id=f"w-{key}",
                    patient_id=f"pat-{key}",
                    service=_GAP_SERVICE,
                    preferred_slot_type="any",
                    added_at="2025-05-01T00:00:00Z",
                    seq=0,
                )
            )

    decision = Decision(
        id=f"d-{key}",
        kind=kind,
        finding_key=f"k-{key}",
        summary="s",
        recommended_action="a",
        action_payload={"slot_id": slot_id} if is_gap else {},
        supporting_record_count=6,
        status=DecisionStatus.OPEN,
        generated_at="2025-06-02T00:00:00Z",
    )
    decision_store.create(decision)

    # Inject a persistence fault only for the gap-fill approve path that actually
    # runs an action (a matching waitlisted patient exists).
    appt_store: object = real_appt
    wait_store: object = real_wait
    apply_fault = (
        is_gap
        and action == "approve"
        and waitlist_present
        and fault_choice is not None
    )
    if apply_fault:
        if fault_choice == "appointment_create":
            appt_store = wrap(real_appt, fail_on("create"))
        elif fault_choice == "slot_claim":
            # ``claim_slot``, not ``set_slot_status``: booking a slot is now a
            # conditional open -> booked claim, so that is the write that can fail
            # here. Pointed at the old method this fault stopped firing at all, and
            # the property quietly stopped testing anything.
            appt_store = wrap(real_appt, fail_on("claim_slot"))
        else:  # waitlist_remove
            wait_store = wrap(real_wait, fail_on("remove"))

    counter = itertools.count()
    service = DecisionActionService(
        decision_store=decision_store,
        appointment_store=appt_store,  # type: ignore[arg-type]
        waitlist_store=wait_store,  # type: ignore[arg-type]
        clock=lambda: "2025-06-05T00:00:00Z",
        id_gen=lambda: f"appt-{next(counter)}",
    )

    def open_ids() -> set[str]:
        return {d.id for d in decision_store.list_open().unwrap()}

    def appointments_on_day() -> list[Appointment]:
        return real_appt.list_by_provider_and_day(_PROVIDER, _SLOT_DAY).unwrap()

    def slot_status() -> SlotStatus:
        return real_appt.get_slot(slot_id).unwrap().status

    def waitlist_count() -> int:
        return len(real_wait.list_by_service_ordered(_GAP_SERVICE).unwrap())

    if action == "dismiss":
        result = service.dismiss(decision.id)
        # Dismissal records `dismissed` and executes no action (Req 14.4).
        assert result.outcome is DecisionActionOutcome.DISMISSED
        assert result.decision is not None
        assert result.decision.status is DecisionStatus.DISMISSED
        assert decision.id not in open_ids()  # left the open feed
        if is_gap:
            assert appointments_on_day() == []  # no action executed
            assert slot_status() is SlotStatus.OPEN
            assert waitlist_count() == (1 if waitlist_present else 0)
        return

    # action == "approve"
    result = service.approve(decision.id)

    if not is_gap:
        # Advisory Decision: approval recorded, no Data_Layer action (Req 14.3).
        assert result.outcome is DecisionActionOutcome.APPROVED
        assert result.decision is not None
        assert result.decision.status is DecisionStatus.APPROVED
        assert result.gap_fill is None
        assert decision.id not in open_ids()
        return

    # gap_fill approve
    if not waitlist_present:
        # No matching waitlisted patient -> action cannot complete: Decision
        # stays open, no partial effect (Req 14.6).
        assert result.outcome is DecisionActionOutcome.ACTION_FAILED
        assert decision.id in open_ids()
        assert appointments_on_day() == []
        assert slot_status() is SlotStatus.OPEN
        assert waitlist_count() == 0
    elif fault_choice is None:
        # Action executes: books the waitlisted patient, releases the entry
        # (Req 14.3), and the Decision leaves the open feed.
        assert result.outcome is DecisionActionOutcome.APPROVED
        assert result.gap_fill is not None
        assert decision.id not in open_ids()
        appts = appointments_on_day()
        assert len(appts) == 1
        assert appts[0].slot_id == slot_id
        assert slot_status() is SlotStatus.BOOKED
        assert waitlist_count() == 0  # entry removed
    else:
        # Action fails to persist: Decision remains open (action-failed) and no
        # partial effect is retained (Req 14.6) — slot open, entry intact, no
        # appointment.
        assert result.outcome is DecisionActionOutcome.ACTION_FAILED
        assert result.error is not None
        assert decision.id in open_ids()
        assert appointments_on_day() == []
        assert slot_status() is SlotStatus.OPEN
        assert waitlist_count() == 1


# ===========================================================================
# Property 23: Role-scoped access (Req 15.5, 15.7)
# ===========================================================================
# Feature: clinic-front-desk-agent, Property 23: For any viewer with an assigned
# role, the presented set of views equals exactly the set permitted for that
# role; for any viewer without an assigned role, access is denied and no
# schedule, activity, or metrics data is returned.
# Validates: Requirements 15.5, 15.7

_VIEWS = list(DashboardView)
_ROLE_VIEW_SUBSET = st.lists(
    st.sampled_from(_VIEWS), unique=True, max_size=len(_VIEWS)
).map(frozenset)
_ROLE_MAP = st.dictionaries(
    keys=st.sampled_from(list(Role)),
    values=_ROLE_VIEW_SUBSET,
    max_size=len(Role),
)
# A viewer is a real Role, a role's string value, an unrecognized string, or
# None (no assigned role).
_VIEWER = st.one_of(
    st.sampled_from(list(Role)),
    st.sampled_from([r.value for r in Role]),
    st.text(min_size=0, max_size=6),
    st.none(),
)


def _expected_access(
    role_map: dict[Role, frozenset[DashboardView]],
    viewer: Role | str | None,
) -> frozenset[DashboardView] | None:
    """Independent oracle: permitted views for an assigned+mapped role, else
    ``None`` meaning access is denied (no role, or unrecognized/unmapped)."""
    if viewer is None:
        return None
    if isinstance(viewer, Role):
        coerced: Role | None = viewer
    else:
        try:
            coerced = Role(viewer)
        except ValueError:
            coerced = None
    if coerced is None:
        return None
    return role_map.get(coerced)


@settings(max_examples=150)
@given(role_map=_ROLE_MAP, viewer=_VIEWER)
def test_property_23_role_scoped_access(
    role_map: dict[Role, frozenset[DashboardView]],
    viewer: Role | str | None,
) -> None:
    gate = RoleGate(role_map)
    expected = _expected_access(role_map, viewer)

    decision = gate.resolve(viewer)
    data = {view: f"payload-{view.value}" for view in _VIEWS}
    filtered = gate.filter_data(viewer, data)

    if expected is None:
        # No assigned (recognized, mapped) role -> denied, no data returned.
        assert decision.granted is False
        assert decision.permitted_views == frozenset()
        assert gate.permitted_views(viewer) == frozenset()
        assert filtered == {}
        for view in _VIEWS:
            assert gate.is_permitted(viewer, view) is False
    else:
        # Assigned role -> presented views equal exactly the permitted set.
        assert decision.granted is True
        assert decision.permitted_views == expected
        assert gate.permitted_views(viewer) == expected
        # filter_data returns exactly the permitted views' payloads, unaltered.
        assert set(filtered.keys()) == set(expected)
        for view in expected:
            assert filtered[view] == data[view]
        for view in _VIEWS:
            assert gate.is_permitted(viewer, view) is (view in expected)


# ===========================================================================
# Property 21: Impact metrics computation and trend (Req 15.3)
# ===========================================================================
# Feature: clinic-front-desk-agent, Property 21: For any dataset and reporting
# window in {7, 30, 90} days, the computed hours saved, waitlist-recovered
# count, and no-show rate equal their reference computations over that window,
# and the no-show-rate trend equals the current-period rate minus the rate over
# the immediately preceding period of equal length.
# Validates: Requirements 15.3

_NOW = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
_WINDOW = st.sampled_from([7, 30, 90])
# Day offsets back from `now`, spanning the current, preceding, and out-of-range
# regions for every window.
_OFFSET = st.integers(min_value=0, max_value=210)
_OPT_OUTCOME = st.sampled_from([None, *list(CallOutcome)])


def _in(instant: datetime, start: datetime, end: datetime) -> bool:
    """Half-open membership ``(start, end]`` — the reference period rule."""
    return start < instant <= end


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


@settings(max_examples=120)
@given(
    appts=st.lists(
        st.tuples(st.sampled_from(list(AppointmentStatus)), _OFFSET),
        max_size=20,
    ),
    sessions=st.lists(st.tuples(_OPT_OUTCOME, _OFFSET), max_size=20),
    decisions=st.lists(
        st.tuples(
            st.sampled_from(list(DecisionKind)),
            st.sampled_from(list(DecisionStatus)),
            st.booleans(),  # has resolved_at
            _OFFSET,
        ),
        max_size=20,
    ),
    window_days=_WINDOW,
)
def test_property_21_impact_metrics_and_trend(
    appts: list[tuple[AppointmentStatus, int]],
    sessions: list[tuple[CallOutcome | None, int]],
    decisions: list[tuple[DecisionKind, DecisionStatus, bool, int]],
    window_days: int,
) -> None:
    # --- Build records from the generated data ---
    appointments: list[Appointment] = []
    for i, (status, offset) in enumerate(appts):
        day = (_NOW - timedelta(days=offset)).date().isoformat()
        appointments.append(
            Appointment(
                id=f"a{i}",
                provider_id=_PROVIDER,
                patient_id=f"p{i}",
                service=_GAP_SERVICE,
                slot_id=f"s{i}",
                date=day,
                time="09:00",
                status=status,
            )
        )

    call_sessions: list[CallSession] = []
    for i, (outcome, offset) in enumerate(sessions):
        started = (_NOW - timedelta(days=offset)).isoformat()
        call_sessions.append(
            CallSession(id=f"c{i}", started_at=started, outcome=outcome)
        )

    decision_records: list[Decision] = []
    for i, (kind, status, has_resolved, offset) in enumerate(decisions):
        resolved = (
            (_NOW - timedelta(days=offset)).isoformat() if has_resolved else None
        )
        decision_records.append(
            Decision(
                id=f"d{i}",
                kind=kind,
                finding_key=f"fk{i}",
                summary="s",
                recommended_action="a",
                supporting_record_count=6,
                status=status,
                generated_at="2025-01-01T00:00:00+00:00",
                resolved_at=resolved,
            )
        )

    # --- Independent reference oracle ---
    width = timedelta(days=window_days)
    cur_start, cur_end = _NOW - width, _NOW
    prec_start, prec_end = _NOW - 2 * width, _NOW - width

    handled = sum(
        1
        for outcome, offset in sessions
        if outcome is not None
        and outcome is not CallOutcome.INTERRUPTED
        and _in(_NOW - timedelta(days=offset), cur_start, cur_end)
    )
    expected_hours = handled * AVERAGE_CALL_HANDLING_MINUTES / 60.0

    expected_recovered = sum(
        1
        for kind, status, has_resolved, offset in decisions
        if kind is DecisionKind.GAP_FILL
        and status is DecisionStatus.APPROVED
        and has_resolved
        and _in(_NOW - timedelta(days=offset), cur_start, cur_end)
    )

    def rate(start: datetime, end: datetime) -> float:
        attended = 0
        no_shows = 0
        for status, offset in appts:
            if status not in (
                AppointmentStatus.COMPLETED,
                AppointmentStatus.NO_SHOW,
            ):
                continue
            instant = _parse((_NOW - timedelta(days=offset)).date().isoformat())
            if not _in(instant, start, end):
                continue
            attended += 1
            if status is AppointmentStatus.NO_SHOW:
                no_shows += 1
        return (no_shows / attended) if attended else 0.0

    expected_current_rate = rate(cur_start, cur_end)
    expected_preceding_rate = rate(prec_start, prec_end)

    # --- Compute + compare ---
    metrics = compute_impact_metrics(
        appointments=appointments,
        call_sessions=call_sessions,
        recovered_decisions=decision_records,
        window_days=window_days,
        now=_NOW,
    )

    assert metrics.window_days == window_days
    assert metrics.front_desk_hours_saved == pytest.approx(expected_hours)
    assert metrics.waitlist_recovered_count == expected_recovered
    assert metrics.no_show_rate == pytest.approx(expected_current_rate)
    assert metrics.no_show_rate_trend == pytest.approx(
        expected_current_rate - expected_preceding_rate
    )


# ===========================================================================
# Property 22: Activity log content and ordering (Req 15.2)
# ===========================================================================
# Feature: clinic-front-desk-agent, Property 22: For any set of call sessions and
# escalations, the activity log lists entries ordered most-recent-first, and each
# entry exposes its interaction type (booked, rescheduled, cancelled, or
# escalated), its date-time, and the associated patient identifier.
# Validates: Requirements 15.2

_TS_POOL = st.sampled_from(
    [
        "2025-06-01T09:00:00+00:00",
        "2025-06-01T10:00:00+00:00",
        "2025-06-02T09:00:00+00:00",
        "2025-06-03T12:30:00+00:00",
        "2025-06-04T08:15:00+00:00",
    ]
)
_OPT_TEXT = st.one_of(st.none(), st.text(min_size=1, max_size=6))
_PATIENT_REF = st.builds(
    PatientRef,
    patient_id=_OPT_TEXT,
    name=_OPT_TEXT,
    callback_phone=_OPT_TEXT,
)
_OUTCOME_TO_TYPE = {
    CallOutcome.BOOKED: InteractionType.BOOKED,
    CallOutcome.RESCHEDULED: InteractionType.RESCHEDULED,
    CallOutcome.CANCELLED: InteractionType.CANCELLED,
}


def _identifier(ref: PatientRef) -> str | None:
    for candidate in (ref.patient_id, ref.callback_phone, ref.name):
        if candidate:
            return candidate
    return None


@settings(max_examples=120)
@given(
    sessions=st.lists(
        st.tuples(
            _IDENT,
            st.sampled_from([None, *list(CallOutcome)]),
            _TS_POOL,  # started_at
            st.one_of(st.none(), _TS_POOL),  # ended_at
            _PATIENT_REF,
        ),
        max_size=15,
        unique_by=lambda t: t[0],
    ),
    escalations=st.lists(
        st.tuples(_IDENT, st.sampled_from(list(EscalationReason)), _TS_POOL, _PATIENT_REF),
        max_size=15,
        unique_by=lambda t: t[0],
    ),
)
def test_property_22_activity_log_content_and_ordering(
    sessions: list[tuple[str, CallOutcome | None, str, str | None, PatientRef]],
    escalations: list[tuple[str, EscalationReason, str, PatientRef]],
) -> None:
    session_records = [
        CallSession(
            id=f"s-{sid}",
            started_at=started,
            ended_at=ended,
            outcome=outcome,
            patient_ref=ref,
        )
        for sid, outcome, started, ended, ref in sessions
    ]
    escalation_records = [
        Escalation(
            id=f"e-{eid}",
            reason=reason,
            call_session_id="call",
            context="ctx",
            created_at=created,
            patient_ref=ref,
        )
        for eid, reason, created, ref in escalations
    ]

    # --- Independent reference oracle ---
    expected: list[tuple[InteractionType, str, str | None, str]] = []
    for sid, outcome, started, ended, ref in sessions:
        interaction = _OUTCOME_TO_TYPE.get(outcome) if outcome is not None else None
        if interaction is None:
            continue  # waitlisted/escalated/no_action/interrupted/None -> no entry
        timestamp = ended if ended else started
        expected.append((interaction, timestamp, _identifier(ref), f"s-{sid}"))
    for eid, reason, created, ref in escalations:
        expected.append(
            (InteractionType.ESCALATED, created, _identifier(ref), f"e-{eid}")
        )
    # Most-recent-first, ties broken by source id descending (stable).
    expected.sort(key=lambda e: (e[1], e[3]), reverse=True)

    log = build_activity_log(session_records, escalation_records)

    assert len(log) == len(expected)
    for entry, (interaction, timestamp, identifier, source_id) in zip(log, expected):
        assert entry.interaction_type is interaction
        assert entry.interaction_type in set(InteractionType)
        assert entry.timestamp == timestamp
        assert entry.patient_identifier == identifier
        assert entry.source_id == source_id

    # Ordering invariant: timestamps are non-increasing down the log.
    assert all(
        log[i].timestamp >= log[i + 1].timestamp for i in range(len(log) - 1)
    )
