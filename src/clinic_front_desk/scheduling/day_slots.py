"""Generating a day's bookable slots for a provider.

The doctor publishes a day and the calendar fills with fixed-length slots. That is
the only way a bookable slot comes into existence: the agent may *book* a slot and
*release* one, but it can never create availability. If it could, a caller pressing
for an earlier time would eventually be offered a slot the doctor never opened.

Pure by design — no store, no clock, no ids from the environment — so a published
day is reproducible and testable. Persisting is the caller's job.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from clinic_front_desk.models import ISODate, Slot, SlotStatus

#: Minutes per slot. A 30-minute consultation is the clinic's working unit.
DEFAULT_SLOT_MINUTES = 30

#: The default publishing window: the whole day, midnight to midnight.
#:
#: A 24-hour window is unusual for a consultant and deliberately permitted, because
#: the hospital this was built for lists round-the-clock cover. It does mean a
#: caller can be offered 03:00, so :func:`generate_day_slots` takes explicit
#: ``start``/``end`` bounds and the portal exposes them — publishing 09:00–17:00 is
#: one parameter away.
DAY_START = "00:00"
DAY_END = "24:00"

#: Guard against a pathological request (a one-minute slot length over a year)
#: turning into a runaway write.
MAX_SLOTS_PER_DAY = 200


class SlotGenerationError(ValueError):
    """The requested day/window/length cannot produce a sane set of slots."""


def _parse_minutes(clock: str, *, field: str) -> int:
    """Minutes since midnight for an ``HH:MM`` clock time, allowing ``24:00``."""
    text = clock.strip()
    parts = text.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise SlotGenerationError(f"{field} must be HH:MM, got {clock!r}")
    hours, minutes = int(parts[0]), int(parts[1])
    # 24:00 is the exclusive end of a full day; it is not a valid start.
    if not (0 <= hours <= 24) or not (0 <= minutes < 60) or (hours == 24 and minutes):
        raise SlotGenerationError(f"{field} is not a valid time: {clock!r}")
    return hours * 60 + minutes


def _iso(day: ISODate, minutes_from_midnight: int) -> str:
    """``YYYY-MM-DDTHH:MM`` for an offset into ``day``, rolling into the next day.

    The last slot of a full day ends at midnight, which belongs to *tomorrow*.
    Formatting it as ``24:00`` would sort and parse wrongly everywhere else, so it
    is carried into the next date properly.
    """
    try:
        base = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise SlotGenerationError(f"day must be YYYY-MM-DD, got {day!r}") from exc
    moment = base + timedelta(minutes=minutes_from_midnight)
    return moment.strftime("%Y-%m-%dT%H:%M")


#: Upper bound on a single range publish. A little over a year, so "the rest of
#: this year" and "the next twelve months" both fit, while a typo in the end date
#: cannot try to open the next decade.
MAX_PUBLISH_DAYS = 400


def slot_id_for(day: ISODate, provider_id: str, start_clock: str) -> str:
    """A stable slot id.

    Derived from day, provider and start time rather than random, so publishing the
    same day twice cannot create a second set of duplicate slots side by side — the
    ids collide and the store replaces rather than accumulates. That property is
    what makes the portal's publish button safe to press twice.
    """
    return f"slot-{provider_id}-{day}-{start_clock.replace(':', '')}"


def generate_day_slots(
    day: ISODate,
    provider_id: str,
    service: str,
    *,
    minutes: int = DEFAULT_SLOT_MINUTES,
    start: str = DAY_START,
    end: str = DAY_END,
) -> list[Slot]:
    """Build one day of back-to-back open slots for a provider.

    Args:
        day: The ISO date to publish (``YYYY-MM-DD``).
        provider_id: The provider whose calendar this is. Required — a slot with
            no provider cannot be booked (Req 16.7).
        service: The service the slots are bookable for. Matched against the
            clinic's offered services by exact name when a caller asks, so it
            should be a configured service name.
        minutes: Slot length. Defaults to :data:`DEFAULT_SLOT_MINUTES`.
        start: First slot's start, ``HH:MM``.
        end: Exclusive end of the window, ``HH:MM`` or ``24:00``.

    Returns:
        Slots ordered earliest-first, every one ``OPEN``. A window that does not
        divide evenly stops before overrunning ``end`` rather than emitting a
        short final slot, so no slot is ever shorter than advertised.

    Raises:
        SlotGenerationError: On a malformed day/time, a non-positive length, an
            empty or inverted window, a blank provider or service, or a request
            that would exceed :data:`MAX_SLOTS_PER_DAY`.
    """
    if not provider_id or not provider_id.strip():
        raise SlotGenerationError("provider_id is required to publish slots")
    if not service or not service.strip():
        raise SlotGenerationError("service is required to publish slots")
    if minutes <= 0:
        raise SlotGenerationError(f"slot length must be positive, got {minutes}")

    first = _parse_minutes(start, field="start")
    last = _parse_minutes(end, field="end")
    if last <= first:
        raise SlotGenerationError(
            f"the window {start}-{end} is empty; end must be after start"
        )

    count = (last - first) // minutes
    if count == 0:
        raise SlotGenerationError(
            f"the window {start}-{end} is shorter than one {minutes}-minute slot"
        )
    if count > MAX_SLOTS_PER_DAY:
        raise SlotGenerationError(
            f"{count} slots requested; the daily maximum is {MAX_SLOTS_PER_DAY}"
        )

    slots: list[Slot] = []
    for index in range(count):
        begins = first + index * minutes
        start_iso = _iso(day, begins)
        slots.append(
            Slot(
                id=slot_id_for(day, provider_id.strip(), start_iso.partition("T")[2]),
                provider_id=provider_id.strip(),
                service=service.strip(),
                start=start_iso,
                end=_iso(day, begins + minutes),
                status=SlotStatus.OPEN,
            )
        )
    return slots


def generate_range_slots(
    first_day: ISODate,
    last_day: ISODate,
    provider_id: str,
    service: str,
    *,
    minutes: int = DEFAULT_SLOT_MINUTES,
    start: str = DAY_START,
    end: str = DAY_END,
    open_weekdays: frozenset[int] | None = None,
) -> list[Slot]:
    """Build slots for every day from ``first_day`` to ``last_day`` inclusive.

    Publishing a day at a time is fine for tomorrow and absurd for a year, which
    is the only reason this exists.

    Args:
        first_day / last_day: Inclusive ISO date bounds.
        provider_id / service / minutes / start / end: As
            :func:`generate_day_slots`.
        open_weekdays: Weekday indices (0 = Sunday) to publish. Days outside the
            set are skipped entirely, which is how a clinic closed on Sundays
            avoids offering Sunday appointments. ``None`` publishes every day.

    Returns:
        Every slot across the range, ordered earliest-first.

    Raises:
        SlotGenerationError: On malformed dates, an inverted range, a range longer
            than :data:`MAX_PUBLISH_DAYS`, or anything
            :func:`generate_day_slots` rejects.
    """
    try:
        begins = datetime.strptime(first_day, "%Y-%m-%d").replace(tzinfo=UTC)
        ends = datetime.strptime(last_day, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise SlotGenerationError(
            f"dates must be YYYY-MM-DD, got {first_day!r} and {last_day!r}"
        ) from exc
    if ends < begins:
        raise SlotGenerationError(
            f"{last_day} is before {first_day}; the range is empty"
        )

    span = (ends - begins).days + 1
    if span > MAX_PUBLISH_DAYS:
        raise SlotGenerationError(
            f"{span} days requested; the maximum in one publish is {MAX_PUBLISH_DAYS}"
        )

    slots: list[Slot] = []
    for offset in range(span):
        moment = begins + timedelta(days=offset)
        if open_weekdays is not None:
            # Python's weekday() is Monday=0; the clinic's convention is Sunday=0.
            weekday = (moment.weekday() + 1) % 7
            if weekday not in open_weekdays:
                continue
        slots.extend(
            generate_day_slots(
                moment.strftime("%Y-%m-%d"),
                provider_id,
                service,
                minutes=minutes,
                start=start,
                end=end,
            )
        )
    if not slots:
        raise SlotGenerationError(
            "no days in that range are open, so there is nothing to publish"
        )
    return slots


__all__ = [
    "DEFAULT_SLOT_MINUTES",
    "DAY_START",
    "DAY_END",
    "MAX_SLOTS_PER_DAY",
    "MAX_PUBLISH_DAYS",
    "SlotGenerationError",
    "generate_day_slots",
    "generate_range_slots",
    "slot_id_for",
]
