"""The ``lookup_patient`` tool and patient creation (task 6.6, Req 3).

This module implements the patient-facing lookup/creation half of Requirement 3
as pure functions over the :class:`~clinic_front_desk.data_layer.interfaces.PatientStore`
interface. Every function returns a discriminated
:data:`~clinic_front_desk.models.ToolResult` (``Ok`` | ``Err``) so the
Voice_Front_Desk can branch on the failure ``kind`` per the error-handling
requirements.

Responsibilities, mapped to the acceptance criteria:

- :func:`lookup_patient` — retrieve every Patient record matching a name and
  callback phone through the store (Req 3.1); when the caller supplies extra
  identifying information, narrow the candidates so a multi-match set can
  converge toward exactly one (Req 3.6). A retrieval failure is surfaced as a
  ``store_failure`` :class:`~clinic_front_desk.models.ToolError` so the agent can
  tell the patient its records could not be accessed (Req 3.7). A no-match
  lookup returns ``Ok([])`` — the signal that a new record must be collected and
  created (Req 3.3).
- :func:`create_patient` — persist a new Patient record through the store once
  the required name and callback phone have been collected (Req 3.4); a
  persistence failure is surfaced as a ``store_failure`` error so the agent can
  tell the patient the record could not be saved (Req 3.8).

Design signature (design "Strands Tool Suite"):

    lookup_patient(input: { name, callbackPhone, extraIdentifiers? })
      -> { ok: true; matches: Patient[] } | { ok: false; error: ToolError }

The store's ``id`` and ``created_at`` fields are server-managed, so
:func:`create_patient` assigns them through injectable ``id_factory`` / ``clock``
callables (defaulting to a random UUID and the current UTC time) — mirroring the
clock injection used by :mod:`clinic_front_desk.config.save` so tests stay
deterministic.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import uuid4

from clinic_front_desk.data_layer.interfaces import PatientStore
from clinic_front_desk.models import (
    Err,
    Ok,
    Patient,
    StoreFailure,
    ToolResult,
    is_err,
    patient_code,
)

#: A clock returning the current time as an ISO-8601 UTC string. Injectable so
#: tests can pin ``created_at`` deterministically.
Clock = Callable[[], str]

#: A factory returning a fresh, unique patient id. Injectable for deterministic
#: tests.
IdFactory = Callable[[], str]

#: Label placed on emitted ``StoreFailure`` errors so the caller can identify
#: the failing store (Req 3.7, 3.8).
_STORE_LABEL = "PatientStore"


def _default_clock() -> str:
    return datetime.now(UTC).isoformat()


def _default_id_factory() -> str:
    return uuid4().hex


def _matches_extra(patient: Patient, extra_identifiers: Mapping[str, str]) -> bool:
    """Return ``True`` if ``patient`` satisfies every supplied extra identifier.

    A patient matches when, for each key/value the caller provided, the
    patient's own ``extra_identifiers`` carries that key with an equal value.
    Extra identifiers a patient holds but the caller did not ask about are
    ignored, so supplying more identifiers can only narrow the candidate set —
    the convergence behaviour Req 3.6 relies on.
    """
    existing = patient.extra_identifiers or {}
    return all(existing.get(key) == value for key, value in extra_identifiers.items())


def lookup_patient(
    store: PatientStore,
    name: str,
    callback_phone: str,
    extra_identifiers: Mapping[str, str] | None = None,
) -> ToolResult[list[Patient]]:
    """Retrieve Patient records matching ``name`` and ``callback_phone`` (Req 3.1).

    Args:
        store: The patient store to read through.
        name: The patient-provided name to match.
        callback_phone: The patient-provided callback phone number to match.
        extra_identifiers: Optional additional identifying information used to
            disambiguate when more than one record matches the name and phone
            (Req 3.6). Only candidates carrying every supplied identifier with an
            equal value are kept.

    Returns:
        ``Ok(matches)`` with the (possibly empty) list of matching patients. An
        empty list means no record matched and a new one must be collected and
        created (Req 3.3). On a store retrieval failure, ``Err`` carrying a
        ``store_failure`` :class:`~clinic_front_desk.models.ToolError` so the
        agent can tell the patient its records could not be accessed (Req 3.7).
    """
    result = store.find_by_name_and_phone(name, callback_phone)
    if is_err(result):
        return Err(StoreFailure(store=_STORE_LABEL, detail=result.error.detail))

    matches = result.value
    if extra_identifiers:
        matches = [p for p in matches if _matches_extra(p, extra_identifiers)]
    return Ok(matches)


#: Blood groups accepted on intake, normalised to these exact strings.
#:
#: A closed set on purpose. Speech-to-text renders "A positive" a dozen ways ("a
#: positive", "A+", "eight positive"), and a free-text blood group in a patient
#: record is worse than none: it looks authoritative and cannot be relied on.
#: Anything not recognised is left unset and the doctor can ask.
BLOOD_GROUPS: tuple[str, ...] = ("A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-")

#: Plausible human bounds for intake measurements. Outside these a value is far
#: more likely to be a transcription slip (a weight heard as 720 kg) than a real
#: measurement, and a wrong number in a patient record is worse than a missing one.
AGE_RANGE = (0, 120)
WEIGHT_KG_RANGE = (0.5, 400.0)
HEIGHT_CM_RANGE = (20.0, 260.0)


def normalize_blood_group(raw: str | None) -> str | None:
    """Normalise a spoken blood group to one of :data:`BLOOD_GROUPS`, or ``None``.

    Accepts the ways a transcript actually renders them — ``"a positive"``,
    ``"A +"``, ``"o neg"``, ``"AB negative"`` — and refuses anything else rather
    than guessing.
    """
    if not raw:
        return None
    text = raw.strip().upper().replace(" ", "")
    text = text.replace("POSITIVE", "+").replace("POS", "+")
    text = text.replace("NEGATIVE", "-").replace("NEG", "-")
    return text if text in BLOOD_GROUPS else None


def _in_range(value: float | None, bounds: tuple[float, float]) -> bool:
    return value is not None and bounds[0] <= value <= bounds[1]


def create_patient(
    store: PatientStore,
    name: str,
    callback_phone: str,
    extra_identifiers: Mapping[str, str] | None = None,
    *,
    age: int | None = None,
    blood_group: str | None = None,
    weight_kg: float | None = None,
    height_cm: float | None = None,
    clock: Clock = _default_clock,
    id_factory: IdFactory = _default_id_factory,
) -> ToolResult[Patient]:
    """Create a new Patient record through the store (Req 3.4).

    Called once the required name and callback phone have been collected because
    no existing record matched (Req 3.3).

    Args:
        store: The patient store to persist through.
        name: The new patient's name.
        callback_phone: The new patient's callback phone number.
        extra_identifiers: Optional additional identifying information to retain
            on the record for future disambiguation (Req 3.6).
        clock: Injectable clock for the ``created_at`` stamp (defaults to now).
        id_factory: Injectable id generator (defaults to a random UUID).

    Returns:
        ``Ok(patient)`` with the persisted record on success. On a persistence
        failure, ``Err`` carrying a ``store_failure``
        :class:`~clinic_front_desk.models.ToolError` and — because the store
        contract is non-destructive (Req 16.6) — no partial record is retained,
        so the agent can tell the patient the record could not be saved
        (Req 3.8).
    """
    patient = Patient(
        id=id_factory(),
        name=name,
        callback_phone=callback_phone,
        extra_identifiers=dict(extra_identifiers) if extra_identifiers else None,
        created_at=clock(),
        # Assigned once, at creation, and stored. Deriving it on read would change
        # the patient's code whenever a misheard name was corrected, so the code
        # they were read out on the call would stop working.
        code=patient_code(name, callback_phone),
        # Implausible or unrecognised intake values are dropped, not stored. A
        # wrong number in a patient record reads as fact; a missing one prompts
        # the doctor to ask.
        age=age if _in_range(age, AGE_RANGE) else None,
        blood_group=normalize_blood_group(blood_group),
        weight_kg=weight_kg if _in_range(weight_kg, WEIGHT_KG_RANGE) else None,
        height_cm=height_cm if _in_range(height_cm, HEIGHT_CM_RANGE) else None,
    )
    result = store.create(patient)
    if is_err(result):
        return Err(StoreFailure(store=_STORE_LABEL, detail=result.error.detail))
    return Ok(result.value)


# ---------------------------------------------------------------------------
# Recording intake details onto a record that already exists
# ---------------------------------------------------------------------------
#
# Observed on a live call. The caller booked, then offered her blood group,
# height and weight. Registration found her existing record, returned it
# unchanged, and reported ``ok``. The agent read that as success and told her:
# "I've added your blood group (B positive), height (154 centimeters), and weight
# (58 kilograms) to your clinic records." Nothing had been written — the record
# still held ``None`` for all three.
#
# Telling a patient her medical details are on file when they are not is worse
# than never asking for them. She has no reason to repeat herself, and the doctor
# reads an empty record believing it was offered and declined.
#
# So the rules here are:
#   * a blank field may be filled — that is the case this was built for;
#   * a field that already holds a value is never silently overwritten, because
#     the person on the phone may not be the person whose record it is;
#   * the result states exactly which fields were written and which were not, so
#     the agent can only confirm what actually happened.


@dataclass(frozen=True)
class IntakeOutcome:
    """What an intake attempt actually did to the stored record.

    Attributes:
        patient: The record as it now stands.
        recorded: Field names written by this call.
        already_on_file: Fields left alone because the record already had a value.
        rejected: Fields dropped as implausible or unrecognised.
    """

    patient: Patient
    recorded: tuple[str, ...] = ()
    already_on_file: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()


def record_intake(
    store: PatientStore,
    patient: Patient,
    *,
    age: int | None = None,
    blood_group: str | None = None,
    weight_kg: float | None = None,
    height_cm: float | None = None,
) -> ToolResult[IntakeOutcome]:
    """Fill blank intake fields on an existing patient record.

    Args:
        store: The patient store to persist through.
        patient: The already-resolved record to amend.
        age / blood_group / weight_kg / height_cm: Values offered by the caller.
            Each is validated exactly as on creation — an implausible measurement
            or an unrecognised blood group is dropped rather than stored, and
            reported in ``rejected`` so it is never confirmed to the caller.

    Returns:
        ``Ok(IntakeOutcome)`` describing precisely what changed; the store is not
        written at all when there is nothing to fill. ``Err(StoreFailure)`` if the
        write fails, in which case nothing was recorded.
    """
    offered: dict[str, object | None] = {
        "age": age if age is not None and _in_range(age, AGE_RANGE) else None,
        "blood_group": normalize_blood_group(blood_group),
        "weight_kg": (
            weight_kg
            if weight_kg is not None and _in_range(weight_kg, WEIGHT_KG_RANGE)
            else None
        ),
        "height_cm": (
            height_cm
            if height_cm is not None and _in_range(height_cm, HEIGHT_CM_RANGE)
            else None
        ),
    }
    raw = {
        "age": age,
        "blood_group": blood_group,
        "weight_kg": weight_kg,
        "height_cm": height_cm,
    }

    recorded: list[str] = []
    already: list[str] = []
    rejected: list[str] = []
    updated = replace(patient)

    for field, value in offered.items():
        if raw[field] is None:
            # Not offered on this call; silence is not a rejection.
            continue
        if value is None:
            rejected.append(field)
            continue
        if getattr(patient, field, None) is not None:
            already.append(field)
            continue
        setattr(updated, field, value)
        recorded.append(field)

    if not recorded:
        return Ok(
            IntakeOutcome(
                patient=patient,
                already_on_file=tuple(already),
                rejected=tuple(rejected),
            )
        )

    result = store.update(updated)
    if is_err(result):
        # Nothing recorded: the caller must not be told otherwise (Req 3.8).
        return Err(StoreFailure(store=_STORE_LABEL, detail=result.error.detail))
    return Ok(
        IntakeOutcome(
            patient=result.value,
            recorded=tuple(recorded),
            already_on_file=tuple(already),
            rejected=tuple(rejected),
        )
    )


__all__ = [
    "Clock",
    "IdFactory",
    "BLOOD_GROUPS",
    "AGE_RANGE",
    "WEIGHT_KG_RANGE",
    "HEIGHT_CM_RANGE",
    "normalize_blood_group",
    "lookup_patient",
    "create_patient",
    "IntakeOutcome",
    "record_intake",
]
