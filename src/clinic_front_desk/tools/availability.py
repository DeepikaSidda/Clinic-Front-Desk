"""The ``check_availability`` Strands tool (task 6.1, Req 2.2, 2.3, 2.10, 4.4).

Given an *already-matched* offered service (see
:func:`~clinic_front_desk.tools.service_matcher.match_offered_service`), this
tool retrieves open :class:`~clinic_front_desk.models.Slot`\\ s for that service
from the :class:`~clinic_front_desk.data_layer.interfaces.AppointmentStore` and
returns up to a small number of dated offers.

Design contract ("Strands Tool Suite"):

    check_availability(input: {
      providerId?; service; fromDate? = today; limit? = 3
    }) -> { ok: true; slots: Slot[] } | { ok: false; error: ToolError }

Tool-level guardrail (Req 10.3, 10.4): the tool requires an already-matched
offered ``service`` string; there is no symptom→service path here.

Behaviors implemented:

- **Open slots, capped (Req 2.3, 4.4).** Returns at most ``limit`` open slots
  (default :data:`DEFAULT_AVAILABILITY_LIMIT` = 3), each carrying a concrete
  date and time (``Slot.start`` / ``Slot.end`` are ISO-8601 datetimes). The
  offered count is therefore ``min(limit, number of open slots)``. When no open
  slots exist the result is an empty list — the orchestrator turns that into a
  waitlist offer (Req 2.7), which is not this tool's concern.
- **Slots that have already started are never offered.** The store's
  ``list_open_slots`` is *date*-scoped by contract (``from_date`` is an ISO date),
  so at 5pm it still returns this morning's untaken 11:30 slot. Offering that to a
  caller is worse than offering nothing: observed on a live call, where the agent
  confidently offered an 11:30 slot that had already passed and the caller had to
  correct it. Recency is a *presentation* decision, so it is filtered here rather
  than by widening the store interface, and ``now`` is injectable so the rule is
  testable without freezing the clock.
- **A published slot is the doctor's time, not a service-specific offer.** This is
  how the clinic actually works: one ENT doctor, who takes whichever ENT service
  the caller needs in whatever half hour is free. The service is decided at
  booking and recorded on the appointment.

  The data cannot express it any other way. A slot's identity is provider + day +
  start with no service in it (see
  :func:`~clinic_front_desk.scheduling.day_slots.slot_id_for`), so publishing a
  day under a second service overwrites the same slots rather than adding parallel
  ones — at any minute a provider has exactly one slot, labelled with whatever the
  last publish used. Searching by service therefore reported a wide-open day as
  fully booked: observed live, where a year published as "ENT Consultation" made
  every "Hearing Test" request come back with nothing available.

  So availability reads the provider's own calendar and ignores the label. That
  also costs one query instead of one per configured service — measured at 8.1s
  down to well under a second — which matters because Nova Sonic runs tool calls
  concurrently with speech and answers with whatever it has when its turn ends.

  Times are de-duplicated by provider and start, so one minute is never offered
  twice. The offered slots are relabelled with the caller's service before they
  are returned, so the internal label never reaches the caller's ear.
- **Retrieval-failure path (Req 2.10).** If the underlying store read fails for
  any queried provider, the tool surfaces a
  :class:`~clinic_front_desk.models.StoreFailure` ``ToolError`` so the
  orchestrator can tell the patient availability could not be retrieved and
  offer to take a message.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timezone

from clinic_front_desk.data_layer.interfaces import AppointmentStore
from clinic_front_desk.models import (
    Err,
    ISODate,
    Ok,
    Slot,
    StoreFailure,
    ToolResult,
    is_err,
)

#: Default number of slots offered to a patient (Req 2.3).
DEFAULT_AVAILABILITY_LIMIT = 3

#: How many slots to read per offered slot, so de-duplication cannot starve the
#: offer when records share a start time.
#:
#: Four is generous for a case normal publishing cannot create at all. Beyond that
#: many collisions on one minute the caller is simply offered fewer times, which
#: errs the safe way: never an invented option, never the same minute twice.
_OVERFETCH = 4

_STORE = "AppointmentStore"


def _today_iso() -> ISODate:
    """Return today's date (UTC) as an ISO ``YYYY-MM-DD`` string."""
    return datetime.now(timezone.utc).date().isoformat()


def _preferred_instant(from_date: ISODate, from_time: str | None) -> datetime | None:
    """Combine a date and an ``HH:MM`` clock time into an aware UTC instant.

    Returns ``None`` when no time was asked for, or when the value cannot be read
    as a clock time. A malformed preference is *ignored* rather than failing the
    call: the caller still gets real open slots, just not filtered to the hour
    they said. Refusing to book someone because a time arrived as "half three"
    would be the worse outcome.
    """
    if not from_time:
        return None
    text = from_time.strip()
    try:
        return datetime.fromisoformat(f"{from_date}T{text}").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _slot_start_instant(slot: Slot) -> datetime | None:
    """Parse a slot's ``start`` into an aware UTC datetime, or ``None``.

    Slot starts are ISO-8601 and may carry ``Z``, an explicit offset, or nothing.
    A naive value is read as UTC, matching how the rest of the system stores
    timestamps. An unparseable start returns ``None`` and is *kept* rather than
    dropped — a malformed record should surface for correction, not silently
    vanish from availability.
    """
    raw = slot.start
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


#: Weekday names indexed the way the clinic's ``hours`` map is keyed (Sunday = 0).
#: Python's ``date.weekday()`` is Monday = 0, so every conversion between the two
#: goes through :func:`closed_weekday_name` rather than being open-coded.
WEEKDAY_NAMES: tuple[str, ...] = (
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
)


def closed_weekday_name(
    on_date: ISODate, open_weekdays: frozenset[int]
) -> str | None:
    """The weekday name when the clinic has no hours that day, else ``None``.

    Why availability needs this at all: a caller asking for a Sunday gets slots
    back for Monday, because the search is "on or after". The times are correct and
    the reason is missing, so the agent says it could not find anything for Sunday
    and leaves the caller guessing whether the clinic is shut, fully booked, or
    whether the agent simply failed. Observed live on 13 September, a Sunday.

    Deliberately derived from the configured hours rather than a hardcoded weekend,
    so a clinic that opens Sunday and closes Tuesday needs no code change.

    Returns ``None`` for an unparseable date: a malformed date is the caller's
    problem to surface, not a reason to claim the clinic is closed.
    """
    if not open_weekdays:
        # No hours configured at all. Saying "closed on Sunday" would be a guess.
        return None
    try:
        parsed = datetime.strptime(on_date[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    # date.weekday() is Monday=0; the clinic's hours map is Sunday=0.
    index = (parsed.weekday() + 1) % 7
    if index in open_weekdays:
        return None
    return WEEKDAY_NAMES[index]


def check_availability(
    store: AppointmentStore,
    *,
    service: str,
    provider_ids: Sequence[str],
    from_date: ISODate | None = None,
    limit: int = DEFAULT_AVAILABILITY_LIMIT,
    now: datetime | None = None,
    also_published_as: Sequence[str] = (),
    from_time: str | None = None,
) -> ToolResult[list[Slot]]:
    """Retrieve open slots for a matched service (Req 2.2, 2.3, 2.10, 4.4).

    Args:
        store: The appointment/slot store to read open slots from.
        service: An already-matched offered service name (see the service
            matcher). No symptom→service inference happens here (Req 10.3).
        provider_ids: The provider(s) whose calendars to search. For the
            single-ENT demo this is one id; the design allows defaulting to the
            configured provider(s), so callers pass the configured roster here.
        from_date: Search open slots on or after this date; defaults to today
            (UTC).
        limit: Maximum number of slots to offer; defaults to
            :data:`DEFAULT_AVAILABILITY_LIMIT` (3, Req 2.3). Values below zero
            are treated as zero.
        now: The instant to judge recency against; defaults to the current UTC
            time. Slots that already started are not offered. Inject a fixed
            value for deterministic tests.
        also_published_as: Retained for callers that still pass it, and ignored.
            Availability no longer searches by service label at all, so there is
            nothing left for it to widen.
        from_time: The clock time (``HH:MM``) the caller asked for on
            ``from_date``. Earlier slots on that date are not offered; later ones,
            and any on following dates, still are. An unreadable value is ignored.

    Returns:
        ``Ok(slots)`` with at most ``limit`` open, not-yet-started slots for
        ``service`` (plus ``also_published_as``) across the given providers,
        ordered by start time then id, each with a concrete date and time. One
        provider-minute appears at most once, preferring the slot whose label
        matches ``service``. Returns ``Ok([])`` when none are available. Returns
        ``Err(StoreFailure(...))`` if any provider's slot read fails (Req 2.10).
    """
    effective_from = from_date if from_date is not None else _today_iso()
    capped_limit = max(0, limit)
    reference = now if now is not None else datetime.now(timezone.utc)

    # The caller's asked-for time raises the floor above "not yet started". With a
    # clinic publishing midnight to midnight, the first three slots of a day are
    # 00:00, 00:30 and 01:00, so without this every caller — whatever hour they
    # asked for — is offered the middle of the night.
    preferred = _preferred_instant(effective_from, from_time)
    floor = max(reference, preferred) if preferred is not None else reference

    # Push the floor into the store as a lower bound on slot start, so a clinic
    # with a year published does not read thousands of slots to offer three. The
    # store compares it as text and a slot start begins with its date, so the
    # minute-precision form narrows within a day too.
    bound = floor.strftime("%Y-%m-%dT%H:%M")
    if bound[:10] < effective_from:
        # `now` can sit before the caller's requested date. Never widen past it.
        bound = effective_from

    # (provider, start) -> slot, so one minute is never offered twice.
    #
    # Read more than the offer needs, because de-duplication happens after the
    # read: if two records share a start time, asking the store for exactly
    # `limit` would spend the budget on the same minute twice and offer the caller
    # fewer times than the calendar has. Publishing cannot produce that — a slot's
    # id is provider + day + start, so a republish overwrites rather than
    # duplicates — but a store holding such a pair must not silently cost the
    # caller options.
    fetch = capped_limit * _OVERFETCH if capped_limit else 0

    by_minute: dict[tuple[str, str], Slot] = {}
    for provider_id in provider_ids:
        result = store.list_open_slots_for_provider(provider_id, bound, limit=fetch)
        if is_err(result):
            # Retrieval failed for this provider: surface a store failure so the
            # orchestrator can offer to take a message (Req 2.10). No partial
            # availability is returned.
            return Err(StoreFailure(store=_STORE, detail=result.error.detail))
        for slot in result.value:
            by_minute.setdefault((slot.provider_id, slot.start), slot)

    # Re-check the floor as an instant. The store's bound is a text comparison,
    # which cannot reason about a start carrying `Z` or an explicit offset, so the
    # real judgement of "has this already begun" happens here. A start that will
    # not parse is kept rather than dropped: a malformed record should surface for
    # correction, not silently vanish from availability.
    upcoming = []
    for slot in by_minute.values():
        instant = _slot_start_instant(slot)
        if instant is None or instant >= floor:
            upcoming.append(slot)

    # Deterministic ordering across providers: earliest start first, id as a
    # stable tiebreak. Every slot carries a concrete date/time via `start`.
    upcoming.sort(key=lambda s: (s.start, s.id))

    # Offer the time under the service the caller asked for. The label a slot was
    # published with is an internal artefact — one publish sets it for a whole day,
    # and the appointment records the caller's service regardless.
    #
    # Left as-is, that artefact reaches the caller's ear. Observed on a live call:
    # a patient asked for a Hearing Test and was told "only ENT Consultation slots
    # are available at that time, not Hearing Test slots", about slots that were
    # hers to book. She then had to talk the agent into it, and the agent invented
    # a clinical justification to agree with her. Relabelling the *returned copies*
    # removes the contradiction; nothing stored changes, and booking still goes by
    # slot id.
    offered = [
        slot if slot.service == service else replace(slot, service=service)
        for slot in upcoming[:capped_limit]
    ]
    return Ok(offered)


__all__ = [
    "DEFAULT_AVAILABILITY_LIMIT",
    "WEEKDAY_NAMES",
    "check_availability",
    "closed_weekday_name",
]
