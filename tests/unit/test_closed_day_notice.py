"""A caller asking for a day the clinic is shut must be told that, by name.

Availability searches "on or after", so asking for Sunday 13 September returns
Monday's slots. The times are right and the reason is absent, so the agent reported
that it could not find anything for the thirteenth. To a caller that sounds fully
booked — indistinguishable from a busy day, and no reason to try Monday.

The fix is data, not phrasing: the tool result carries the closed weekday, so the
agent has the fact to state rather than a gap to fill in.
"""

from __future__ import annotations

import pytest

from clinic_front_desk.tools.availability import closed_weekday_name

#: Sunday = 0, matching the clinic's ``hours`` map.
MON_TO_SAT = frozenset({1, 2, 3, 4, 5, 6})
EVERY_DAY = frozenset(range(7))


# -- the live case ---------------------------------------------------------


def test_the_thirteenth_is_reported_as_a_closed_sunday() -> None:
    assert closed_weekday_name("2026-09-13", MON_TO_SAT) == "Sunday"


def test_a_working_day_is_not_reported_closed() -> None:
    """15 September is a Tuesday and the clinic is open."""
    assert closed_weekday_name("2026-09-15", MON_TO_SAT) is None


@pytest.mark.parametrize(
    "day",
    ["2026-09-13", "2026-09-20", "2026-09-27", "2026-12-27"],
)
def test_every_sunday_in_the_published_range_is_closed(day: str) -> None:
    assert closed_weekday_name(day, MON_TO_SAT) == "Sunday"


@pytest.mark.parametrize(
    "day",
    ["2026-09-14", "2026-09-15", "2026-10-31", "2026-12-31"],
)
def test_open_days_stay_open(day: str) -> None:
    assert closed_weekday_name(day, MON_TO_SAT) is None


# -- it follows the configured hours, not a hardcoded weekend --------------


def test_a_clinic_that_opens_sunday_and_shuts_tuesday() -> None:
    """No code change should be needed for an unusual week."""
    opens = frozenset({0, 1, 3, 4, 5, 6})  # everything but Tuesday
    assert closed_weekday_name("2026-09-13", opens) is None  # Sunday, open here
    assert closed_weekday_name("2026-09-15", opens) == "Tuesday"


def test_a_clinic_open_every_day_never_reports_closed() -> None:
    assert closed_weekday_name("2026-09-13", EVERY_DAY) is None


# -- what must not be claimed ---------------------------------------------


def test_no_configured_hours_is_not_a_closed_day() -> None:
    """With nothing configured, "the clinic is closed on Sunday" would be a guess."""
    assert closed_weekday_name("2026-09-13", frozenset()) is None


@pytest.mark.parametrize("bad", ["", "not-a-date", "2026-13-45", "13/09/2026"])
def test_an_unparseable_date_is_not_called_closed(bad: str) -> None:
    """A malformed date is a separate problem; do not turn it into a closure."""
    assert closed_weekday_name(bad, MON_TO_SAT) is None


def test_a_full_timestamp_is_accepted() -> None:
    """Slot starts carry a time; the date part is what decides the weekday."""
    assert closed_weekday_name("2026-09-13T09:00", MON_TO_SAT) == "Sunday"


# -- the briefing the agent gets at the start of every call ---------------
#
# The tool notice only fires once a date has been passed to check_availability.
# The briefing is what stops the agent inventing an answer before it ever calls
# the tool, so the closed days have to be named there too.


def test_the_briefing_names_the_closed_day_as_a_holiday() -> None:
    from clinic_front_desk.models import (
        ClinicKnowledgeBase,
        DayHours,
        Provider,
        ServiceConfig,
    )
    from clinic_front_desk.voice.clinic_briefing import _open_weekday_names

    kb = ClinicKnowledgeBase(
        location="Tirupati",
        hours={
            day: (DayHours(open="09:00", close="20:00") if day in MON_TO_SAT else None)
            for day in range(7)
        },
        services=[ServiceConfig(name="ENT Consultation")],
        providers=[Provider(id="prov-raana", name="Dr Raana", specialty="ENT")],
    )

    open_days = _open_weekday_names(kb)
    assert "Sunday" not in open_days
    assert "Monday" in open_days
