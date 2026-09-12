"""Unit tests for the decision approve/dismiss execution service (task 12.2).

Covers :class:`~clinic_front_desk.dashboard.decisions.DecisionActionService`:

- approve of a ``gap_fill`` Decision executes ``fill_gap_from_waitlist`` (books
  the earliest waitlisted patient, removes their entry) and records ``APPROVED``,
  so the Decision leaves the open feed (Req 14.3, 14.5, 8.2, 8.3, 8.4).
- approve of an advisory (non-gap-fill) Decision records ``APPROVED`` with no
  Data_Layer action (Req 14.3).
- approve where the action fails leaves the Decision open with an error
  indication and no partial effect (Req 14.6, 8.5).
- dismiss records ``DISMISSED`` and executes no action (Req 14.4).
- unknown / already-resolved ids and store-read failures are classified.

These are focused example/edge tests; the exhaustive property test lives in
task 12.3 (Property 20).
"""

from __future__ import annotations

from clinic_front_desk.dashboard.decisions import (
    DecisionActionOutcome,
    DecisionActionService,
)
from clinic_front_desk.data_layer.faults import fail_on, wrap
from clinic_front_desk.data_layer.memory import (
    MemoryAppointmentStore,
    MemoryDecisionStore,
    MemoryWaitlistStore,
)
from clinic_front_desk.models import (
    Decision,
    DecisionKind,
    DecisionStatus,
    Slot,
    SlotStatus,
    WaitlistEntry,
    is_ok,
)

FIXED_NOW = "2025-06-05T00:00:00Z"


def _clock() -> str:
    return FIXED_NOW


def _seed_decision(
    store: MemoryDecisionStore,
    *,
    decision_id: str,
    kind: DecisionKind,
    finding_key: str,
    action_payload: dict | None = None,
    generated_at: str = "2025-06-01T09:00:00Z",
) -> Decision:
    result = store.create(
        Decision(
            id=decision_id,
            kind=kind,
            finding_key=finding_key,
            summary="summary",
            recommended_action="do the thing",
            action_payload=action_payload or {},
            supporting_record_count=5,
            status=DecisionStatus.OPEN,
            generated_at=generated_at,
        )
    )
    assert is_ok(result)
    return result.value


def _open_slot(
    store: MemoryAppointmentStore,
    *,
    slot_id: str,
    provider_id: str,
    service: str,
    start: str,
) -> Slot:
    slot = Slot(
        id=slot_id,
        provider_id=provider_id,
        service=service,
        start=start,
        end=start,
        status=SlotStatus.OPEN,
    )
    store.seed_slot(slot)
    return slot


def _seed_entry(
    store: MemoryWaitlistStore,
    *,
    entry_id: str,
    patient_id: str,
    service: str,
    added_at: str,
) -> WaitlistEntry:
    result = store.add(
        WaitlistEntry(
            id=entry_id,
            patient_id=patient_id,
            service=service,
            preferred_slot_type="any",
            added_at=added_at,
            seq=0,
            active=True,
        )
    )
    assert is_ok(result)
    return result.value


def _service(
    *,
    decisions: MemoryDecisionStore | None = None,
    appointments: MemoryAppointmentStore | None = None,
    waitlist: MemoryWaitlistStore | None = None,
    id_gen=None,
) -> DecisionActionService:
    return DecisionActionService(
        decision_store=decisions or MemoryDecisionStore(),
        appointment_store=appointments or MemoryAppointmentStore(),
        waitlist_store=waitlist or MemoryWaitlistStore(),
        clock=_clock,
        id_gen=id_gen,
    )


# -- approve: gap_fill -----------------------------------------------------


def test_approve_gap_fill_executes_action_and_records_approved() -> None:
    """Req 14.3, 14.5, 8.2, 8.3, 8.4: approving a gap_fill Decision books the
    earliest waitlisted patient, removes their entry, records APPROVED, and the
    Decision leaves the open feed."""
    decisions = MemoryDecisionStore()
    appts = MemoryAppointmentStore()
    wl = MemoryWaitlistStore()
    _open_slot(appts, slot_id="s1", provider_id="prov1", service="ent",
               start="2025-06-10T14:30:00Z")
    _seed_entry(wl, entry_id="w_early", patient_id="p_early", service="ent",
                added_at="2025-06-01T09:00:00Z")
    _seed_entry(wl, entry_id="w_late", patient_id="p_late", service="ent",
                added_at="2025-06-02T09:00:00Z")
    _seed_decision(decisions, decision_id="d1", kind=DecisionKind.GAP_FILL,
                   finding_key="gap_fill#s1", action_payload={"slot_id": "s1"})

    svc = _service(decisions=decisions, appointments=appts, waitlist=wl,
                   id_gen=lambda: "a1")
    result = svc.approve("d1")

    assert result.outcome == DecisionActionOutcome.APPROVED
    assert result.ok is True
    # Gap-fill executed against the earliest match (Req 8.2, 8.3, 8.4).
    assert result.gap_fill is not None
    assert result.gap_fill.removed_waitlist_entry_id == "w_early"
    assert result.gap_fill.appointment.patient_id == "p_early"
    assert result.gap_fill.appointment.slot_id == "s1"
    assert appts.get_slot("s1").unwrap().status == SlotStatus.BOOKED
    assert [e.id for e in wl.list_by_service_ordered("ent").unwrap()] == ["w_late"]
    # Approval recorded and Decision removed from the open feed (Req 14.3, 14.5).
    assert result.decision.status == DecisionStatus.APPROVED
    assert result.decision.resolved_at == FIXED_NOW
    assert decisions.list_open().unwrap() == []


def test_approve_gap_fill_action_failure_keeps_decision_open() -> None:
    """Req 14.6, 8.5: when the gap-fill action fails to persist, the Decision is
    left open with an error indication and no partial effect."""
    decisions = MemoryDecisionStore()
    appts_base = MemoryAppointmentStore()
    wl = MemoryWaitlistStore()
    _open_slot(appts_base, slot_id="s1", provider_id="prov1", service="ent",
               start="2025-06-10T14:30:00Z")
    _seed_entry(wl, entry_id="w1", patient_id="p1", service="ent",
                added_at="2025-06-01T09:00:00Z")
    _seed_decision(decisions, decision_id="d1", kind=DecisionKind.GAP_FILL,
                   finding_key="gap_fill#s1", action_payload={"slot_id": "s1"})
    # Force the booking write to fail.
    appts = wrap(appts_base, fail_on("create"))

    svc = _service(decisions=decisions, appointments=appts, waitlist=wl)
    result = svc.approve("d1")

    assert result.outcome == DecisionActionOutcome.ACTION_FAILED
    assert result.ok is False
    assert result.error is not None
    # No partial effect: slot open, no appointment, entry retained (Req 8.5).
    assert appts_base.get_slot("s1").unwrap().status == SlotStatus.OPEN
    assert [e.id for e in wl.list_by_service_ordered("ent").unwrap()] == ["w1"]
    # Decision remains open in the feed (Req 14.6).
    open_ids = [d.id for d in decisions.list_open().unwrap()]
    assert open_ids == ["d1"]
    assert decisions.list_open().unwrap()[0].status == DecisionStatus.OPEN


def test_approve_gap_fill_no_matching_waitlist_keeps_decision_open() -> None:
    """Req 14.6, 8.6: no waitlisted patient for the slot's service means the
    action does not complete; the Decision stays open."""
    decisions = MemoryDecisionStore()
    appts = MemoryAppointmentStore()
    wl = MemoryWaitlistStore()
    _open_slot(appts, slot_id="s1", provider_id="prov1", service="ent",
               start="2025-06-10T14:30:00Z")
    _seed_decision(decisions, decision_id="d1", kind=DecisionKind.GAP_FILL,
                   finding_key="gap_fill#s1", action_payload={"slot_id": "s1"})

    svc = _service(decisions=decisions, appointments=appts, waitlist=wl)
    result = svc.approve("d1")

    assert result.outcome == DecisionActionOutcome.ACTION_FAILED
    assert appts.get_slot("s1").unwrap().status == SlotStatus.OPEN
    assert [d.id for d in decisions.list_open().unwrap()] == ["d1"]


def test_approve_gap_fill_missing_slot_id_is_action_failed() -> None:
    """A malformed gap_fill Decision without a slot id cannot execute; it stays
    open as an action failure (Req 14.6)."""
    decisions = MemoryDecisionStore()
    _seed_decision(decisions, decision_id="d1", kind=DecisionKind.GAP_FILL,
                   finding_key="gap_fill#s1", action_payload={})

    svc = _service(decisions=decisions)
    result = svc.approve("d1")

    assert result.outcome == DecisionActionOutcome.ACTION_FAILED
    assert [d.id for d in decisions.list_open().unwrap()] == ["d1"]


# -- approve: advisory (non gap_fill) --------------------------------------


def test_approve_advisory_decision_records_approved_without_action() -> None:
    """Req 14.3: an advisory Decision has no automated Data_Layer action; approve
    records APPROVED and removes it from the open feed."""
    decisions = MemoryDecisionStore()
    _seed_decision(decisions, decision_id="d1", kind=DecisionKind.NO_SHOW_TREND,
                   finding_key="no_show_trend#prov1")

    svc = _service(decisions=decisions)
    result = svc.approve("d1")

    assert result.outcome == DecisionActionOutcome.APPROVED
    assert result.gap_fill is None
    assert result.decision.status == DecisionStatus.APPROVED
    assert decisions.list_open().unwrap() == []


# -- dismiss ---------------------------------------------------------------


def test_dismiss_records_dismissed_without_action() -> None:
    """Req 14.4: dismiss records DISMISSED and executes no action; the Decision
    leaves the open feed."""
    decisions = MemoryDecisionStore()
    appts = MemoryAppointmentStore()
    wl = MemoryWaitlistStore()
    _open_slot(appts, slot_id="s1", provider_id="prov1", service="ent",
               start="2025-06-10T14:30:00Z")
    _seed_entry(wl, entry_id="w1", patient_id="p1", service="ent",
                added_at="2025-06-01T09:00:00Z")
    _seed_decision(decisions, decision_id="d1", kind=DecisionKind.GAP_FILL,
                   finding_key="gap_fill#s1", action_payload={"slot_id": "s1"})

    svc = _service(decisions=decisions, appointments=appts, waitlist=wl)
    result = svc.dismiss("d1")

    assert result.outcome == DecisionActionOutcome.DISMISSED
    assert result.decision.status == DecisionStatus.DISMISSED
    assert result.decision.resolved_at == FIXED_NOW
    # No action executed: slot still open, waitlist entry untouched (Req 14.4).
    assert appts.get_slot("s1").unwrap().status == SlotStatus.OPEN
    assert [e.id for e in wl.list_by_service_ordered("ent").unwrap()] == ["w1"]
    assert decisions.list_open().unwrap() == []


# -- locating / failure classification -------------------------------------


def test_approve_unknown_id_is_not_found() -> None:
    svc = _service()
    result = svc.approve("ghost")
    assert result.outcome == DecisionActionOutcome.NOT_FOUND
    assert result.ok is False


def test_dismiss_already_resolved_id_is_not_found() -> None:
    """A Decision that is no longer open is not in list_open, so it cannot be
    resolved again."""
    decisions = MemoryDecisionStore()
    _seed_decision(decisions, decision_id="d1", kind=DecisionKind.NO_SHOW_TREND,
                   finding_key="no_show_trend#prov1")
    svc = _service(decisions=decisions)
    assert svc.dismiss("d1").outcome == DecisionActionOutcome.DISMISSED
    # Second dismiss: already resolved, no longer open.
    assert svc.dismiss("d1").outcome == DecisionActionOutcome.NOT_FOUND


def test_approve_read_failure_is_store_error() -> None:
    decisions = wrap(MemoryDecisionStore(), fail_on("list_open"))
    svc = DecisionActionService(
        decision_store=decisions,
        appointment_store=MemoryAppointmentStore(),
        waitlist_store=MemoryWaitlistStore(),
        clock=_clock,
    )
    result = svc.approve("d1")
    assert result.outcome == DecisionActionOutcome.STORE_ERROR


def test_dismiss_set_status_failure_is_store_error() -> None:
    """A write failure while recording the dismissal surfaces as STORE_ERROR and
    leaves the Decision open (Req 16.6)."""
    base = MemoryDecisionStore()
    _seed_decision(base, decision_id="d1", kind=DecisionKind.NO_SHOW_TREND,
                   finding_key="no_show_trend#prov1")
    decisions = wrap(base, fail_on("set_status"))
    svc = DecisionActionService(
        decision_store=decisions,
        appointment_store=MemoryAppointmentStore(),
        waitlist_store=MemoryWaitlistStore(),
        clock=_clock,
    )
    result = svc.dismiss("d1")
    assert result.outcome == DecisionActionOutcome.STORE_ERROR
    # Left open.
    assert [d.id for d in base.list_open().unwrap()] == ["d1"]
