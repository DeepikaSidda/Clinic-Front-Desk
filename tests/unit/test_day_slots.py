"""Unit tests for day-slot generation (``scheduling/day_slots.py``)."""

from __future__ import annotations

import pytest

from clinic_front_desk.models import SlotStatus
from clinic_front_desk.scheduling import (
    DEFAULT_SLOT_MINUTES,
    MAX_SLOTS_PER_DAY,
    SlotGenerationError,
    generate_day_slots,
    slot_id_for,
)

DAY = "2026-09-10"
PROVIDER = "prov-raana"
SERVICE = "ENT Consultation"


def _slots(**kwargs: object) -> list:
    return generate_day_slots(DAY, PROVIDER, SERVICE, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Shape of a published day
# ---------------------------------------------------------------------------


def test_a_full_day_of_thirty_minute_slots_is_forty_eight() -> None:
    assert DEFAULT_SLOT_MINUTES == 30
    assert len(_slots()) == 48


def test_the_day_starts_at_midnight_and_ends_at_midnight() -> None:
    slots = _slots()

    assert slots[0].start == f"{DAY}T00:00"
    assert slots[0].end == f"{DAY}T00:30"
    assert slots[-1].start == f"{DAY}T23:30"
    # Midnight belongs to the next date; formatting it as 24:00 would sort wrongly.
    assert slots[-1].end == "2026-09-11T00:00"


def test_slots_are_back_to_back_with_no_gap_or_overlap() -> None:
    slots = _slots()

    for earlier, later in zip(slots, slots[1:], strict=False):
        assert earlier.end == later.start


def test_slots_are_ordered_earliest_first() -> None:
    slots = _slots()

    assert [s.start for s in slots] == sorted(s.start for s in slots)


def test_every_generated_slot_is_open() -> None:
    assert all(slot.status is SlotStatus.OPEN for slot in _slots())


def test_every_slot_carries_the_provider_and_service() -> None:
    for slot in _slots():
        assert slot.provider_id == PROVIDER
        assert slot.service == SERVICE


def test_slot_ids_are_unique_within_a_day() -> None:
    slots = _slots()

    assert len({slot.id for slot in slots}) == len(slots)


def test_ids_are_stable_so_republishing_replaces_rather_than_duplicates() -> None:
    # This is what makes the portal's publish button safe to press twice.
    first = _slots()
    second = _slots()

    assert [s.id for s in first] == [s.id for s in second]
    assert first[0].id == slot_id_for(DAY, PROVIDER, "00:00")


def test_different_days_and_providers_do_not_collide() -> None:
    mine = generate_day_slots(DAY, "prov-a", SERVICE)
    theirs = generate_day_slots(DAY, "prov-b", SERVICE)
    tomorrow = generate_day_slots("2026-09-11", "prov-a", SERVICE)

    assert not {s.id for s in mine} & {s.id for s in theirs}
    assert not {s.id for s in mine} & {s.id for s in tomorrow}


# ---------------------------------------------------------------------------
# Windows and lengths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(15, 96), (30, 48), (60, 24), (20, 72), (45, 32)],
)
def test_slot_length_determines_the_count(minutes: int, expected: int) -> None:
    assert len(_slots(minutes=minutes)) == expected


def test_a_working_day_window_can_be_published_instead_of_the_whole_day() -> None:
    slots = _slots(start="09:00", end="17:00")

    assert len(slots) == 16
    assert slots[0].start == f"{DAY}T09:00"
    assert slots[-1].end == f"{DAY}T17:00"


def test_a_window_that_does_not_divide_evenly_stops_short_of_the_end() -> None:
    # No slot is ever shorter than advertised, so a caller offered "30 minutes"
    # always gets 30 minutes.
    slots = _slots(start="09:00", end="10:20", minutes=30)

    assert len(slots) == 2
    assert slots[-1].end == f"{DAY}T10:00"


def test_a_single_slot_window_works() -> None:
    slots = _slots(start="09:00", end="09:30")

    assert len(slots) == 1


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("day", ["", "10-09-2026", "2026-13-01", "not-a-day", "2026-09"])
def test_a_malformed_day_is_rejected(day: str) -> None:
    with pytest.raises(SlotGenerationError):
        generate_day_slots(day, PROVIDER, SERVICE)


@pytest.mark.parametrize("clock", ["", "9am", "24:30", "25:00", "09:60", "0900", "9:0:0"])
def test_a_malformed_time_is_rejected(clock: str) -> None:
    with pytest.raises(SlotGenerationError):
        _slots(start=clock)


@pytest.mark.parametrize("minutes", [0, -1, -30])
def test_a_non_positive_slot_length_is_rejected(minutes: int) -> None:
    with pytest.raises(SlotGenerationError):
        _slots(minutes=minutes)


@pytest.mark.parametrize(("start", "end"), [("17:00", "09:00"), ("09:00", "09:00")])
def test_an_empty_or_inverted_window_is_rejected(start: str, end: str) -> None:
    with pytest.raises(SlotGenerationError):
        _slots(start=start, end=end)


def test_a_window_shorter_than_one_slot_is_rejected() -> None:
    with pytest.raises(SlotGenerationError) as excinfo:
        _slots(start="09:00", end="09:20", minutes=30)

    assert "shorter than one" in str(excinfo.value)


@pytest.mark.parametrize("provider", ["", "   "])
def test_a_blank_provider_is_rejected(provider: str) -> None:
    # A slot with no provider cannot be booked (Req 16.7), so it must never exist.
    with pytest.raises(SlotGenerationError):
        generate_day_slots(DAY, provider, SERVICE)


@pytest.mark.parametrize("service", ["", "   "])
def test_a_blank_service_is_rejected(service: str) -> None:
    with pytest.raises(SlotGenerationError):
        generate_day_slots(DAY, PROVIDER, service)


def test_a_runaway_request_is_capped() -> None:
    with pytest.raises(SlotGenerationError) as excinfo:
        _slots(minutes=1)

    assert str(MAX_SLOTS_PER_DAY) in str(excinfo.value)


def test_provider_and_service_are_trimmed() -> None:
    slots = generate_day_slots(DAY, "  prov-a  ", "  Hearing Test  ", start="09:00", end="10:00")

    assert slots[0].provider_id == "prov-a"
    assert slots[0].service == "Hearing Test"


# ---------------------------------------------------------------------------
# Publishing a range of days
# ---------------------------------------------------------------------------


def _range(first: str, last: str, **kwargs: object) -> list:
    from clinic_front_desk.scheduling import generate_range_slots

    return generate_range_slots(first, last, PROVIDER, SERVICE, **kwargs)  # type: ignore[arg-type]


def test_a_single_day_range_matches_a_single_day_publish() -> None:
    assert [s.id for s in _range(DAY, DAY)] == [s.id for s in _slots()]


def test_a_range_covers_every_day_inclusive() -> None:
    slots = _range("2026-09-10", "2026-09-12", start="09:00", end="10:00")

    assert {s.start[:10] for s in slots} == {
        "2026-09-10",
        "2026-09-11",
        "2026-09-12",
    }
    assert len(slots) == 6


def test_a_range_is_ordered_earliest_first_across_days() -> None:
    slots = _range("2026-09-10", "2026-09-12", start="09:00", end="10:00")

    assert [s.start for s in slots] == sorted(s.start for s in slots)


def test_closed_weekdays_are_skipped() -> None:
    # 2026-09-10 is a Thursday, so this week runs Thu Fri Sat Sun Mon.
    # Sunday is 0 in the clinic's convention; excluding it must drop 2026-09-13.
    slots = _range(
        "2026-09-10",
        "2026-09-14",
        start="09:00",
        end="10:00",
        open_weekdays=frozenset({1, 2, 3, 4, 5, 6}),
    )

    days = {s.start[:10] for s in slots}
    assert "2026-09-13" not in days
    assert days == {"2026-09-10", "2026-09-11", "2026-09-12", "2026-09-14"}


def test_publishing_only_one_weekday_works() -> None:
    # Thursdays only, across two weeks.
    slots = _range(
        "2026-09-10", "2026-09-24", start="09:00", end="10:00",
        open_weekdays=frozenset({4}),
    )

    assert {s.start[:10] for s in slots} == {
        "2026-09-10",
        "2026-09-17",
        "2026-09-24",
    }


def test_the_rest_of_a_year_is_within_the_limit() -> None:
    slots = _range("2026-09-09", "2026-12-31", start="09:00", end="10:00")

    assert len({s.start[:10] for s in slots}) == 114


def test_an_inverted_range_is_rejected() -> None:
    with pytest.raises(SlotGenerationError) as excinfo:
        _range("2026-09-12", "2026-09-10")

    assert "is empty" in str(excinfo.value)


def test_a_range_beyond_the_publish_limit_is_rejected() -> None:
    from clinic_front_desk.scheduling import MAX_PUBLISH_DAYS

    with pytest.raises(SlotGenerationError) as excinfo:
        _range("2026-01-01", "2030-01-01")

    # A typo in the end date must not try to open the next decade.
    assert str(MAX_PUBLISH_DAYS) in str(excinfo.value)


def test_a_range_with_no_open_days_is_rejected_rather_than_silently_empty() -> None:
    with pytest.raises(SlotGenerationError) as excinfo:
        # 2026-09-13 is a Sunday; publishing only Mondays over that single day
        # leaves nothing.
        _range("2026-09-13", "2026-09-13", open_weekdays=frozenset({1}))

    assert "nothing to publish" in str(excinfo.value)


@pytest.mark.parametrize("bad", ["", "13-09-2026", "not-a-date"])
def test_a_malformed_range_date_is_rejected(bad: str) -> None:
    with pytest.raises(SlotGenerationError):
        _range(DAY, bad)
    with pytest.raises(SlotGenerationError):
        _range(bad, DAY)


def test_ids_stay_unique_across_a_range() -> None:
    slots = _range("2026-09-10", "2026-09-20")

    assert len({s.id for s in slots}) == len(slots)
