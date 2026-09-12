"""``VoiceFrontDeskAgent`` — end-to-end Voice_Front_Desk wiring (task 9.2).

Task 9.2 (Req 2.4, 3.2, 11.2, 12.3). See design "Voice_Front_Desk (Strands
BidiAgent + Nova Sonic)". This module is the composition root that ties the
already-built voice sub-components together into one agent:

- the **ten patient-facing Strands tools** bound to the Data_Layer stores and
  registered with the Strands ``BidiAgent`` (through
  :class:`~clinic_front_desk.voice.stream.NovaSonicVoiceStream`),
- the **administrative-only guardrail system prompt**
  (:data:`~clinic_front_desk.voice.prompts.ADMINISTRATIVE_ONLY_SYSTEM_PROMPT`)
  attached to that agent,
- and the per-call orchestration —
  :class:`~clinic_front_desk.voice.session_context.SessionContext`,
  :class:`~clinic_front_desk.voice.tool_orchestrator.ToolOrchestrator`,
  :class:`~clinic_front_desk.voice.turn_controller.TurnController`, and
  :class:`~clinic_front_desk.voice.barge_in.BargeInHandler` — connected to the
  :class:`~clinic_front_desk.voice.stream.VoiceStreamManager`'s interpreted-turn
  and barge-in hooks.

The ten patient-facing tools (task 9.2)
----------------------------------------
``match_offered_service`` (the service-matcher-backed booking entry point),
``check_availability``, ``book_appointment``, ``reschedule``, ``cancel``,
``lookup_patient``, ``answer_faq``, ``add_to_waitlist``, and ``flag_for_human``.

``fill_gap_from_waitlist`` is **doctor-approved** (it is executed only when the
Doctor approves a gap-fill Decision, Req 8.2) and ``analyze_patterns`` belongs
to the autonomous Practice_Intelligence agent, so neither is part of the
patient-facing set registered here — matching the design's split of the ten
tools across the two agents.

Testability
-----------
All Strands / Nova Sonic specifics stay behind the
:class:`~clinic_front_desk.voice.stream.NovaSonicVoiceStream` adapter and the
:class:`~clinic_front_desk.voice.stream.VoiceStream` Protocol. The agent depends
only on the Protocol, so a :class:`VoiceSession` can be driven end to end with a
fake stream and in-memory stores — no real Bedrock connection required. The
Strands ``@tool`` definitions themselves are built locally (no network), so tool
registration is assertable in a unit test too.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from strands import tool

from clinic_front_desk.data_layer.interfaces import (
    AppointmentStore,
    CallSessionStore,
    ClinicKnowledgeBaseStore,
    EscalationStore,
    PatientStore,
    WaitlistStore,
)
from clinic_front_desk.models import (
    Appointment,
    AppointmentStatus,
    CallOutcome,
    CallSession,
    Err,
    EscalationReason,
    ISODate,
    ISODateTime,
    Ambiguous,
    NotFound,
    Ok,
    Patient,
    PatientRef,
    Slot,
    StoreFailure,
    StoreResult,
    ToolResult,
    is_err,
    is_ok,
)
from clinic_front_desk.tools.appointments import (
    BookingResult,
    CancelResult,
    RescheduleResult,
    book_appointment,
    cancel,
    reschedule,
)
from clinic_front_desk.tools.availability import (
    DEFAULT_AVAILABILITY_LIMIT,
    check_availability,
    closed_weekday_name,
)
from clinic_front_desk.tools.escalation import flag_for_human
from clinic_front_desk.tools.faq import answer_faq
from clinic_front_desk.tools.patients import (
    IntakeOutcome,
    create_patient,
    lookup_patient,
    record_intake,
)
from clinic_front_desk.tools.service_matcher import match_offered_service, offered_service_names
from clinic_front_desk.tools.waitlist import add_to_waitlist

from .barge_in import BargeInHandler
from .guardrails import GuardrailDecision, GuardrailPolicy, Turn
from .prompts import ADMINISTRATIVE_ONLY_SYSTEM_PROMPT
from .session_context import SessionContext
from .session_lifecycle import finalize_session
from .stream import (
    BargeInStopTiming,
    InterpretedTurn,
    NovaSonicVoiceStream,
    VoiceStream,
    VoiceStreamManager,
)
from .tool_orchestrator import (
    ChainContext,
    ChainOutcome,
    ChainStep,
    ToolOrchestrator,
)
from .turn_controller import EndSession, Escalate, TurnAction, TurnController
from .turn_signals import extract_turn, offers_escalation

if TYPE_CHECKING:  # pragma: no cover - keeps the documents package off the hot path
    from clinic_front_desk.documents.retrieval import DocumentKnowledge

__all__ = [
    "PATIENT_FACING_TOOL_NAMES",
    "VoiceFrontDeskStores",
    "BoundToolset",
    "build_patient_facing_tools",
    "VoiceSession",
    "VoiceFrontDeskAgent",
    "create_voice_front_desk_agent",
]


#: The names of the ten patient-facing tools registered with the ``BidiAgent``
#: (task 9.2). ``fill_gap_from_waitlist`` (doctor-approved) and
#: ``analyze_patterns`` (Practice_Intelligence) are deliberately excluded.
PATIENT_FACING_TOOL_NAMES: tuple[str, ...] = (
    "match_offered_service",
    "register_patient",
    "check_availability",
    "list_appointments",
    "book_appointment",
    "reschedule",
    "cancel",
    "lookup_patient",
    "answer_faq",
    "add_to_waitlist",
    "flag_for_human",
)


def _now_iso() -> ISODateTime:
    """Current UTC timestamp as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Store bundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VoiceFrontDeskStores:
    """The Data_Layer stores the Voice_Front_Desk depends on (Req 16.1).

    The agent and its tools depend only on these narrow interfaces, never on a
    concrete storage backend, so the whole agent can be wired against in-memory
    fakes for tests and DynamoDB in production by swapping the implementations
    (Req 16.5).
    """

    appointments: AppointmentStore
    patients: PatientStore
    waitlist: WaitlistStore
    escalations: EscalationStore
    knowledge_base: ClinicKnowledgeBaseStore
    call_sessions: CallSessionStore
    #: The doctor's uploaded documents, searched only as an ``answer_faq``
    #: fallback for descriptive questions the configuration has no field for.
    #: ``None`` — the default — means the agent answers purely from configuration,
    #: so a deployment with no uploads behaves exactly as before.
    documents: DocumentKnowledge | None = None


# ---------------------------------------------------------------------------
# BoundToolset — the pure tool functions bound to their stores.
# ---------------------------------------------------------------------------


#: How far back to look when checking whether this call already escalated for a
#: given reason. Generous relative to one call's worth of escalations, and the
#: in-memory ``_escalated`` set is the primary guard — this read only needs to
#: recover the existing record to hand back to the caller.
_ESCALATION_LOOKBACK = 200


class BoundToolset:
    """The Strands tool suite bound to a set of Data_Layer stores.

    Each method wraps the corresponding pure tool function (from
    :mod:`clinic_front_desk.tools`) with its store(s) pre-supplied and returns
    the tool's discriminated :data:`~clinic_front_desk.models.ToolResult`. These
    bound callables are what the :class:`ToolOrchestrator` chains (Req 11.2) and
    what the Strands ``@tool`` wrappers in :func:`build_patient_facing_tools`
    delegate to, so store binding lives in exactly one place.
    """

    def __init__(self, stores: VoiceFrontDeskStores) -> None:
        self.stores = stores
        # (call_session_id, reason) pairs already escalated through this toolset.
        #
        # Two independent paths call flag_for_human: the deterministic guardrail
        # backstop, and the model choosing to call the tool itself. Both are
        # wanted — that is the defence in depth — but a caller asking once should
        # produce one handover, not two. Observed live: a single request for a
        # human recorded two escalations a second apart, one from each path.
        # Deduping here rather than in either caller is what makes it hold
        # whichever path fires first.
        self._escalated: set[tuple[str, EscalationReason]] = set()
        # The Call_Session these tools act for, bound when a session starts.
        #
        # This is deliberately *not* a model-supplied tool argument. When it was,
        # the model had no way to learn the real id and invented one — so a
        # model-initiated escalation was filed against a Call_Session that did not
        # exist, could not be correlated in the activity log, and defeated the
        # per-call dedupe (one request for a human produced two handovers, one per
        # id). Binding it here is the same reasoning that keeps stores out of the
        # model-facing schema: the model should only supply what it actually knows.
        self._session_id: str = ""
        # What the most recent register_patient call actually wrote. Held here
        # rather than folded into the Patient it returns, because the record alone
        # cannot say whether *this* call stored anything — a field already on file
        # looks identical to one just saved. That ambiguity is what let the agent
        # confirm details to a caller that had been silently discarded.
        self._last_intake: IntakeOutcome | None = None
        # The Call_Session context to record outcomes on, bound when a session
        # starts. None outside a session.
        self._context: SessionContext | None = None

    def bind_context(self, context: "SessionContext") -> None:
        """Bind the Call_Session context these tools record outcomes on."""
        self._context = context

    def _record_outcome(self, outcome: CallOutcome) -> None:
        """Record a completed task on the bound context, if there is one.

        Silent when unbound: a toolset used outside a session (tests, the
        dashboard's gap-fill path) has no call to describe.
        """
        if self._context is not None:
            self._context.record_outcome(outcome)

    def take_last_intake(self) -> IntakeOutcome | None:
        """Consume the outcome of the last ``register_patient`` call.

        Cleared on read so a later confirmation can never reuse an earlier call's
        result and claim a write that did not happen this time.
        """
        outcome, self._last_intake = self._last_intake, None
        return outcome

    def bind_session(self, session_id: str) -> None:
        """Bind the Call_Session these tools record against."""
        self._session_id = session_id

    @property
    def session_id(self) -> str:
        """The bound Call_Session id (empty before a session starts)."""
        return self._session_id

    # -- clinic-config-derived helpers --------------------------------------

    def offered_service_names(self) -> list[str]:
        """The clinic's currently offered service names (empty if unconfigured)."""
        result = self.stores.knowledge_base.get()
        if isinstance(result, Err) or result.value is None:
            return []
        return offered_service_names(result.value)

    def configured_provider_ids(self) -> list[str]:
        """The configured Provider ids used to default availability searches (Req 2.2)."""
        result = self.stores.knowledge_base.get()
        if isinstance(result, Err) or result.value is None:
            return []
        return [provider.id for provider in result.value.providers]

    # -- the ten patient-facing tools --------------------------------------

    def match_service(self, named_service: str) -> ToolResult[str]:
        """Resolve a patient-named service to an offered service (Req 2.1, 2.9)."""
        return match_offered_service(named_service, self.offered_service_names())

    def check_availability(
        self,
        *,
        service: str,
        provider_ids: Sequence[str] | None = None,
        from_date: ISODate | None = None,
        limit: int = DEFAULT_AVAILABILITY_LIMIT,
        from_time: str | None = None,
    ) -> ToolResult[list[Slot]]:
        """Retrieve open slots for a matched service (Req 2.2, 2.3, 4.4).

        The clinic's other offered services are passed as ``also_published_as``: a
        published slot is the doctor's time, and its service label is only
        whatever the last publish of that day happened to use. Without this, a
        year published as one service reports every other service as fully
        booked.

        ``from_time`` carries the hour the caller asked for, which matters because
        this clinic publishes midnight to midnight: the earliest three slots of
        any day are 00:00, 00:30 and 01:00.

        No service list is read here. Availability searches the provider's own
        calendar and ignores the label a day was published under, so the extra
        configuration read this used to make was a round trip spent on an argument
        that is now discarded — and this call sits on the path where every
        millisecond decides whether the model has an answer before it speaks.
        """
        ids = list(provider_ids) if provider_ids is not None else self.configured_provider_ids()
        return check_availability(
            self.stores.appointments,
            service=service,
            provider_ids=ids,
            from_date=from_date,
            limit=limit,
            from_time=from_time,
        )

    def open_weekdays(self) -> frozenset[int]:
        """Weekday indices the clinic has configured hours for (Sunday = 0)."""
        result = self.stores.knowledge_base.get()
        if isinstance(result, Err) or result.value is None:
            return frozenset()
        return frozenset(
            day for day, hours in result.value.hours.items() if hours is not None
        )

    def closed_weekday(self, on_date: ISODate | None) -> str | None:
        """The weekday name when ``on_date`` is a day the clinic does not open.

        Read from the configured hours, so it stays true if the doctor changes
        which days the clinic works. ``None`` when the date is open, unparseable,
        or when no hours are configured at all — none of which justify telling a
        caller the clinic is shut.
        """
        if not on_date:
            return None
        return closed_weekday_name(on_date, self.open_weekdays())

    def find_by_code(self, code: str) -> ToolResult[list[Patient]]:
        """Patients whose short code matches, for a caller who quotes theirs.

        A list, because five characters can collide. The caller of this must
        disambiguate rather than take the first — one patient shown another's
        appointments is worse than one asked to repeat their name.
        """
        result = self.stores.patients.find_by_code(code)
        if is_err(result):
            return Err(StoreFailure(store="PatientStore", detail=result.error.detail))
        return Ok(result.value)

    def list_appointments(
        self, *, name: str = "", callback_phone: str = "", code: str = ""
    ) -> ToolResult[list[Appointment]]:
        """The caller's bookings, so they can be moved or cancelled.

        Without this, ``reschedule`` and ``cancel`` were unreachable. Both need an
        appointment id, no caller has ever memorised one, and nothing else could
        produce one from a name and a number. Observed live: a caller asked to move
        her 9:00 appointment, the agent found her patient record, could not find the
        booking, asked her for a "reference number", and then offered to "pull up
        your full appointment history" — a thing it had no way to do. She spelled
        her name out twice and the call went nowhere.

        Only bookings that can still be acted on are returned. Offering a cancelled
        appointment as something to move would waste the caller's time on a request
        that must fail.
        """
        if code:
            by_code = self.find_by_code(code)
            if is_err(by_code):
                return by_code
            if not by_code.value:
                return Err(NotFound(detail=f"no patient with code {code!r}"))
            if len(by_code.value) > 1:
                # Never guess between them, and never surface the other patients'
                # names: reading one caller a different patient's name to pick from
                # would leak it. Opaque ids only — the agent asks for the caller's
                # own name and looks them up that way.
                return Err(Ambiguous(candidates=sorted(p.id for p in by_code.value)))
            patients = by_code.value
        else:
            found = lookup_patient(self.stores.patients, name, callback_phone)
            if is_err(found):
                return found
            if not found.value:
                return Err(NotFound(detail=f"no patient record for {name!r}"))
            patients = found.value

        collected: list[Appointment] = []
        for patient in patients:
            result = self.stores.appointments.list_by_patient(patient.id)
            if is_err(result):
                return Err(
                    StoreFailure(store="AppointmentStore", detail=result.error.detail)
                )
            collected.extend(
                appointment
                for appointment in result.value
                if appointment.status == AppointmentStatus.BOOKED
            )
        collected.sort(key=lambda a: (a.date, a.time, a.id))
        return Ok(collected)

    def book_appointment(
        self,
        *,
        provider_id: str,
        patient_id: str,
        slot_id: str,
        service: str,
        appointment_id: str | None = None,
    ) -> ToolResult[BookingResult]:
        """Write an appointment and book its slot (Req 2.5, 2.6, 2.8).

        Records ``booked`` on the call so the Call_Session says what the call
        achieved. The orchestrated ``book`` chain already did this; the model
        calling this tool directly — which is what happens on a real call — did
        not, so completed bookings were persisted as abandoned calls.
        """
        result = book_appointment(
            self.stores.appointments,
            provider_id=provider_id,
            patient_id=patient_id,
            slot_id=slot_id,
            service=service,
            appointment_id=appointment_id,
        )
        if not is_err(result):
            self._record_outcome(CallOutcome.BOOKED)
        return result

    def reschedule(self, *, appointment_id: str, new_slot_id: str) -> ToolResult[RescheduleResult]:
        """Move an appointment to a new slot (Req 4.7, 4.8, 4.9)."""
        result = reschedule(
            self.stores.appointments,
            appointment_id=appointment_id,
            new_slot_id=new_slot_id,
        )
        if not is_err(result):
            self._record_outcome(CallOutcome.RESCHEDULED)
        return result

    def cancel(self, *, appointment_id: str) -> ToolResult[CancelResult]:
        """Remove an appointment and release its slot (Req 5.5, 5.7, 5.8)."""
        result = cancel(self.stores.appointments, appointment_id=appointment_id)
        if not is_err(result):
            self._record_outcome(CallOutcome.CANCELLED)
        return result

    def lookup_patient(
        self,
        *,
        name: str,
        callback_phone: str,
        extra_identifiers: Mapping[str, str] | None = None,
    ) -> ToolResult[list[Patient]]:
        """Retrieve patients matching name + callback phone (Req 3.1, 3.6)."""
        return lookup_patient(self.stores.patients, name, callback_phone, extra_identifiers)

    def register_patient(
        self,
        *,
        name: str,
        callback_phone: str,
        age: int | None = None,
        blood_group: str | None = None,
        weight_kg: float | None = None,
        height_cm: float | None = None,
    ) -> ToolResult[Patient]:
        """Find or create the patient record for a booking, with intake details.

        Returns the existing record when name and phone already match one, so a
        returning patient is not duplicated on every call — and fills in any
        intake field that record is still missing.

        It used to return the existing record untouched. On a live call the caller
        booked first and offered her blood group, height and weight afterwards;
        registration found her record, changed nothing, and reported success, so
        the agent told her all three were on file when the record held ``None``
        for every one of them. Not writing is a defensible policy; reporting
        success for a write that did not happen is not.

        A field that already holds a value is still never overwritten — the person
        on the phone may not be the person whose record it is — and the outcome
        names exactly which fields were written, so the agent can only confirm what
        actually happened.
        """
        found = lookup_patient(self.stores.patients, name, callback_phone)
        if is_err(found):
            return found
        if found.value:
            outcome = record_intake(
                self.stores.patients,
                found.value[0],
                age=age,
                blood_group=blood_group,
                weight_kg=weight_kg,
                height_cm=height_cm,
            )
            if is_err(outcome):
                return outcome
            self._last_intake = outcome.value
            return Ok(outcome.value.patient)
        created = create_patient(
            self.stores.patients,
            name,
            callback_phone,
            age=age,
            blood_group=blood_group,
            weight_kg=weight_kg,
            height_cm=height_cm,
        )
        if not is_err(created):
            self._last_intake = _intake_of_new_record(
                created.value,
                age=age,
                blood_group=blood_group,
                weight_kg=weight_kg,
                height_cm=height_cm,
            )
        return created

    def create_patient(
        self,
        *,
        name: str,
        callback_phone: str,
        extra_identifiers: Mapping[str, str] | None = None,
    ) -> ToolResult[Patient]:
        """Create a new patient record (Req 3.4)."""
        return create_patient(self.stores.patients, name, callback_phone, extra_identifiers)

    def answer_faq(
        self,
        *,
        topic: str,
        service: str | None = None,
        question: str | None = None,
    ) -> ToolResult[str]:
        """Answer a clinic FAQ from the knowledge base, then the documents (Req 6.1-6.6).

        The document corpus is passed *into* the tool rather than exposed as a
        separate retrieval tool on purpose. A second tool would let the model
        choose retrieval over the configured answer — and then a passage of prose
        could outrank the price or the offered-service list the doctor actually
        entered. Keeping it inside ``answer_faq`` makes configuration-first the
        only reachable order.
        """
        return answer_faq(
            self.stores.knowledge_base,
            topic,
            service,
            question=question,
            documents=self.stores.documents,
        )

    def add_to_waitlist(
        self,
        *,
        patient_id: str,
        service: str,
        preferred_slot_type: str,
    ) -> ToolResult[Any]:
        """Record a waitlist entry, suppressing an active duplicate (Req 7.1-7.5)."""
        return add_to_waitlist(
            self.stores.waitlist,
            patient_id=patient_id,
            service=service,
            preferred_slot_type=preferred_slot_type,
        )

    def flag_for_human(
        self,
        *,
        reason: EscalationReason,
        call_session_id: str,
        context: str,
        patient_ref: PatientRef | None = None,
    ) -> ToolResult[Any]:
        """Record an escalation routing a request to a human (Req 9.4, 9.9).

        Idempotent per ``(call_session_id, reason)``: a repeat for the same call
        and reason returns the already-recorded escalation instead of writing a
        second one. See :attr:`_escalated` for why.
        """
        key = (call_session_id, reason)
        if key in self._escalated:
            existing = self._find_existing_escalation(call_session_id, reason)
            if existing is not None:
                return Ok(existing)
        result = flag_for_human(
            self.stores.escalations,
            reason=reason,
            call_session_id=call_session_id,
            context=context,
            patient_ref=patient_ref,
        )
        if is_ok(result):
            self._escalated.add(key)
        return result

    def _find_existing_escalation(
        self, call_session_id: str, reason: EscalationReason
    ) -> Any | None:
        """Locate an already-recorded escalation for this call and reason.

        Scans the recent-escalation window, which is the only read the
        ``EscalationStore`` contract offers. Returning the existing record keeps
        the tool's success shape intact for the caller (the model expects an
        escalation back), while writing nothing new.
        """
        recent = self.stores.escalations.list_recent(_ESCALATION_LOOKBACK)
        if is_err(recent):
            return None
        for escalation in recent.value:
            if (
                escalation.call_session_id == call_session_id
                and escalation.reason == reason
            ):
                return escalation
        return None


# ---------------------------------------------------------------------------
# Strands @tool definitions bound to the stores.
# ---------------------------------------------------------------------------


def _intake_of_new_record(
    patient: Patient,
    *,
    age: int | None,
    blood_group: str | None,
    weight_kg: float | None,
    height_cm: float | None,
) -> IntakeOutcome:
    """Describe what a freshly created record actually kept.

    A new record accepts every valid detail, so "recorded" is whatever survived
    validation — and anything the caller offered that did *not* survive is
    reported as rejected rather than quietly missing, so the agent asks again
    instead of confirming a value the store dropped.
    """
    offered = {
        "age": age,
        "blood_group": blood_group,
        "weight_kg": weight_kg,
        "height_cm": height_cm,
    }
    recorded = tuple(
        field
        for field, value in offered.items()
        if value is not None and getattr(patient, field, None) is not None
    )
    rejected = tuple(
        field
        for field, value in offered.items()
        if value is not None and getattr(patient, field, None) is None
    )
    return IntakeOutcome(patient=patient, recorded=recorded, rejected=rejected)


def _payload(result: ToolResult[Any]) -> dict[str, Any]:
    """Render a :data:`ToolResult` as a JSON-friendly ``{ ok, ... }`` dict.

    The Strands ``BidiAgent`` receives tool results as data, so the discriminated
    ``Ok``/``Err`` union is flattened into the ``{ ok: true, value } | { ok:
    false, error: {...} }`` shape the design specifies for every tool.
    """
    if isinstance(result, Ok):
        return {"ok": True, "value": result.value}
    error = result.error
    detail = {"kind": error.kind}
    # Surface the error's own fields (store/detail/field/candidates/...) without
    # assuming a particular ToolError variant.
    for key, value in vars(error).items():
        if key != "kind":
            detail[key] = value
    return {"ok": False, "error": detail}


def build_patient_facing_tools(
    stores: VoiceFrontDeskStores, *, toolset: BoundToolset | None = None
) -> dict[str, Any]:
    """Build the ten patient-facing Strands tools bound to ``stores`` (task 9.2).

    Returns a mapping of tool name to the Strands ``@tool``-decorated definition,
    ready to register with a ``BidiAgent``. Each tool closes over ``stores`` (via
    a :class:`BoundToolset`) so no store argument leaks into the model-facing
    schema, and returns the design's ``{ ok, ... }`` result dict.

    Args:
        stores: The Data_Layer stores the tools read and write through.
        toolset: An existing :class:`BoundToolset` to bind to instead of creating
            one. The agent passes its own so the model-invoked tools and the
            guardrail backstop share a single instance — and therefore a single
            escalation-dedupe set, which is what stops one request for a human
            producing two handovers.

    ``fill_gap_from_waitlist`` and ``analyze_patterns`` are intentionally absent:
    the former is doctor-approved (Req 8.2) and the latter belongs to
    Practice_Intelligence.
    """
    toolset = toolset if toolset is not None else BoundToolset(stores)

    @tool(name="match_offered_service")
    def match_offered_service_tool(named_service: str) -> dict[str, Any]:
        """Match a service the patient explicitly named to an offered service.

        Routing is by exact offered-service name only; a symptom or unoffered
        name yields a not-offered result and selects nothing (Req 2.1, 2.9,
        10.2, 10.3).

        Args:
            named_service: The service name the patient explicitly said.
        """
        return _payload(toolset.match_service(named_service))

    @tool(name="check_availability")
    def check_availability_tool(
        service: str,
        from_date: str | None = None,
        limit: int = DEFAULT_AVAILABILITY_LIMIT,
        provider_id: str | None = None,
        from_time: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve up to ``limit`` open slots for an already-matched service.

        Args:
            service: A matched offered service (from ``match_offered_service``).
            Recorded on the appointment. It does not narrow the search: the
            doctor takes whichever ENT service is needed in whatever half hour is
            free.
            from_date: Earliest date to search (ISO ``YYYY-MM-DD``); defaults to today.
            limit: Maximum slots to offer (default 3, Req 2.3).
            provider_id: Optional single provider to search; defaults to all
                configured providers.
            from_time: The 24-hour clock time the patient asked for on
                ``from_date``, as ``HH:MM`` — pass "15:00" when they say
                "three in the afternoon". The clinic's calendar runs midnight to
                midnight, so omitting this offers slots starting at 00:00.

        When the result carries ``clinic_closed_on_requested_date``, the clinic does
        not open that weekday. Say so first, naming the day and calling it the
        clinic's holiday — "the thirteenth is a Sunday, that's our clinic holiday,
        we're closed" — and only then offer the slots, which are on later dates.
        Never report a closed day as "I could not find anything": that is what a
        fully booked day sounds like, and the caller gives up instead of taking the
        next working day.
        """
        provider_ids = [provider_id] if provider_id else None
        payload = _payload(
            toolset.check_availability(
                service=service,
                provider_ids=provider_ids,
                from_date=from_date,
                limit=limit,
                from_time=from_time,
            )
        )
        # The search is "on or after", so asking for a Sunday quietly returns
        # Monday's slots. Correct times, missing reason — the agent then reports it
        # found nothing for the requested date and the caller cannot tell whether
        # the clinic is shut, full, or the agent simply failed.
        closed_on = toolset.closed_weekday(from_date)
        if closed_on is not None and from_date:
            payload["requested_date"] = from_date
            payload["requested_weekday"] = closed_on
            payload["clinic_closed_on_requested_date"] = True
            payload["closed_notice"] = (
                f"{from_date} falls on a {closed_on}, which is the clinic's "
                f"holiday. The clinic is closed every {closed_on} and there are "
                "no appointments that day. Say this to the caller before "
                "offering anything else; any slots listed here are on later "
                "dates."
            )
        return payload

    @tool(name="list_appointments")
    def list_appointments_tool(
        name: str = "", callback_phone: str = "", code: str = ""
    ) -> dict[str, Any]:
        """Find the caller's existing appointments so one can be moved or cancelled.

        Call this FIRST whenever a caller wants to reschedule or cancel. It is the
        only way to obtain the ``appointment_id`` that ``reschedule`` and ``cancel``
        require. NEVER ask a caller for an appointment reference or booking number —
        those are long internal ids nobody has to hand.

        Identify them either way:
        - ``code`` — their short patient code, like "SA901", if they have it. One
          call, nothing to spell.
        - ``name`` + ``callback_phone`` — always works, and is the fallback when
          they have lost the code or never had one.

        Args:
            name: The caller's full name, as they said it.
            callback_phone: Their mobile number.
            code: Their short patient code, if they quote one.

        Returns each booking with its ``id``, ``date``, ``time`` and ``service``.
        An empty list means they have nothing booked — say so plainly and offer to
        book something, rather than asking them to prove it. An ``ambiguous`` error
        means the code matches more than one patient: ask for their full name, and
        never pick one yourself.
        """
        return _payload(
            toolset.list_appointments(
                name=name, callback_phone=callback_phone, code=code
            )
        )

    @tool(name="book_appointment")
    def book_appointment_tool(
        provider_id: str, patient_id: str, slot_id: str, service: str
    ) -> dict[str, Any]:
        """Book a confirmed slot as an appointment on the provider's calendar.

        Args:
            provider_id: The owning provider's id (required, Req 16.7).
            patient_id: The patient the appointment is for.
            slot_id: The open slot the patient confirmed.
            service: The matched offered service.
        """
        return _payload(
            toolset.book_appointment(
                provider_id=provider_id, patient_id=patient_id, slot_id=slot_id, service=service
            )
        )

    @tool(name="reschedule")
    def reschedule_tool(appointment_id: str, new_slot_id: str) -> dict[str, Any]:
        """Move an existing appointment onto a new slot, releasing the old one.

        Args:
            appointment_id: The appointment to move.
            new_slot_id: The confirmed replacement slot.
        """
        return _payload(toolset.reschedule(appointment_id=appointment_id, new_slot_id=new_slot_id))

    @tool(name="cancel")
    def cancel_tool(appointment_id: str) -> dict[str, Any]:
        """Cancel an appointment and release its slot after patient confirmation.

        Args:
            appointment_id: The appointment to cancel.
        """
        return _payload(toolset.cancel(appointment_id=appointment_id))

    @tool(name="lookup_patient")
    def lookup_patient_tool(
        name: str, callback_phone: str, extra_identifiers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Look up patient records by the name and callback phone provided.

        Args:
            name: The patient-provided name.
            callback_phone: The patient-provided callback phone number.
            extra_identifiers: Optional extra identifiers to disambiguate multiple
                matches (Req 3.6).
        """
        return _payload(
            toolset.lookup_patient(
                name=name, callback_phone=callback_phone, extra_identifiers=extra_identifiers
            )
        )

    @tool(name="answer_faq")
    def answer_faq_tool(
        topic: str, service: str | None = None, question: str | None = None
    ) -> dict[str, Any]:
        """Answer a question about the clinic from clinic records, without fabricating.

        Use the specific topic when one fits. Use ``clinic_info`` for anything else
        about the practice — parking, which floor or suite, holiday closures,
        accessibility, cancellation or late policy, what the practice does — and
        pass the caller's own words as ``question``. A not-found result means the
        clinic has not provided that information: say so, do not fill it in.

        Args:
            topic: One of hours, location, what_to_bring, prep, insurance,
                pricing, or clinic_info for any other clinic detail.
            service: Required for pricing; scopes prep/what_to_bring when given.
            question: The caller's question verbatim. Required for clinic_info,
                and worth passing for every topic — it is what finds the right
                detail in the clinic's own documents.
        """
        return _payload(
            toolset.answer_faq(topic=topic, service=service, question=question)
        )

    @tool(name="add_to_waitlist")
    def add_to_waitlist_tool(
        patient_id: str, service: str, preferred_slot_type: str
    ) -> dict[str, Any]:
        """Add the patient to the waitlist for a full service/slot type.

        Args:
            patient_id: The patient requesting waitlist placement.
            service: The matched offered service.
            preferred_slot_type: The slot type the patient prefers.
        """
        return _payload(
            toolset.add_to_waitlist(
                patient_id=patient_id, service=service, preferred_slot_type=preferred_slot_type
            )
        )

    @tool(name="flag_for_human")
    def flag_for_human_tool(
        reason: str, context: str, patient_id: str | None = None
    ) -> dict[str, Any]:
        """Escalate a request to a human and record it (Req 9.4, 9.9).

        The Call_Session id is bound from the active session, not passed in — the
        model cannot know it, and inventing one filed escalations against a
        non-existent call.

        Args:
            reason: One of clinical_content, outside_admin_rules, patient_distress,
                patient_request.
            context: Free-text description of the escalated request.
            patient_id: The patient id when known.
        """
        patient_ref = PatientRef(patient_id=patient_id) if patient_id else None
        return _payload(
            toolset.flag_for_human(
                reason=EscalationReason(reason),
                call_session_id=toolset.session_id,
                context=context,
                patient_ref=patient_ref,
            )
        )

    @tool(name="register_patient")
    def register_patient_tool(
        name: str,
        callback_phone: str,
        age: int | None = None,
        blood_group: str | None = None,
        weight_kg: float | None = None,
        height_cm: float | None = None,
    ) -> dict[str, Any]:
        """Register the patient for a booking and get their patient id.

        Call this once you have the caller's name and mobile number, before
        booking. It returns the existing record if there is one, so a returning
        patient is not duplicated. Call it again later if the caller offers a
        detail after booking — a blank field will be filled in.

        Name and mobile number are required — without them the clinic cannot tell
        two patients apart or call anyone back. The rest are optional: ask for
        them once, politely, and if the caller does not want to give one, leave it
        out and carry on booking. Never refuse or delay an appointment over them,
        and never read them back aloud.

        The result carries ``recorded``, ``already_on_file`` and ``rejected``.
        Confirm ONLY the fields listed in ``recorded``. A field in ``rejected`` was
        not stored because the value did not look like a real measurement — ask for
        that one again. Never tell a caller a detail is on file unless this tool
        listed it in ``recorded``.

        Args:
            name: The caller's full name, as they said it.
            callback_phone: Their mobile number.
            age: Age in years, if given.
            blood_group: Blood group as spoken (e.g. "A positive", "O negative").
            weight_kg: Weight in kilograms, if given.
            height_cm: Height in centimetres, if given.

        The result carries ``code`` — the patient's short code, like "SA901". Read
        it back to them when the booking is confirmed and tell them to keep it. It
        is the only identifier they can write down; the appointment reference is a
        thirty-six character id and asking for it later gets you nowhere.
        """
        payload = _payload(
            toolset.register_patient(
                name=name,
                callback_phone=callback_phone,
                age=age,
                blood_group=blood_group,
                weight_kg=weight_kg,
                height_cm=height_cm,
            )
        )
        record = payload.get("value")
        if payload.get("ok") and record is not None:
            # Lifted out beside the record so the model cannot miss it. This is the
            # one identifier a caller can actually write down, and reading it back
            # is what saves them spelling their name on the next call.
            payload["code"] = getattr(record, "code", "")
        intake = toolset.take_last_intake()
        if payload.get("ok") and intake is not None:
            # Spelled out beside the record, because the record alone does not say
            # whether *this* call wrote anything — which is how a caller came to be
            # told her blood group was saved when it had been silently discarded.
            payload["recorded"] = list(intake.recorded)
            payload["already_on_file"] = list(intake.already_on_file)
            payload["rejected"] = list(intake.rejected)
        return payload

    definitions: list[Any] = [
        match_offered_service_tool,
        register_patient_tool,
        check_availability_tool,
        list_appointments_tool,
        book_appointment_tool,
        reschedule_tool,
        cancel_tool,
        lookup_patient_tool,
        answer_faq_tool,
        add_to_waitlist_tool,
        flag_for_human_tool,
    ]
    return {definition.tool_name: definition for definition in definitions}


# ---------------------------------------------------------------------------
# VoiceSession — one Call_Session's orchestration, wired to the stream.
# ---------------------------------------------------------------------------


#: The service offered to a caller who describes a symptom or cannot say which
#: service they need.
#:
#: A consultation is the appointment at which the doctor works out what is needed,
#: so offering it answers "which service should I book?" without interpreting
#: anything about the caller's symptom — it is the same answer whatever they
#: describe. Ignored unless the clinic has a service by this exact name, because an
#: offer the clinic cannot honour is worse than no offer.
GENERAL_CONSULTATION = "ENT Consultation"

#: Selects a slot from the availability offers (default: the earliest offered).
SlotSelector = Callable[[Sequence[Slot]], "Slot | None"]


def _earliest_slot(slots: Sequence[Slot]) -> Slot | None:
    """Default slot selection: the first (earliest) offered slot, or ``None``."""
    return slots[0] if slots else None


class VoiceSession:
    """One active Call_Session's orchestration wired to a voice stream (task 9.2).

    Composes the per-call state — :class:`SessionContext`,
    :class:`TurnController`, :class:`BargeInHandler`, :class:`ToolOrchestrator` —
    and connects them to a :class:`VoiceStreamManager`'s interpreted-turn and
    barge-in hooks. Created via :meth:`VoiceFrontDeskAgent.start_session`.

    Wiring summary:

    - **Interpreted turns (Req 12.4-12.6):** a finalized user transcript means
      Nova Sonic interpreted the speech, so it resets the per-request
      interpretation-failure / silence bounds on the :class:`TurnController`. If
      a barge-in is awaiting resume, receiving the interruption's turn means the
      interruption has been processed, so the pre-interruption task step is
      restored (Req 12.3).
    - **Barge-in (Req 12.3):** the manager stops playback within budget
      (Req 12.2); this session captures the current task step on the
      :class:`BargeInHandler` so the task resumes from where it was interrupted.
    - **Guardrail (Req 10):** :meth:`classify_turn` runs the
      :class:`GuardrailPolicy` over a turn and auto-escalates via
      ``flag_for_human`` when the classification requires it.
    - **Tool chaining (Req 11.2):** :meth:`book` runs the lookup -> availability
      -> book chain through the :class:`ToolOrchestrator`, threading each tool's
      output into the next and retaining context on a mid-chain failure
      (Req 11.3).
    """

    def __init__(
        self,
        agent: VoiceFrontDeskAgent,
        session_id: str,
        stream: VoiceStream,
    ) -> None:
        self.agent = agent
        self.session_id = session_id
        self.context = SessionContext(session_id=session_id)
        # Let the model-invoked tools record the call's outcome on this context.
        #
        # Without it, only the orchestrated `book`/`cancel`/`reschedule` chains
        # ever set an outcome — and on a real call the model calls the tools
        # directly, so a completed booking was persisted as `interrupted`. Checked
        # against the live table: a call that booked 2026-09-11 13:00 and read the
        # reference back to the caller is stored as an abandoned call, which makes
        # the doctor's booking count zero however many appointments were taken.
        self.agent.toolset.bind_context(self.context)
        self.turn_controller = TurnController()
        self.barge_in = BargeInHandler()
        self.orchestrator = ToolOrchestrator(self.context)
        self.manager = VoiceStreamManager(
            stream,
            on_interpreted_turn=self._on_interpreted_turn,
            on_barge_in=self._on_barge_in,
        )
        self._finalized = False
        # Guardrail state (task 7.3 wiring). Tracks an outstanding escalation
        # offer — made by the guardrail on distress, or by the model in its own
        # words — so a following "yes" accepts it (Req 9.3 → 9.8). Handover
        # de-duplication lives on the shared BoundToolset, not here, so it covers
        # the model calling flag_for_human directly too.
        self._escalation_offered = False
        #: The most recent guardrail decision, for inspection and tests.
        self.last_guardrail_decision: GuardrailDecision | None = None

    # -- convenience accessors ---------------------------------------------

    @property
    def toolset(self) -> BoundToolset:
        """The store-bound tool suite shared from the parent agent."""
        return self.agent.toolset

    @property
    def guardrail(self) -> GuardrailPolicy:
        """The parent agent's guardrail policy."""
        return self.agent.guardrail

    # -- stream lifecycle ---------------------------------------------------

    async def start(self) -> None:
        """Open the underlying voice stream for this Call_Session."""
        await self.manager.start()

    async def run(self) -> None:
        """Consume stream events until the connection closes."""
        await self.manager.run()

    async def stop(self) -> None:
        """Close the voice stream (does not itself finalize the outcome)."""
        await self.manager.stop()

    # -- stream hooks (the wiring) -----------------------------------------

    def _on_interpreted_turn(self, turn: InterpretedTurn) -> None:
        """Handle a finalized interpreted turn from the stream (Req 12.3-12.6)."""
        if turn.role != "user":
            # The agent's own speech is not patient input, but it *is* how we learn
            # the model offered a handover on its own initiative — which arms the
            # "a following yes accepts it" path (Req 9.8). Without this, a caller
            # answering the model's own offer is never escalated.
            if turn.role == "assistant" and offers_escalation(turn.text or ""):
                self._escalation_offered = True
            return
        # A finalized user transcript = speech was interpreted successfully, so
        # the per-request interpretation-failure and silence bounds reset.
        self.turn_controller.on_interpretable_turn()
        # If a barge-in is outstanding, this turn is the processed interruption;
        # resume the interrupted task from its pre-interruption step (Req 12.3).
        if self.barge_in.interrupted:
            self.barge_in.resume(self.context)
        # Run the guardrail over what the patient actually said. This is the
        # tool-layer backstop (Req 10.4, 10.5, 9.1, 9.2, 9.7, 9.8): it does not
        # depend on the model choosing to call flag_for_human, so clinical content
        # escalates and is recorded even when the model would have answered it.
        self.apply_guardrail(turn.text)

    def apply_guardrail(self, transcript: str) -> GuardrailDecision | None:
        """Classify a patient transcript and escalate when the guardrail requires it.

        Extracts structured signals from the utterance
        (:func:`~clinic_front_desk.voice.turn_signals.extract_turn`), classifies
        them, and — via :meth:`classify_turn` — records an escalation when one is
        required. Also tracks whether an escalation *offer* is outstanding so a
        following "yes" is read as accepting it (Req 9.3 → 9.8).

        Returns the decision, or ``None`` for an empty transcript.
        """
        if not transcript or not transcript.strip():
            return None
        extracted = extract_turn(
            transcript,
            self.guardrail.offered_services,
            escalation_offered=self._escalation_offered,
        )
        decision = self.classify_turn(extracted.turn, context_text=extracted.describe())
        # A distress turn offers escalation rather than escalating (Req 9.3); the
        # offer stays open until the patient accepts or the guardrail escalates.
        # An offer the *model* made (seen on its transcript) also counts, so only
        # clear the flag once the turn was actually resolved one way or the other.
        if decision.offer_escalation:
            self._escalation_offered = True
        elif decision.requires_escalation or decision.is_administrative:
            self._escalation_offered = False
        self.last_guardrail_decision = decision
        return decision

    def _on_barge_in(self, timing: BargeInStopTiming) -> None:
        """Handle a barge-in reported by the stream manager (Req 12.3).

        The manager has already stopped playback within the ≤ 500 ms budget
        (Req 12.2). Here we preserve the current task step so the task resumes
        from exactly where it was interrupted once the interruption is processed.
        The accumulated :class:`SessionContext` facts are never cleared.
        """
        self.barge_in.on_barge_in(self.context)

    # -- guardrail ----------------------------------------------------------

    def classify_turn(
        self, turn: Turn, *, context_text: str | None = None
    ) -> GuardrailDecision:
        """Classify a patient turn and auto-escalate when required (Req 10).

        Runs the :class:`GuardrailPolicy`. When the decision requires escalation
        (clinical content, symptom-only routing, an explicit human request, or an
        out-of-rules request) ``flag_for_human`` is invoked with the session
        context so the request is routed to a human (Req 10.4, 9.x). Any offered
        service the patient named is recorded on the context (Req 2.1, 10.2).

        Args:
            turn: The structured signals of the patient's turn.
            context_text: Optional handover context recorded on the escalation.
                Defaults to the classification name; :meth:`apply_guardrail`
                passes the transcript plus the signals that fired, so a human
                picking up the call can see why it escalated.

        The escalation is recorded **once per reason per call**: a caller who
        keeps describing symptoms across several turns needs one handover, not one
        per sentence. That de-duplication lives in
        :meth:`BoundToolset.flag_for_human` rather than here, so it also covers the
        case where the *model* calls the tool itself.
        """
        decision = self.guardrail.classify(turn)
        if decision.selected_service is not None:
            self.context.set_requested_service(decision.selected_service)
        if decision.requires_escalation and decision.escalation_reason is not None:
            self._escalate(
                decision.escalation_reason,
                context_text or decision.classification.value,
            )
            # An escalated call ends as `escalated` rather than `interrupted`
            # (Req 11.5), unless a task already completed and set an outcome.
            if self.context.outcome is None:
                self.context.record_outcome(CallOutcome.ESCALATED)
        return decision

    # -- turn-level events --------------------------------------------------

    def on_interpretation_failure(self) -> TurnAction:
        """Bounded interpretation-failure handling; escalate on the 2nd (Req 12.4, 12.5)."""
        action = self.turn_controller.on_interpretation_failure()
        if isinstance(action, Escalate):
            self._escalate(action.reason, "interpretation failure limit reached")
        return action

    def on_silence_timeout(self) -> TurnAction:
        """Re-prompt once after 10 s of silence (Req 12.6)."""
        return self.turn_controller.on_silence_timeout()

    def on_voice_layer_lost(self) -> TurnAction:
        """End and record the session as interrupted on voice-layer loss (Req 12.7)."""
        action = self.turn_controller.on_voice_layer_lost()
        if isinstance(action, EndSession):
            self.finalize(action.outcome)
        return action

    # -- escalation + finalize ---------------------------------------------

    def _escalate(self, reason: EscalationReason, context_text: str) -> ToolResult[Any]:
        """Record an escalation for this session via ``flag_for_human`` (Req 9.4)."""
        return self.toolset.flag_for_human(
            reason=reason,
            call_session_id=self.session_id,
            context=context_text,
            patient_ref=self.context.patient_ref,
        )

    def finalize(
        self,
        outcome: CallOutcome | None = None,
        *,
        transcript: str | None = None,
        recording_uri: str | None = None,
    ) -> "StoreResult[CallSession]":
        """Persist the Call_Session outcome and patient identity on end (Req 11.5, 12.7).

        ``transcript`` and ``recording_uri`` are stored alongside the outcome when
        the call was transcribed and recorded.
        """
        self._finalized = True
        return finalize_session(
            self.agent.stores.call_sessions,
            self.context,
            outcome,
            transcript=transcript,
            recording_uri=recording_uri,
        )

    # -- multi-step booking chain (Req 11.2, 2.4, 3.2) ----------------------

    def book(
        self,
        *,
        named_service: str,
        name: str,
        callback_phone: str,
        provider_id: str,
        extra_identifiers: Mapping[str, str] | None = None,
        from_date: ISODate | None = None,
        from_time: str | None = None,
        select_slot: SlotSelector = _earliest_slot,
    ) -> ChainOutcome:
        """Run the booking task as a chained tool sequence (Req 11.2, 2.4, 3.2).

        Chains, in order, so each tool's output informs the next:

        1. ``match_offered_service`` — resolve the patient-named service to an
           offered service and record it on the context (Req 2.1).
        2. ``lookup_patient`` — locate the patient by name + callback phone,
           creating a new record when none matches (Req 3.1, 3.3, 3.4); the
           resolved patient id is retained for the session (Req 3.2).
        3. ``check_availability`` — retrieve open slots for the matched service
           (Req 2.4) and select one, recording the selection on the context.
        4. ``book_appointment`` — write the appointment to the provider's
           calendar (Req 2.5).

        On success returns :class:`ChainCompleted` and records the ``booked``
        outcome on the context. On any mid-chain failure the
        :class:`ToolOrchestrator` returns :class:`OfferToTakeMessage` with the
        facts gathered so far retained (Req 11.3).
        """
        self.context.set_identity(
            name=name,
            callback_phone=callback_phone,
            extra_identifiers=dict(extra_identifiers) if extra_identifiers else None,
        )

        def match_step(ctx: ChainContext) -> ToolResult[str]:
            result = self.toolset.match_service(named_service)
            if is_ok(result):
                ctx.session.set_requested_service(result.value)
            return result

        def lookup_step(ctx: ChainContext) -> ToolResult[Patient]:
            found = self.toolset.lookup_patient(
                name=name, callback_phone=callback_phone, extra_identifiers=extra_identifiers
            )
            if isinstance(found, Err):
                return found
            if found.value:
                # Exactly-one / first match informs the current session (Req 3.2).
                patient = found.value[0]
            else:
                # No match -> create a new record (Req 3.3, 3.4).
                created = self.toolset.create_patient(
                    name=name, callback_phone=callback_phone, extra_identifiers=extra_identifiers
                )
                if isinstance(created, Err):
                    return created
                patient = created.value
            ctx.session.set_identity(patient_id=patient.id)
            return Ok(patient)

        def availability_step(ctx: ChainContext) -> ToolResult[Slot]:
            service = ctx.session.requested_service
            assert service is not None  # set by match_step
            # A time recorded earlier in the call stands in when the caller of
            # `book` did not name one, so an asked-for hour is not lost mid-chain.
            preferred_time = from_time or ctx.session.requested_time
            slots = self.toolset.check_availability(
                service=service,
                provider_ids=[provider_id],
                from_date=from_date,
                from_time=preferred_time,
            )
            if isinstance(slots, Err):
                return slots
            selected = select_slot(slots.value)
            if selected is None:
                # No open slots to offer -> the orchestrator offers the waitlist
                # (Req 2.7); surfaced here as a not-found so the chain stops.
                return _no_slots(service)
            ctx.session.select_slot(selected.id)
            return Ok(selected)

        def book_step(ctx: ChainContext) -> ToolResult[BookingResult]:
            service = ctx.session.requested_service
            patient_id = ctx.session.patient_id
            slot_id = ctx.session.selected_slot_id
            assert service is not None and patient_id is not None and slot_id is not None
            return self.toolset.book_appointment(
                provider_id=provider_id,
                patient_id=patient_id,
                slot_id=slot_id,
                service=service,
            )

        outcome = self.orchestrator.run_chain(
            [
                ChainStep(name="match_offered_service", run=match_step),
                ChainStep(name="lookup_patient", run=lookup_step),
                ChainStep(name="check_availability", run=availability_step),
                ChainStep(name="book_appointment", run=book_step),
            ]
        )
        if outcome.kind == "chain_completed":
            self.context.record_outcome(CallOutcome.BOOKED)
        return outcome


def _no_slots(service: str) -> ToolResult[Slot]:
    """Build a not-found :data:`ToolResult` for an empty availability offer."""
    return Err(NotFound(detail=f"no open slots for service {service!r}"))


# ---------------------------------------------------------------------------
# VoiceFrontDeskAgent — the composition root.
# ---------------------------------------------------------------------------


class VoiceFrontDeskAgent:
    """The end-to-end Voice_Front_Desk agent (task 9.2).

    Composes the ten patient-facing Strands tools (bound to the Data_Layer
    stores), the administrative-only guardrail system prompt, and the
    per-Call_Session orchestration, over a :class:`VoiceStream`. The default
    stream is a :class:`NovaSonicVoiceStream` built with the tools and system
    prompt registered on its underlying ``BidiAgent``; tests inject a fake
    :class:`VoiceStream` instead.
    """

    def __init__(
        self,
        stores: VoiceFrontDeskStores,
        *,
        stream: VoiceStream | None = None,
        stream_factory: Callable[[], VoiceStream] | None = None,
        model: Any | None = None,
        model_id: str | None = None,
        region: str | None = None,
        voice_id: str | None = None,
        system_prompt: str = ADMINISTRATIVE_ONLY_SYSTEM_PROMPT,
        offered_services: Sequence[str] | None = None,
    ) -> None:
        """Create the agent.

        Args:
            stores: The Data_Layer stores the tools bind to.
            stream: A pre-built :class:`VoiceStream` to reuse for every session
                (typically a fake in tests). Mutually informative with
                ``stream_factory``; ``stream`` takes precedence when both are set.
            stream_factory: A factory building a fresh :class:`VoiceStream` per
                session (production typically wants one Nova Sonic stream per
                call). When neither ``stream`` nor ``stream_factory`` is given, a
                :class:`NovaSonicVoiceStream` is built per session from the tools
                and system prompt.
            model: The Nova Sonic model (id or ``BidiModel``) for the default
                stream; ignored when a stream/factory is supplied.
            system_prompt: The guardrail system prompt attached to the agent
                (defaults to :data:`ADMINISTRATIVE_ONLY_SYSTEM_PROMPT`, Req 10.1).
            offered_services: The offered-service names for the guardrail; when
                omitted they are read from the knowledge base at construction.
        """
        self.stores = stores
        self.toolset = BoundToolset(stores)
        self.system_prompt = system_prompt
        # Same toolset instance, so a model-invoked flag_for_human and the
        # guardrail backstop share one escalation-dedupe set.
        self.tools = build_patient_facing_tools(stores, toolset=self.toolset)
        if offered_services is None:
            offered_services = self.toolset.offered_service_names()
        # The consultation offered when a caller describes a symptom or cannot say
        # which service they need. Ignored unless the clinic actually offers it.
        self.guardrail = GuardrailPolicy(
            offered_services, general_consultation=GENERAL_CONSULTATION
        )
        self._stream = stream
        self._stream_factory = stream_factory
        self._model = model
        self._model_id = model_id
        self._region = region
        self._voice_id = voice_id

    # -- introspection ------------------------------------------------------

    @property
    def tool_names(self) -> tuple[str, ...]:
        """The names of the ten registered patient-facing tools (task 9.2)."""
        return tuple(self.tools.keys())

    @property
    def tool_definitions(self) -> list[Any]:
        """The Strands ``@tool`` definitions registered with the ``BidiAgent``."""
        return list(self.tools.values())

    # -- stream construction ------------------------------------------------

    def _make_stream(self) -> VoiceStream:
        """Provide the :class:`VoiceStream` for a new session.

        Reuses an injected ``stream``, else calls ``stream_factory``, else builds
        a :class:`NovaSonicVoiceStream` with the ten tools and the guardrail
        system prompt registered on its ``BidiAgent`` — keeping every Strands /
        Nova Sonic detail behind that adapter.
        """
        if self._stream is not None:
            return self._stream
        if self._stream_factory is not None:
            return self._stream_factory()
        return NovaSonicVoiceStream(
            model=self._model,
            model_id=self._model_id,
            region=self._region,
            voice_id=self._voice_id,
            tools=self.tool_definitions,
            system_prompt=self.system_prompt,
        )

    # -- session construction ----------------------------------------------

    def start_session(
        self, session_id: str | None = None, *, open_record: bool = True
    ) -> VoiceSession:
        """Create a :class:`VoiceSession` for one Call_Session (Req 11.1).

        Args:
            session_id: The Call_Session id; a fresh id is generated when omitted.
            open_record: When ``True`` (default) an open ``CallSession`` record is
                created through the store so :meth:`VoiceSession.finalize` can
                persist its outcome on end (Req 11.5).
        """
        sid = session_id or uuid4().hex
        if open_record:
            self.stores.call_sessions.create(
                CallSession(id=sid, started_at=_now_iso())
            )
        # Bind the id the model-invoked tools record against, so a
        # model-initiated escalation lands on this Call_Session rather than an
        # invented one. A fresh agent is built per call, so this is call-scoped.
        self.toolset.bind_session(sid)
        return VoiceSession(self, sid, self._make_stream())


def create_voice_front_desk_agent(
    stores: VoiceFrontDeskStores,
    *,
    stream: VoiceStream | None = None,
    stream_factory: Callable[[], VoiceStream] | None = None,
    model: Any | None = None,
    model_id: str | None = None,
    region: str | None = None,
    voice_id: str | None = None,
    system_prompt: str = ADMINISTRATIVE_ONLY_SYSTEM_PROMPT,
    offered_services: Sequence[str] | None = None,
) -> VoiceFrontDeskAgent:
    """Factory building a fully wired :class:`VoiceFrontDeskAgent` (task 9.2).

    A thin convenience wrapper over the constructor, matching the design's
    "factory function" phrasing and giving callers a single entry point that
    composes the tools, guardrail prompt, and orchestration over the given
    stores and (optional) voice stream. When no ``stream``/``stream_factory`` is
    given, each session builds a real Nova Sonic :class:`NovaSonicVoiceStream`
    from ``model_id`` / ``region`` / ``voice_id`` (genuine Bedrock
    speech-to-speech).
    """
    return VoiceFrontDeskAgent(
        stores,
        stream=stream,
        stream_factory=stream_factory,
        model=model,
        model_id=model_id,
        region=region,
        voice_id=voice_id,
        system_prompt=system_prompt,
        offered_services=offered_services,
    )
