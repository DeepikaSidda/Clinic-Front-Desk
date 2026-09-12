"""Fault-injection store wrappers for failure-path tests (Req 16.6).

The Data_Layer contract says a write that fails returns an
:class:`~clinic_front_desk.models.Err` carrying a
:class:`~clinic_front_desk.models.StoreError` and leaves *every* prior record
unchanged — no partial write. To exercise that contract we need to force a
chosen store operation to fail *on demand*, without depending on a flaky real
backend.

This module provides exactly that: a thin wrapper around **any** of the seven
Data_Layer store interfaces that can be configured to make a chosen operation
(by method name, optionally only after N successful calls and/or only for a
specific record) return an ``Err(StoreError(kind=store_failure))`` instead of
delegating. Every other call passes straight through to the wrapped store.

Because a forced failure short-circuits *before* delegating, the underlying
store is never touched on a fault — which is precisely the atomicity guarantee
(Req 16.6) the write-atomicity property test (Property 26, task 3.6) and the
other failure-path properties (Properties 20, 28) rely on.

Design notes:
    - There is one concrete wrapper per interface so the wrapped object still
      satisfies ``isinstance(wrapper, AppointmentStore)`` etc. and can be passed
      anywhere the real store is expected.
    - :func:`wrap` is a small factory that picks the right wrapper for a given
      store instance; :func:`fail_on` is sugar for constructing a single
      :class:`Fault`.
    - Everything is fully typed and mypy-friendly: each wrapper mirrors its
      interface's signatures and returns the same ``StoreResult`` shapes.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Iterable

from clinic_front_desk.models import (
    Appointment,
    CallOutcome,
    CallSession,
    ClinicDocument,
    ClinicKnowledgeBase,
    Decision,
    DecisionStatus,
    DocumentChunk,
    Err,
    Escalation,
    ISODate,
    ISODateTime,
    Patient,
    PatientRef,
    Slot,
    SlotStatus,
    StoreError,
    StoreErrorKind,
    StoreResult,
    WaitlistEntry,
)

from .interfaces import (
    AppointmentStore,
    CallSessionStore,
    ClinicDocumentStore,
    ClinicKnowledgeBaseStore,
    DecisionStore,
    EscalationStore,
    NewAppointment,
    NewCallSession,
    NewDecision,
    NewEscalation,
    NewPatient,
    NewWaitlistEntry,
    OpenSlotSpan,
    PatientStore,
    SlotRelease,
    WaitlistStore,
)

# ---------------------------------------------------------------------------
# Fault specification and controller
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fault:
    """A rule that forces a store operation to fail.

    Attributes:
        method: The store method name to fail (e.g. ``"create"``, ``"move"``,
            ``"set_slot_status"``). A rule only ever affects this one method.
        after_calls: Number of *matching* calls to let succeed (delegate) before
            the fault triggers. ``0`` (the default) fails the very first matching
            call; ``2`` lets two calls through and fails the third onward.
        record_id: If set, the fault only applies to calls that target this
            record id — matched against any ``str`` argument or the ``id`` of any
            entity argument (e.g. the appointment id passed to ``move``/``remove``
            or the ``id`` of a ``create`` input). If ``None`` the fault applies to
            every call of ``method``.
        detail: Human-readable detail placed on the emitted
            :class:`~clinic_front_desk.models.StoreError`.
    """

    method: str
    after_calls: int = 0
    record_id: str | None = None
    detail: str = "injected store fault"


def fail_on(
    method: str,
    *,
    after_calls: int = 0,
    record_id: str | None = None,
    detail: str = "injected store fault",
) -> Fault:
    """Convenience constructor for a single :class:`Fault` (reads nicely at call sites)."""
    return Fault(
        method=method,
        after_calls=after_calls,
        record_id=record_id,
        detail=detail,
    )


def _candidate_ids(args: tuple[Any, ...], kwargs: dict[str, Any]) -> set[str]:
    """Collect the record identifiers a call could be targeting.

    A call "targets" a record if any argument is that record's id string, or is
    an entity carrying an ``id`` attribute equal to it. This lets a
    ``record_id`` filter match both id-taking methods (``get``/``move``/
    ``remove``/``set_status``) and entity-taking methods (``create``/``add``).
    """
    ids: set[str] = set()
    for value in (*args, *kwargs.values()):
        if isinstance(value, str):
            ids.add(value)
            continue
        record_id = getattr(value, "id", None)
        if isinstance(record_id, str):
            ids.add(record_id)
    return ids


@dataclass
class FaultController:
    """Decides, per call, whether a configured :class:`Fault` should fire.

    A controller owns a set of faults and the per-fault call counters used to
    honour ``after_calls``. It is shared by the wrapper so tests can inspect or
    mutate the fault set between operations.
    """

    faults: list[Fault] = field(default_factory=list)
    _counts: dict[int, int] = field(default_factory=lambda: defaultdict(int))

    def add(self, fault: Fault) -> None:
        """Register an additional fault rule."""
        self.faults.append(fault)

    def clear(self) -> None:
        """Remove all fault rules and reset counters (store passes through cleanly)."""
        self.faults.clear()
        self._counts.clear()

    def error_for(
        self,
        method: str,
        store_label: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> StoreError | None:
        """Return a :class:`StoreError` if a fault should fire for this call, else ``None``."""
        candidates: set[str] | None = None
        for fault in self.faults:
            if fault.method != method:
                continue
            if fault.record_id is not None:
                if candidates is None:
                    candidates = _candidate_ids(args, kwargs)
                if fault.record_id not in candidates:
                    continue
            key = id(fault)
            self._counts[key] += 1
            if self._counts[key] > fault.after_calls:
                return StoreError(
                    kind=StoreErrorKind.STORE_FAILURE,
                    detail=fault.detail,
                    store=store_label,
                )
        return None


# ---------------------------------------------------------------------------
# Wrapper base
# ---------------------------------------------------------------------------


class _FaultInjectingStore:
    """Shared plumbing for the per-interface wrappers.

    Holds the wrapped store, the shared :class:`FaultController`, and a label
    used on emitted :class:`StoreError`s. Subclasses delegate each interface
    method through :meth:`_guard`.
    """

    def __init__(
        self,
        wrapped: object,
        controller: FaultController,
        *,
        store_label: str | None = None,
    ) -> None:
        self._wrapped = wrapped
        self._controller = controller
        self._store_label = store_label or type(wrapped).__name__

    @property
    def controller(self) -> FaultController:
        """The shared fault controller (add/clear faults between operations)."""
        return self._controller

    def _guard(
        self, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Err[StoreError] | None:
        """Return an ``Err`` if a fault should fire for ``method``, else ``None``.

        On a fault we return *before* delegating, so the wrapped store is never
        mutated — preserving the atomicity contract (Req 16.6).
        """
        error = self._controller.error_for(method, self._store_label, args, kwargs)
        if error is not None:
            return Err(error)
        return None


# ---------------------------------------------------------------------------
# Per-interface wrappers
# ---------------------------------------------------------------------------


class FaultInjectingAppointmentStore(_FaultInjectingStore, AppointmentStore):
    """Fault-injecting wrapper around an :class:`AppointmentStore`."""

    def __init__(
        self,
        wrapped: AppointmentStore,
        controller: FaultController,
        *,
        store_label: str | None = None,
    ) -> None:
        super().__init__(wrapped, controller, store_label=store_label)
        self._store: AppointmentStore = wrapped

    def create(self, a: NewAppointment) -> StoreResult[Appointment]:
        return self._guard("create", (a,), {}) or self._store.create(a)

    def get(self, id: str) -> StoreResult[Appointment | None]:
        return self._guard("get", (id,), {}) or self._store.get(id)

    def list_by_provider_and_day(
        self, provider_id: str, day: ISODate
    ) -> StoreResult[list[Appointment]]:
        return self._guard(
            "list_by_provider_and_day", (provider_id, day), {}
        ) or self._store.list_by_provider_and_day(provider_id, day)

    def list_by_patient(self, patient_id: str) -> StoreResult[list[Appointment]]:
        return self._guard("list_by_patient", (patient_id,), {}) or self._store.list_by_patient(
            patient_id
        )

    def move(self, id: str, new_slot_id: str) -> StoreResult[Appointment]:
        return self._guard("move", (id, new_slot_id), {}) or self._store.move(id, new_slot_id)

    def remove(self, id: str) -> StoreResult[SlotRelease]:
        return self._guard("remove", (id,), {}) or self._store.remove(id)

    def add_slots(self, slots: list[Slot]) -> StoreResult[list[Slot]]:
        return self._guard("add_slots", (slots,), {}) or self._store.add_slots(slots)

    def list_slots_for_day(
        self, provider_id: str, day: ISODate
    ) -> StoreResult[list[Slot]]:
        return self._guard(
            "list_slots_for_day", (provider_id, day), {}
        ) or self._store.list_slots_for_day(provider_id, day)

    def get_slot(self, slot_id: str) -> StoreResult[Slot | None]:
        return self._guard("get_slot", (slot_id,), {}) or self._store.get_slot(slot_id)

    def list_open_slots(
        self,
        provider_id: str,
        service: str,
        from_date: ISODate,
        *,
        limit: int | None = None,
    ) -> StoreResult[list[Slot]]:
        return self._guard(
            "list_open_slots", (provider_id, service, from_date), {"limit": limit}
        ) or self._store.list_open_slots(provider_id, service, from_date, limit=limit)

    def list_open_slots_for_provider(
        self,
        provider_id: str,
        from_bound: str,
        *,
        limit: int | None = None,
    ) -> StoreResult[list[Slot]]:
        return self._guard(
            "list_open_slots_for_provider", (provider_id, from_bound), {"limit": limit}
        ) or self._store.list_open_slots_for_provider(
            provider_id, from_bound, limit=limit
        )

    def open_slot_span(
        self, provider_id: str, from_date: ISODate
    ) -> StoreResult[OpenSlotSpan | None]:
        return self._guard(
            "open_slot_span", (provider_id, from_date), {}
        ) or self._store.open_slot_span(provider_id, from_date)

    def set_slot_status(self, slot_id: str, status: SlotStatus) -> StoreResult[Slot]:
        return self._guard("set_slot_status", (slot_id, status), {}) or self._store.set_slot_status(
            slot_id, status
        )

    def set_slot_statuses(
        self, slots: Sequence[Slot], status: SlotStatus
    ) -> StoreResult[list[Slot]]:
        return self._guard(
            "set_slot_statuses", (slots, status), {}
        ) or self._store.set_slot_statuses(slots, status)

    def remove_slots(self, slots: Sequence[Slot]) -> StoreResult[list[str]]:
        return self._guard("remove_slots", (slots,), {}) or self._store.remove_slots(
            slots
        )


class FaultInjectingPatientStore(_FaultInjectingStore, PatientStore):
    """Fault-injecting wrapper around a :class:`PatientStore`."""

    def __init__(
        self,
        wrapped: PatientStore,
        controller: FaultController,
        *,
        store_label: str | None = None,
    ) -> None:
        super().__init__(wrapped, controller, store_label=store_label)
        self._store: PatientStore = wrapped

    def create(self, p: NewPatient) -> StoreResult[Patient]:
        return self._guard("create", (p,), {}) or self._store.create(p)

    def find_by_name_and_phone(self, name: str, phone: str) -> StoreResult[list[Patient]]:
        return self._guard(
            "find_by_name_and_phone", (name, phone), {}
        ) or self._store.find_by_name_and_phone(name, phone)

    def get(self, id: str) -> StoreResult[Patient | None]:
        return self._guard("get", (id,), {}) or self._store.get(id)

    def find_by_code(self, code: str) -> StoreResult[list[Patient]]:
        return self._guard("find_by_code", (code,), {}) or self._store.find_by_code(code)

    def update(self, p: Patient) -> StoreResult[Patient]:
        return self._guard("update", (p,), {}) or self._store.update(p)


class FaultInjectingWaitlistStore(_FaultInjectingStore, WaitlistStore):
    """Fault-injecting wrapper around a :class:`WaitlistStore`."""

    def __init__(
        self,
        wrapped: WaitlistStore,
        controller: FaultController,
        *,
        store_label: str | None = None,
    ) -> None:
        super().__init__(wrapped, controller, store_label=store_label)
        self._store: WaitlistStore = wrapped

    def add(self, e: NewWaitlistEntry) -> StoreResult[WaitlistEntry]:
        return self._guard("add", (e,), {}) or self._store.add(e)

    def find_active(
        self, patient_id: str, service: str, slot_type: str
    ) -> StoreResult[WaitlistEntry | None]:
        return self._guard(
            "find_active", (patient_id, service, slot_type), {}
        ) or self._store.find_active(patient_id, service, slot_type)

    def list_by_service_ordered(self, service: str) -> StoreResult[list[WaitlistEntry]]:
        return self._guard(
            "list_by_service_ordered", (service,), {}
        ) or self._store.list_by_service_ordered(service)

    def remove(self, id: str) -> StoreResult[None]:
        return self._guard("remove", (id,), {}) or self._store.remove(id)


class FaultInjectingDecisionStore(_FaultInjectingStore, DecisionStore):
    """Fault-injecting wrapper around a :class:`DecisionStore`."""

    def __init__(
        self,
        wrapped: DecisionStore,
        controller: FaultController,
        *,
        store_label: str | None = None,
    ) -> None:
        super().__init__(wrapped, controller, store_label=store_label)
        self._store: DecisionStore = wrapped

    def create(self, d: NewDecision) -> StoreResult[Decision]:
        return self._guard("create", (d,), {}) or self._store.create(d)

    def list_open(self) -> StoreResult[list[Decision]]:
        return self._guard("list_open", (), {}) or self._store.list_open()

    def list_by_status(self, status: DecisionStatus) -> StoreResult[list[Decision]]:
        return self._guard(
            "list_by_status", (status,), {}
        ) or self._store.list_by_status(status)

    def find_open_by_finding_key(self, key: str) -> StoreResult[Decision | None]:
        return self._guard(
            "find_open_by_finding_key", (key,), {}
        ) or self._store.find_open_by_finding_key(key)

    def set_status(
        self, id: str, status: DecisionStatus, resolved_at: ISODateTime
    ) -> StoreResult[Decision]:
        return self._guard(
            "set_status", (id, status, resolved_at), {}
        ) or self._store.set_status(id, status, resolved_at)


class FaultInjectingClinicKnowledgeBaseStore(
    _FaultInjectingStore, ClinicKnowledgeBaseStore
):
    """Fault-injecting wrapper around a :class:`ClinicKnowledgeBaseStore`."""

    def __init__(
        self,
        wrapped: ClinicKnowledgeBaseStore,
        controller: FaultController,
        *,
        store_label: str | None = None,
    ) -> None:
        super().__init__(wrapped, controller, store_label=store_label)
        self._store: ClinicKnowledgeBaseStore = wrapped

    def get(self) -> StoreResult[ClinicKnowledgeBase | None]:
        return self._guard("get", (), {}) or self._store.get()

    def save(self, kb: ClinicKnowledgeBase) -> StoreResult[ClinicKnowledgeBase]:
        return self._guard("save", (kb,), {}) or self._store.save(kb)


class FaultInjectingCallSessionStore(_FaultInjectingStore, CallSessionStore):
    """Fault-injecting wrapper around a :class:`CallSessionStore`."""

    def __init__(
        self,
        wrapped: CallSessionStore,
        controller: FaultController,
        *,
        store_label: str | None = None,
    ) -> None:
        super().__init__(wrapped, controller, store_label=store_label)
        self._store: CallSessionStore = wrapped

    def create(self, s: NewCallSession) -> StoreResult[CallSession]:
        return self._guard("create", (s,), {}) or self._store.create(s)

    def finalize(
        self,
        id: str,
        outcome: CallOutcome,
        patient_info: PatientRef,
        *,
        ended_at: ISODateTime | None = None,
        transcript: str | None = None,
        recording_uri: str | None = None,
    ) -> StoreResult[CallSession]:
        kwargs: dict[str, Any] = {
            "ended_at": ended_at,
            "transcript": transcript,
            "recording_uri": recording_uri,
        }
        return self._guard(
            "finalize", (id, outcome, patient_info), kwargs
        ) or self._store.finalize(id, outcome, patient_info, **kwargs)

    def list_recent(self, limit: int) -> StoreResult[list[CallSession]]:
        return self._guard("list_recent", (limit,), {}) or self._store.list_recent(limit)


class FaultInjectingEscalationStore(_FaultInjectingStore, EscalationStore):
    """Fault-injecting wrapper around an :class:`EscalationStore`."""

    def __init__(
        self,
        wrapped: EscalationStore,
        controller: FaultController,
        *,
        store_label: str | None = None,
    ) -> None:
        super().__init__(wrapped, controller, store_label=store_label)
        self._store: EscalationStore = wrapped

    def create(self, e: NewEscalation) -> StoreResult[Escalation]:
        return self._guard("create", (e,), {}) or self._store.create(e)

    def list_recent(self, limit: int) -> StoreResult[list[Escalation]]:
        return self._guard("list_recent", (limit,), {}) or self._store.list_recent(limit)


class FaultInjectingClinicDocumentStore(_FaultInjectingStore, ClinicDocumentStore):
    """Fault-injecting wrapper around a :class:`ClinicDocumentStore`.

    ``list_chunks`` is the one that matters most: it runs mid-call on the
    retrieval path, so its failure behaviour (fall back to the configured answer,
    never a guess) needs to be exercisable rather than reasoned about.
    """

    def __init__(
        self,
        wrapped: ClinicDocumentStore,
        controller: FaultController,
        *,
        store_label: str | None = None,
    ) -> None:
        super().__init__(wrapped, controller, store_label=store_label)
        self._store: ClinicDocumentStore = wrapped

    def put(
        self,
        document: ClinicDocument,
        *,
        original: bytes,
        chunks: list[DocumentChunk],
    ) -> StoreResult[ClinicDocument]:
        kwargs: dict[str, Any] = {"original": original, "chunks": chunks}
        return self._guard("put", (document,), kwargs) or self._store.put(
            document, original=original, chunks=chunks
        )

    def list_documents(self) -> StoreResult[list[ClinicDocument]]:
        return self._guard("list_documents", (), {}) or self._store.list_documents()

    def list_chunks(self) -> StoreResult[list[DocumentChunk]]:
        return self._guard("list_chunks", (), {}) or self._store.list_chunks()

    def get_original(self, document_id: str) -> StoreResult[bytes | None]:
        return self._guard(
            "get_original", (document_id,), {}
        ) or self._store.get_original(document_id)

    def delete(self, document_id: str) -> StoreResult[None]:
        return self._guard("delete", (document_id,), {}) or self._store.delete(
            document_id
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

# Map each interface to its wrapper. Order does not matter because the store
# interfaces are disjoint types.
_WRAPPERS: tuple[tuple[type, type[_FaultInjectingStore]], ...] = (
    (AppointmentStore, FaultInjectingAppointmentStore),
    (PatientStore, FaultInjectingPatientStore),
    (WaitlistStore, FaultInjectingWaitlistStore),
    (DecisionStore, FaultInjectingDecisionStore),
    (ClinicKnowledgeBaseStore, FaultInjectingClinicKnowledgeBaseStore),
    (CallSessionStore, FaultInjectingCallSessionStore),
    (EscalationStore, FaultInjectingEscalationStore),
    (ClinicDocumentStore, FaultInjectingClinicDocumentStore),
)


def wrap(
    store: object,
    faults: Iterable[Fault] | Fault | None = None,
    *,
    controller: FaultController | None = None,
    store_label: str | None = None,
) -> Any:
    """Wrap ``store`` (any Data_Layer interface) in its fault-injecting proxy.

    The returned wrapper implements the *same* interface as ``store`` (so it can
    be passed anywhere the real store is expected) and shares a
    :class:`FaultController` — either the one supplied, or one seeded from
    ``faults``.

    Args:
        store: An instance of any Data_Layer store interface.
        faults: A single :class:`Fault`, an iterable of them, or ``None``. Used
            to seed a new controller when ``controller`` is not given.
        controller: An existing controller to share; takes precedence over
            ``faults``.
        store_label: Optional label placed on emitted ``StoreError``s (defaults
            to the wrapped store's class name).

    Returns:
        The matching ``FaultInjecting*Store`` wrapper.

    Raises:
        TypeError: If ``store`` is not a known Data_Layer interface.
    """
    if controller is None:
        if faults is None:
            seed: list[Fault] = []
        elif isinstance(faults, Fault):
            seed = [faults]
        else:
            seed = list(faults)
        controller = FaultController(faults=seed)

    for interface, wrapper_cls in _WRAPPERS:
        if isinstance(store, interface):
            return wrapper_cls(store, controller, store_label=store_label)

    raise TypeError(
        f"{type(store).__name__} is not a known Data_Layer store interface"
    )


__all__ = [
    "Fault",
    "fail_on",
    "FaultController",
    "FaultInjectingAppointmentStore",
    "FaultInjectingPatientStore",
    "FaultInjectingWaitlistStore",
    "FaultInjectingDecisionStore",
    "FaultInjectingClinicKnowledgeBaseStore",
    "FaultInjectingCallSessionStore",
    "FaultInjectingEscalationStore",
    "FaultInjectingClinicDocumentStore",
    "wrap",
]
