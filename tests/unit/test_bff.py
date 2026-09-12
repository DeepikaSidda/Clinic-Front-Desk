"""Unit tests for the Dashboard BFF and its pub/sub channel (task 12.1).

Covers :mod:`clinic_front_desk.dashboard.bff` and
:mod:`clinic_front_desk.dashboard.pubsub`:

- The channel is a :class:`ChangeEmitter`: it fans successful-mutation events
  out to every connected client synchronously (Req 9.6, 14.5, 14.8, 15.4).
- Subscribe / unsubscribe / connect / close manage subscribers correctly, and a
  faulty subscriber never breaks delivery to the others (Req 16.6 emitter
  contract).
- Stores constructed with the channel broadcast their ChangeEvents to connected
  dashboard clients on mutation (the fan-out wiring).
- The BFF reads exclusively through the Data_Layer: open decisions (Req 14.1),
  schedule for a day (Req 15.1), recent activity (Req 15.2/9.6), and delegates
  metric computation to an injected computer (Req 15.3, task 12.6).
- Read failures are propagated, never partial views (Req 16.6).
"""

from __future__ import annotations

import pytest

from clinic_front_desk.dashboard.bff import (
    DashboardBFF,
    ScheduleView,
    build_memory_bff,
)
from clinic_front_desk.dashboard.pubsub import DashboardChannel
from clinic_front_desk.data_layer.events import (
    ChangeEntity,
    ChangeEvent,
    ChangeKind,
)
from clinic_front_desk.data_layer.faults import fail_on, wrap
from clinic_front_desk.data_layer.memory import (
    MemoryAppointmentStore,
    MemoryCallSessionStore,
    MemoryDecisionStore,
    MemoryEscalationStore,
    MemoryWaitlistStore,
)
from clinic_front_desk.models import (
    Appointment,
    CallOutcome,
    Decision,
    DecisionKind,
    DecisionStatus,
    Escalation,
    EscalationReason,
    PatientRef,
    Slot,
    SlotStatus,
    is_err,
    is_ok,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _event(entity: ChangeEntity = ChangeEntity.DECISION) -> ChangeEvent:
    return ChangeEvent(entity=entity, id="x", kind=ChangeKind.CREATED)


def _decision(id: str, *, generated_at: str, finding_key: str = "k") -> Decision:
    return Decision(
        id=id,
        kind=DecisionKind.GAP_FILL,
        finding_key=finding_key,
        summary="s",
        recommended_action="a",
        generated_at=generated_at,
        supporting_record_count=5,
        status=DecisionStatus.OPEN,
    )


def _appt(id: str, *, provider_id: str, day: str, time: str, slot_id: str) -> Appointment:
    return Appointment(
        id=id,
        provider_id=provider_id,
        patient_id="pat-" + id,
        service="hearing_test",
        slot_id=slot_id,
        date=day,
        time=time,
    )


def _slot(id: str, *, provider_id: str, service: str, start: str) -> Slot:
    return Slot(
        id=id,
        provider_id=provider_id,
        service=service,
        start=start,
        end=start,
        status=SlotStatus.OPEN,
    )


# ---------------------------------------------------------------------------
# DashboardChannel — pub/sub fan-out (Req 9.6, 14.5, 14.8, 15.4)
# ---------------------------------------------------------------------------


def test_channel_is_a_change_emitter_and_broadcasts_on_emit() -> None:
    channel = DashboardChannel()
    conn = channel.connect()

    event = _event()
    channel.emit(event)

    assert conn.events == [event]


def test_channel_fans_out_to_every_connected_client() -> None:
    channel = DashboardChannel()
    a = channel.connect()
    b = channel.connect()

    channel.emit(_event())

    assert len(a.events) == 1
    assert len(b.events) == 1
    assert channel.subscriber_count == 2


def test_raw_subscribe_receives_events() -> None:
    channel = DashboardChannel()
    received: list[ChangeEvent] = []
    channel.subscribe(received.append)

    channel.emit(_event())

    assert len(received) == 1


def test_unsubscribe_stops_delivery() -> None:
    channel = DashboardChannel()
    received: list[ChangeEvent] = []
    sub = channel.subscribe(received.append)

    channel.emit(_event())
    sub.unsubscribe()
    channel.emit(_event())

    assert len(received) == 1
    assert channel.subscriber_count == 0


def test_connection_close_unsubscribes() -> None:
    channel = DashboardChannel()
    conn = channel.connect()

    conn.close()
    channel.emit(_event())

    assert conn.events == []
    assert channel.subscriber_count == 0


def test_double_unsubscribe_is_safe() -> None:
    channel = DashboardChannel()
    sub = channel.subscribe(lambda e: None)
    sub.unsubscribe()
    sub.unsubscribe()  # no error
    assert channel.subscriber_count == 0


def test_faulty_subscriber_does_not_break_others_or_caller() -> None:
    """A subscriber that raises is isolated: the other client still receives the
    event, the error is recorded, and emit() does not raise (Req 16.6)."""
    channel = DashboardChannel()

    def boom(_e: ChangeEvent) -> None:
        raise RuntimeError("client crashed")

    channel.subscribe(boom)
    good = channel.connect()

    channel.emit(_event())  # must not raise

    assert len(good.events) == 1
    assert len(channel.last_delivery_errors) == 1
    assert isinstance(channel.last_delivery_errors[0], RuntimeError)


def test_unsubscribe_during_broadcast_does_not_disturb_current_delivery() -> None:
    channel = DashboardChannel()
    seen_b: list[ChangeEvent] = []

    # a unsubscribes itself while handling the event; b must still be delivered.
    holder: dict[str, object] = {}

    def a(_e: ChangeEvent) -> None:
        holder["sub_a"].unsubscribe()  # type: ignore[attr-defined]

    holder["sub_a"] = channel.subscribe(a)
    channel.subscribe(seen_b.append)

    channel.emit(_event())

    assert len(seen_b) == 1


# ---------------------------------------------------------------------------
# Store -> channel fan-out wiring
# ---------------------------------------------------------------------------


def test_store_mutation_fans_out_to_connected_client() -> None:
    """A store constructed with the channel broadcasts its ChangeEvent to a
    connected dashboard client on a successful mutation (Req 14.8)."""
    channel = DashboardChannel()
    decisions = MemoryDecisionStore(channel)
    conn = channel.connect()

    decisions.create(_decision("d1", generated_at="2025-06-01T09:00:00Z"))

    assert len(conn.events) == 1
    assert conn.events[0].entity == ChangeEntity.DECISION
    assert conn.events[0].kind == ChangeKind.CREATED
    assert conn.events[0].id == "d1"


def test_failed_mutation_emits_nothing() -> None:
    """A write that fails must not fan out any event (Req 16.6)."""
    channel = DashboardChannel()
    decisions = wrap(MemoryDecisionStore(channel), fail_on("set_status"))
    conn = channel.connect()

    result = decisions.set_status("missing", DecisionStatus.APPROVED, "2025-06-01T09:00:00Z")

    assert is_err(result)
    assert conn.events == []


# ---------------------------------------------------------------------------
# DashboardBFF reads through the Data_Layer
# ---------------------------------------------------------------------------


def _bff(
    *,
    channel: DashboardChannel,
    decisions: MemoryDecisionStore,
    appointments: MemoryAppointmentStore,
    calls: MemoryCallSessionStore,
    escalations: MemoryEscalationStore,
    waitlist: MemoryWaitlistStore | None = None,
) -> DashboardBFF:
    return DashboardBFF(
        channel=channel,
        decision_store=decisions,
        appointment_store=appointments,
        call_session_store=calls,
        escalation_store=escalations,
        waitlist_store=waitlist,
    )


def test_open_decisions_reads_feed_newest_first() -> None:
    channel = DashboardChannel()
    decisions = MemoryDecisionStore(channel)
    decisions.create(_decision("old", generated_at="2025-06-01T08:00:00Z", finding_key="a"))
    decisions.create(_decision("new", generated_at="2025-06-01T12:00:00Z", finding_key="b"))
    bff = _bff(
        channel=channel,
        decisions=decisions,
        appointments=MemoryAppointmentStore(channel),
        calls=MemoryCallSessionStore(channel),
        escalations=MemoryEscalationStore(channel),
    )

    result = bff.open_decisions()

    assert is_ok(result)
    assert [d.id for d in result.value] == ["new", "old"]


def test_open_decisions_propagates_read_failure() -> None:
    channel = DashboardChannel()
    bff = _bff(
        channel=channel,
        decisions=wrap(MemoryDecisionStore(channel), fail_on("list_open")),
        appointments=MemoryAppointmentStore(channel),
        calls=MemoryCallSessionStore(channel),
        escalations=MemoryEscalationStore(channel),
    )

    assert is_err(bff.open_decisions())


def test_schedule_for_day_returns_appointments_and_open_slots() -> None:
    channel = DashboardChannel()
    appointments = MemoryAppointmentStore(channel)
    appointments.seed_slot(
        _slot("s-open", provider_id="prov1", service="hearing_test", start="2025-06-01T10:00:00Z")
    )
    # A slot on a different day must be excluded from the day's view.
    appointments.seed_slot(
        _slot("s-other", provider_id="prov1", service="hearing_test", start="2025-06-02T10:00:00Z")
    )
    appointments.create(
        _appt("a1", provider_id="prov1", day="2025-06-01", time="09:00", slot_id="s-booked")
    )

    bff = _bff(
        channel=channel,
        decisions=MemoryDecisionStore(channel),
        appointments=appointments,
        calls=MemoryCallSessionStore(channel),
        escalations=MemoryEscalationStore(channel),
    )

    result = bff.schedule_for_day("prov1", "2025-06-01", services=["hearing_test"])

    assert is_ok(result)
    view = result.value
    assert isinstance(view, ScheduleView)
    assert [a.id for a in view.appointments] == ["a1"]
    assert [s.id for s in view.open_slots] == ["s-open"]


def test_schedule_for_day_without_services_returns_no_open_slots() -> None:
    channel = DashboardChannel()
    appointments = MemoryAppointmentStore(channel)
    appointments.seed_slot(
        _slot("s-open", provider_id="prov1", service="hearing_test", start="2025-06-01T10:00:00Z")
    )
    bff = _bff(
        channel=channel,
        decisions=MemoryDecisionStore(channel),
        appointments=appointments,
        calls=MemoryCallSessionStore(channel),
        escalations=MemoryEscalationStore(channel),
    )

    result = bff.schedule_for_day("prov1", "2025-06-01")

    assert is_ok(result)
    assert result.value.open_slots == []


def test_schedule_for_day_propagates_appointment_read_failure() -> None:
    channel = DashboardChannel()
    bff = _bff(
        channel=channel,
        decisions=MemoryDecisionStore(channel),
        appointments=wrap(MemoryAppointmentStore(channel), fail_on("list_by_provider_and_day")),
        calls=MemoryCallSessionStore(channel),
        escalations=MemoryEscalationStore(channel),
    )

    assert is_err(bff.schedule_for_day("prov1", "2025-06-01", services=["hearing_test"]))


def test_schedule_for_day_propagates_open_slot_read_failure() -> None:
    channel = DashboardChannel()
    bff = _bff(
        channel=channel,
        decisions=MemoryDecisionStore(channel),
        appointments=wrap(MemoryAppointmentStore(channel), fail_on("list_open_slots")),
        calls=MemoryCallSessionStore(channel),
        escalations=MemoryEscalationStore(channel),
    )

    assert is_err(bff.schedule_for_day("prov1", "2025-06-01", services=["hearing_test"]))


def test_recent_activity_aggregates_sessions_and_escalations() -> None:
    channel = DashboardChannel()
    calls = MemoryCallSessionStore(channel)
    escalations = MemoryEscalationStore(channel)

    from clinic_front_desk.models import CallSession

    calls.create(CallSession(id="s1", started_at="2025-06-01T09:00:00Z"))
    # `ended_at` is passed explicitly: the log orders a call by when it *ended*,
    # and finalize now defaults that to the wall clock — which would make the call
    # newer than the escalation and the assertion below time-dependent.
    calls.finalize(
        "s1",
        CallOutcome.BOOKED,
        PatientRef(patient_id="p1"),
        ended_at="2025-06-01T09:05:00Z",
    )
    escalations.create(
        Escalation(
            id="e1",
            reason=EscalationReason.CLINICAL_CONTENT,
            call_session_id="s1",
            context="ctx",
            created_at="2025-06-01T11:00:00Z",
        )
    )

    bff = _bff(
        channel=channel,
        decisions=MemoryDecisionStore(channel),
        appointments=MemoryAppointmentStore(channel),
        calls=calls,
        escalations=escalations,
    )

    result = bff.recent_activity(limit=10)

    assert is_ok(result)
    assert [e.source_id for e in result.value] == ["e1", "s1"]


def test_recent_activity_propagates_read_failure() -> None:
    channel = DashboardChannel()
    bff = _bff(
        channel=channel,
        decisions=MemoryDecisionStore(channel),
        appointments=MemoryAppointmentStore(channel),
        calls=wrap(MemoryCallSessionStore(channel), fail_on("list_recent")),
        escalations=MemoryEscalationStore(channel),
    )

    assert is_err(bff.recent_activity())


def test_impact_metrics_delegates_to_injected_computer_with_stores() -> None:
    """The BFF supplies the Data_Layer stores and returns the computer's output;
    it does not compute metrics itself (task 12.6)."""
    channel = DashboardChannel()
    appointments = MemoryAppointmentStore(channel)
    waitlist = MemoryWaitlistStore(channel)
    calls = MemoryCallSessionStore(channel)
    bff = _bff(
        channel=channel,
        decisions=MemoryDecisionStore(channel),
        appointments=appointments,
        calls=calls,
        escalations=MemoryEscalationStore(channel),
        waitlist=waitlist,
    )

    received: dict[str, object] = {}

    def compute(appt_store, wl_store, cs_store):  # type: ignore[no-untyped-def]
        received["appt"] = appt_store
        received["wl"] = wl_store
        received["cs"] = cs_store
        return {"hours_saved": 42}

    result = bff.impact_metrics(compute)

    assert result == {"hours_saved": 42}
    assert received["appt"] is appointments
    assert received["wl"] is waitlist
    assert received["cs"] is calls


def test_impact_metrics_requires_waitlist_store() -> None:
    channel = DashboardChannel()
    bff = _bff(
        channel=channel,
        decisions=MemoryDecisionStore(channel),
        appointments=MemoryAppointmentStore(channel),
        calls=MemoryCallSessionStore(channel),
        escalations=MemoryEscalationStore(channel),
        waitlist=None,
    )

    with pytest.raises(RuntimeError):
        bff.impact_metrics(lambda a, w, c: None)


# ---------------------------------------------------------------------------
# build_memory_bff wiring
# ---------------------------------------------------------------------------


def test_build_memory_bff_wires_channel_and_reads_empty() -> None:
    bff = build_memory_bff()

    # Reads work against empty stores (Req 16.4).
    decisions = bff.open_decisions()
    schedule = bff.schedule_for_day("prov1", "2025-06-01")
    activity = bff.recent_activity()

    assert is_ok(decisions) and decisions.value == []
    assert is_ok(schedule) and schedule.value.appointments == []
    assert is_ok(activity) and activity.value == []

    # The channel is live and clients can connect for fan-out.
    conn = bff.connect()
    assert bff.channel.subscriber_count == 1
    bff.channel.emit(_event())
    assert len(conn.events) == 1
