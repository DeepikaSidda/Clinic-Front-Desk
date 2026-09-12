"""Dashboard backend-for-frontend (task 12.1, Req 14.8, 14.5, 15.4, 9.6).

The BFF is the *thin* server the dashboard web app talks to. Two responsibilities,
both drawn directly from the design's "Dashboard" section:

1. **Read exclusively through the Data_Layer.** Every read the dashboard
   components need is served by calling a store interface — never storage
   directly (Req 16.1). The BFF exposes:

   - :meth:`DashboardBFF.open_decisions` → ``DecisionStore.list_open`` (the
     "Decisions to make" feed, newest-first, Req 14.1).
   - :meth:`DashboardBFF.schedule_for_day` → ``AppointmentStore`` (the schedule
     view's appointments and open slots for a day, Req 15.1/15.4).
   - :meth:`DashboardBFF.recent_activity` → the pure aggregation in
     :mod:`clinic_front_desk.dashboard.activity_log` over ``CallSessionStore``
     and ``EscalationStore`` (the call-activity log, Req 15.2/9.6).
   - :meth:`DashboardBFF.impact_metrics` → delegates to a caller-supplied
     metrics computer over the relevant stores. The BFF does **not** compute
     metrics itself (that is ``dashboard/metrics.py``, task 12.6); it only wires
     the Data_Layer reads to whatever computation is injected.

2. **Fan ChangeEvents out to connected clients.** The BFF owns a
   :class:`~clinic_front_desk.dashboard.pubsub.DashboardChannel` — an in-process
   pub/sub abstraction standing in for the WebSocket/SSE transport. The channel
   *is* the :class:`~clinic_front_desk.data_layer.events.ChangeEmitter` the seven
   stores are constructed with, so every successful mutation is broadcast to all
   connected dashboard clients immediately and synchronously. That immediacy is
   what keeps propagation inside the required budgets (decision add ≤ 5 s /
   removal ≤ 2 s, schedule & activity ≤ 5 s, escalation ≤ 5 s — Req 14.8, 14.5,
   15.4, 9.6): there is no queue or poll between the mutation and the client
   seeing the event.

Wiring pattern::

    channel = DashboardChannel()
    stores = wire_stores(emitter=channel)   # every store gets `channel`
    bff = DashboardBFF(channel=channel, ...stores...)
    connection = bff.connect()              # a dashboard client subscribes

:func:`build_memory_bff` performs exactly this wiring against the in-memory fake
stores, for tests and local runs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from clinic_front_desk.dashboard.activity_log import (
    ActivityLogEntry,
    aggregate_activity_log,
)
from clinic_front_desk.dashboard.pubsub import (
    DashboardChannel,
    DashboardConnection,
    Subscriber,
    Subscription,
)
from clinic_front_desk.data_layer.interfaces import (
    AppointmentStore,
    CallSessionStore,
    ClinicKnowledgeBaseStore,
    DecisionStore,
    EscalationStore,
    PatientStore,
    WaitlistStore,
)
from clinic_front_desk.models import (
    Appointment,
    Decision,
    ISODate,
    Ok,
    Slot,
    StoreResult,
    is_err,
)

T = TypeVar("T")

# A metrics computer reads through the Data_Layer stores it needs and returns
# whatever metrics shape it likes. The BFF supplies the stores and stays out of
# the computation (task 12.6 owns dashboard/metrics.py).
MetricsComputer = Callable[[AppointmentStore, WaitlistStore, CallSessionStore], T]


@dataclass(frozen=True)
class ScheduleView:
    """The schedule view's data for one provider on one day (Req 15.1).

    Attributes:
        provider_id: The provider whose calendar this is.
        day: The ISO date the view covers.
        appointments: Booked appointments for the day.
        open_slots: Open slots for the day across the requested services.
    """

    provider_id: str
    day: ISODate
    appointments: list[Appointment]
    open_slots: list[Slot]


def _slot_on_day(slot: Slot, day: ISODate) -> bool:
    """True when ``slot`` starts on ``day``.

    Slot start times are ISO-8601 UTC date-times (``YYYY-MM-DDT...``); the date
    portion is the leading ``YYYY-MM-DD``, so a prefix check selects a single
    calendar day.
    """
    return slot.start.startswith(day)


class DashboardBFF:
    """Reads through the Data_Layer and fans change events out to clients.

    The BFF holds references to the store interfaces (never concrete storage)
    and the :class:`DashboardChannel` the stores emit through. Read methods
    return the same :data:`~clinic_front_desk.models.StoreResult` the stores do,
    so a read failure is propagated (never a partial view), consistent with the
    Data_Layer's all-or-nothing contract.
    """

    def __init__(
        self,
        *,
        channel: DashboardChannel,
        decision_store: DecisionStore,
        appointment_store: AppointmentStore,
        call_session_store: CallSessionStore,
        escalation_store: EscalationStore,
        waitlist_store: WaitlistStore | None = None,
        patient_store: PatientStore | None = None,
        clinic_knowledge_base_store: ClinicKnowledgeBaseStore | None = None,
    ) -> None:
        self._channel = channel
        self._decision_store = decision_store
        self._appointment_store = appointment_store
        self._call_session_store = call_session_store
        self._escalation_store = escalation_store
        self._waitlist_store = waitlist_store
        self._patient_store = patient_store
        self._clinic_knowledge_base_store = clinic_knowledge_base_store

    # -- real-time channel -------------------------------------------------

    @property
    def channel(self) -> DashboardChannel:
        """The pub/sub channel the stores emit through and clients subscribe to."""
        return self._channel

    def connect(self) -> DashboardConnection:
        """Attach a new dashboard client to the change-event fan-out (Req 14.8)."""
        return self._channel.connect()

    def subscribe(self, callback: Subscriber) -> Subscription:
        """Register a raw change-event callback on the channel."""
        return self._channel.subscribe(callback)

    # -- reads through the Data_Layer --------------------------------------

    def open_decisions(self) -> StoreResult[list[Decision]]:
        """Return the open "Decisions to make" feed, newest-first (Req 14.1).

        Served entirely by ``DecisionStore.list_open``; a read failure is
        propagated as an ``Err``.
        """
        return self._decision_store.list_open()

    def schedule_for_day(
        self,
        provider_id: str,
        day: ISODate,
        services: list[str] | None = None,
    ) -> StoreResult[ScheduleView]:
        """Return the provider's appointments and open slots for ``day`` (Req 15.1).

        Appointments come from ``AppointmentStore.list_by_provider_and_day``. Open
        slots are read per requested service via ``list_open_slots`` (the store's
        only open-slot access path is service-scoped) and filtered to ``day``;
        when ``services`` is omitted no open slots are gathered (the caller can
        pass the clinic's offered-service names). Any store read failure aborts
        with an ``Err`` so the view is never partial (Req 16.6).

        The default day is the caller's concern (Req 15.1 "current day"): the BFF
        reads exactly the ``day`` it is given, which also serves the
        select-another-day path (Req 15.6).
        """
        appts_result = self._appointment_store.list_by_provider_and_day(
            provider_id, day
        )
        if is_err(appts_result):
            return appts_result

        open_slots: list[Slot] = []
        for service in services or []:
            slots_result = self._appointment_store.list_open_slots(
                provider_id, service, day
            )
            if is_err(slots_result):
                return slots_result
            # list_open_slots returns slots from `day` onward; keep only the
            # requested day so the schedule view shows a single day (Req 15.1).
            open_slots.extend(
                slot for slot in slots_result.value if _slot_on_day(slot, day)
            )

        return Ok(
            ScheduleView(
                provider_id=provider_id,
                day=day,
                appointments=appts_result.value,
                open_slots=open_slots,
            )
        )

    def recent_activity(
        self, limit: int = 50
    ) -> StoreResult[list[ActivityLogEntry]]:
        """Return the most-recent-first call-activity log (Req 15.2, 9.6).

        Delegates to :func:`~clinic_front_desk.dashboard.activity_log.aggregate_activity_log`,
        which reads ``CallSessionStore`` and ``EscalationStore`` and defers all
        shaping/ordering to the pure aggregation.
        """
        return aggregate_activity_log(
            self._call_session_store, self._escalation_store, limit
        )

    def impact_metrics(self, compute: MetricsComputer[T]) -> T:
        """Compute impact metrics via an injected computer (Req 15.3, task 12.6).

        The BFF supplies the Data_Layer stores the metrics computation reads
        (appointments for no-show rate, waitlist for recovered-appointment count,
        call sessions for front-desk hours saved) and returns whatever the
        computer produces. Metric computation itself lives in
        ``dashboard/metrics.py``; the BFF only wires the reads.

        Raises:
            RuntimeError: if the waitlist store was not provided at construction,
                since metrics computation requires it.
        """
        if self._waitlist_store is None:
            raise RuntimeError(
                "impact_metrics requires a WaitlistStore; construct the BFF with one"
            )
        return compute(
            self._appointment_store,
            self._waitlist_store,
            self._call_session_store,
        )


def build_memory_bff() -> DashboardBFF:
    """Wire a :class:`DashboardBFF` over the in-memory fake stores.

    Constructs a single :class:`DashboardChannel` and gives it to every store as
    their :class:`~clinic_front_desk.data_layer.events.ChangeEmitter`, so all
    mutations fan out to dashboard clients through the one channel. Convenient
    for tests and local runs; production wiring supplies the DynamoDB stores the
    same way.
    """
    from clinic_front_desk.data_layer.memory import (
        MemoryAppointmentStore,
        MemoryCallSessionStore,
        MemoryClinicKnowledgeBaseStore,
        MemoryDecisionStore,
        MemoryEscalationStore,
        MemoryPatientStore,
        MemoryWaitlistStore,
    )

    channel = DashboardChannel()
    return DashboardBFF(
        channel=channel,
        decision_store=MemoryDecisionStore(channel),
        appointment_store=MemoryAppointmentStore(channel),
        call_session_store=MemoryCallSessionStore(channel),
        escalation_store=MemoryEscalationStore(channel),
        waitlist_store=MemoryWaitlistStore(channel),
        patient_store=MemoryPatientStore(channel),
        clinic_knowledge_base_store=MemoryClinicKnowledgeBaseStore(channel),
    )


__all__ = [
    "DashboardBFF",
    "ScheduleView",
    "MetricsComputer",
    "build_memory_bff",
]
