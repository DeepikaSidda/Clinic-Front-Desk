"""``SessionContext`` — per-Call_Session fact retention and task step index (task 7.1).

The ``SessionContext`` is the in-memory store of a single active
:class:`~clinic_front_desk.models.entities.CallSession`'s collected facts and
its current task step. It is created when a call starts and lives for the
duration of that call only.

Responsibilities (design "Voice_Front_Desk / SessionContext"):

- Retain the patient's provided identifying details (name, callback phone, any
  extra disambiguating identifiers, and the resolved patient id once known),
  the requested service, the requested date and time, and the selected slot for
  the whole Call_Session (Req 11.1).
- Let the orchestrator recall any earlier-provided fact without re-prompting the
  patient (Req 11.4) — every retained fact has a getter and there is a generic
  :meth:`SessionContext.recall` / :meth:`SessionContext.remembers` pair.
- Expose a mutable *current task step index* so barge-in handling (task 7.9) can
  resume a multi-step task from its pre-interruption step (Req 12.3). Because the
  context is not reset on an interruption, the step index naturally survives a
  barge-in.

Persisting the final :class:`~clinic_front_desk.models.enums.CallOutcome` to the
Data_Layer on session end is the orchestrator's job (task 7.11); this class only
*holds* the outcome via :meth:`record_outcome` so the orchestrator has a single
source of truth to persist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from clinic_front_desk.models import CallOutcome, PatientRef

# The names of the facts retained for the duration of a Call_Session (Req 11.1).
# Used by :meth:`SessionContext.recall` / :meth:`SessionContext.remembers` and
# :meth:`SessionContext.known_facts` so retrieval-without-re-prompt (Req 11.4)
# is driven by one authoritative list.
RETAINED_FACTS: tuple[str, ...] = (
    "patient_id",
    "name",
    "callback_phone",
    "extra_identifiers",
    "requested_service",
    "requested_date",
    "requested_time",
    "selected_slot_id",
)


@dataclass
class SessionContext:
    """In-memory context for one active Call_Session (Req 11.1, 11.4, 12.3).

    All fact fields default to ``None`` (nothing provided yet). The orchestrator
    fills them in as the patient supplies information and reads them back later
    without asking the patient to repeat themselves.
    """

    session_id: str

    # --- Identifying details (Req 11.1) ------------------------------------
    patient_id: str | None = None
    name: str | None = None
    callback_phone: str | None = None
    extra_identifiers: dict[str, str] | None = None

    # --- Request facts (Req 11.1) ------------------------------------------
    requested_service: str | None = None
    requested_date: str | None = None
    requested_time: str | None = None
    selected_slot_id: str | None = None

    # --- Multi-step task position (barge-in resume, Req 12.3 / task 7.9) ----
    task_step_index: int = 0

    # --- Terminal outcome (held here; persisted by the orchestrator, 7.11) --
    outcome: CallOutcome | None = None

    # ---------------------------------------------------------------- identity
    def set_identity(
        self,
        *,
        name: str | None = None,
        callback_phone: str | None = None,
        extra_identifiers: dict[str, str] | None = None,
        patient_id: str | None = None,
    ) -> None:
        """Record identifying details provided by the patient (Req 11.1).

        Only non-``None`` arguments overwrite existing values, so identity can
        be built up incrementally across turns (e.g. name first, phone later,
        an extra identifier for disambiguation, then the resolved patient id)
        without clobbering what was already captured.
        """
        if name is not None:
            self.name = name
        if callback_phone is not None:
            self.callback_phone = callback_phone
        if extra_identifiers is not None:
            # Merge rather than replace so successive disambiguating answers
            # accumulate (Req 3.6 flows feed this).
            merged = dict(self.extra_identifiers or {})
            merged.update(extra_identifiers)
            self.extra_identifiers = merged
        if patient_id is not None:
            self.patient_id = patient_id

    @property
    def patient_ref(self) -> PatientRef:
        """The identifying details as a :class:`PatientRef` (for tools/outcome)."""
        return PatientRef(
            patient_id=self.patient_id,
            name=self.name,
            callback_phone=self.callback_phone,
        )

    # ---------------------------------------------------------------- request
    def set_requested_service(self, service: str) -> None:
        """Record the matched offered service the patient asked for (Req 11.1)."""
        self.requested_service = service

    def set_requested_datetime(
        self, *, date: str | None = None, time: str | None = None
    ) -> None:
        """Record the requested date and/or time (Req 11.1)."""
        if date is not None:
            self.requested_date = date
        if time is not None:
            self.requested_time = time

    def select_slot(self, slot_id: str) -> None:
        """Record the slot the patient selected for booking/rescheduling (Req 11.1)."""
        self.selected_slot_id = slot_id

    # ---------------------------------------------------------------- recall
    def remembers(self, fact: str) -> bool:
        """Return ``True`` if ``fact`` was provided earlier in the call (Req 11.4).

        Enables the orchestrator to reuse earlier-provided information instead of
        re-prompting. Raises :class:`KeyError` for a name that is not a retained
        fact so typos fail loudly rather than silently reporting "not known".
        """
        if fact not in RETAINED_FACTS:
            raise KeyError(f"{fact!r} is not a retained Call_Session fact")
        return getattr(self, fact) is not None

    def recall(self, fact: str) -> Any:
        """Return an earlier-provided fact without re-prompting (Req 11.4).

        Returns ``None`` when the fact has not been provided yet. Raises
        :class:`KeyError` for an unknown fact name.
        """
        if fact not in RETAINED_FACTS:
            raise KeyError(f"{fact!r} is not a retained Call_Session fact")
        return getattr(self, fact)

    def known_facts(self) -> dict[str, Any]:
        """A mapping of every fact provided so far (Req 11.4).

        Facts not yet provided are omitted, so the result reflects exactly what
        the patient has told us and can be reused without re-asking.
        """
        return {
            fact: getattr(self, fact)
            for fact in RETAINED_FACTS
            if getattr(self, fact) is not None
        }

    # ------------------------------------------------------------- task step
    @property
    def current_step_index(self) -> int:
        """The current task step index, for barge-in resume (Req 12.3)."""
        return self.task_step_index

    def set_step(self, index: int) -> None:
        """Set the current task step index (task 7.9 resume support)."""
        if index < 0:
            raise ValueError("task step index must be non-negative")
        self.task_step_index = index

    def advance_step(self) -> int:
        """Advance to and return the next task step index."""
        self.task_step_index += 1
        return self.task_step_index

    # --------------------------------------------------------------- outcome
    def record_outcome(self, outcome: CallOutcome) -> None:
        """Hold the terminal Call_Session outcome (persisted by task 7.11, Req 11.5)."""
        self.outcome = outcome


__all__ = ["SessionContext", "RETAINED_FACTS"]
