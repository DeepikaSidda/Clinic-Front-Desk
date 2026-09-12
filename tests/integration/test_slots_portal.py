"""Integration tests for the doctor's day-calendar portal (``/slots``)."""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from clinic_front_desk.deployment.app import build_memory_application
from clinic_front_desk.models import (
    ClinicKnowledgeBase,
    DayHours,
    Provider,
    ServiceConfig,
    SlotStatus,
    is_ok,
)

pytestmark = pytest.mark.integration

starlette_testclient = pytest.importorskip("starlette.testclient")
TestClient = starlette_testclient.TestClient

def _test_day() -> str:
    """A published day far enough ahead that the wall clock cannot invalidate it.

    This was a fixed ``2026-09-10``, which happened to be "today" — so the tests
    asserting that a 09:00-12:00 window is offered to a caller passed all morning
    and failed from noon onward, because availability correctly refuses to offer a
    time that has already started. The date has to be in the future for the
    assertion to mean what it says.

    Sundays are skipped: the fixture configures hours for Monday to Saturday only,
    and publishing skips days the clinic has no hours for.
    """
    day = datetime.now(UTC).date() + timedelta(days=7)
    if day.weekday() == 6:  # Python: Monday=0 ... Sunday=6
        day += timedelta(days=1)
    return day.isoformat()


DAY = _test_day()
PROVIDER = "prov-raana"


@pytest.fixture
def app() -> Any:
    application = build_memory_application()
    application.stores.knowledge_base.save(
        ClinicKnowledgeBase(
            location="Aster Narayanadri Hospital, Renigunta Road, Tirupati",
            hours={d: DayHours(open="00:00", close="23:59") for d in range(1, 7)},
            services=[
                ServiceConfig(name="ENT Consultation", price=500.0),
                ServiceConfig(name="Hearing Test", price=800.0),
            ],
            providers=[
                Provider(id=PROVIDER, name="Dr. Kuppam Divya Raana", specialty="ENT")
            ],
            configured=True,
        )
    )
    return application


@pytest.fixture
def client(app: Any) -> Any:
    from clinic_front_desk.deployment.server import create_asgi_app

    return TestClient(create_asgi_app(app))


def _stat(page: str, label: str) -> int:
    """Read one count out of the day's summary strip.

    Asserted on the number rather than a formatted sentence, so restyling the
    strip does not break tests about how many slots exist.
    """
    match = re.search(
        r"<strong>(\d+)</strong><span>" + re.escape(label) + r"</span>", page
    )
    assert match is not None, f"no {label!r} count on the page"
    return int(match.group(1))


def _publish(client: Any, **overrides: str) -> Any:
    form = {
        "day": DAY,
        "provider_id": PROVIDER,
        "service": "ENT Consultation",
        "minutes": "30",
        "start": "00:00",
        # Empty means "through to midnight", which is how the form expresses a full
        # day — an <input type="time"> cannot hold 24:00.
        "end": "",
    }
    form.update(overrides)
    return client.post("/slots?role=doctor", data=form)


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def test_the_calendar_needs_a_role(client: Any) -> None:
    assert client.get("/slots").status_code == 403


def test_the_doctor_and_the_assistant_may_both_manage_the_calendar(client: Any) -> None:
    # Gated on the schedule view, which the assistant also holds — running the
    # day's diary is front-desk work.
    assert client.get("/slots?role=doctor").status_code == 200
    assert client.get("/slots?role=assistant").status_code == 200


def test_an_unknown_role_is_denied(client: Any) -> None:
    assert client.get("/slots?role=intruder").status_code == 403


def test_the_dashboard_links_to_the_calendar(client: Any) -> None:
    page = client.get("/?role=doctor").text

    assert 'href="/slots' in page


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def test_publishing_a_full_day_creates_forty_eight_half_hour_slots(
    client: Any, app: Any
) -> None:
    response = _publish(client)

    assert response.status_code == 200
    assert "Published 48 slots of 30 minutes" in response.text

    stored = app.stores.appointments.list_slots_for_day(PROVIDER, DAY)
    assert is_ok(stored)
    assert len(stored.value) == 48
    assert stored.value[0].start == f"{DAY}T00:00"
    assert stored.value[-1].start == f"{DAY}T23:30"


def test_the_published_day_is_rendered_as_a_grid(client: Any) -> None:
    page = _publish(client).text

    assert "day-schedule__grid" in page
    assert page.count('class="day-schedule__cell"') == 48
    assert "00:00" in page and "23:30" in page
    assert _stat(page, "open") == 48
    assert _stat(page, "booked") == 0
    assert _stat(page, "blocked") == 0
    assert _stat(page, "total") == 48
    # Grouped by part of day: 48 times as one flat run is a wall nobody scans.
    for heading in ("Overnight", "Morning", "Afternoon", "Evening"):
        assert heading in page, heading


def test_the_service_name_is_not_repeated_on_every_open_slot(client: Any) -> None:
    """One label stated once, rather than forty-eight times saying nothing."""
    page = _publish(client).text

    assert page.count("day-schedule__cell-service") == 0
    assert "Every slot below is published as ENT Consultation" in page


def test_republishing_the_same_day_does_not_duplicate_slots(
    client: Any, app: Any
) -> None:
    _publish(client)
    _publish(client)

    stored = app.stores.appointments.list_slots_for_day(PROVIDER, DAY)

    # Stable ids mean the publish button is safe to press twice.
    assert len(stored.value) == 48


def test_republishing_leaves_a_booked_slot_alone(client: Any, app: Any) -> None:
    _publish(client)
    slots = app.stores.appointments.list_slots_for_day(PROVIDER, DAY).value
    booked_id = slots[20].id
    app.stores.appointments.set_slot_status(booked_id, SlotStatus.BOOKED)

    response = _publish(client)

    assert "1 already-booked or blocked slot were left as they are" in response.text
    after = app.stores.appointments.get_slot(booked_id)
    # Republishing must never silently strand a patient's appointment.
    assert after.value is not None
    assert after.value.status is SlotStatus.BOOKED


def test_a_booked_slot_is_visibly_different_in_the_grid(client: Any, app: Any) -> None:
    _publish(client)
    slots = app.stores.appointments.list_slots_for_day(PROVIDER, DAY).value
    app.stores.appointments.set_slot_status(slots[0].id, SlotStatus.BOOKED)

    page = client.get(f"/slots?role=doctor&day={DAY}&provider_id={PROVIDER}").text

    assert 'data-status="booked"' in page
    assert _stat(page, "open") == 47
    assert _stat(page, "booked") == 1
    # No block control on a booked slot: freeing that time means cancelling the
    # patient's appointment, which is a separate act.
    assert "day-schedule__cell-locked" in page


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [("15", 96), ("30", 48), ("60", 24)],
)
def test_the_slot_length_is_honoured(
    client: Any, app: Any, minutes: str, expected: int
) -> None:
    _publish(client, minutes=minutes)

    stored = app.stores.appointments.list_slots_for_day(PROVIDER, DAY)

    assert len(stored.value) == expected


def test_a_working_window_can_be_published_instead_of_the_whole_day(
    client: Any, app: Any
) -> None:
    _publish(client, start="09:00", end="17:00")

    stored = app.stores.appointments.list_slots_for_day(PROVIDER, DAY)

    assert len(stored.value) == 16
    assert stored.value[0].start == f"{DAY}T09:00"


# ---------------------------------------------------------------------------
# Rejections that stay on the page
# ---------------------------------------------------------------------------


def test_an_inverted_window_is_reported_not_a_server_error(
    client: Any, app: Any
) -> None:
    response = _publish(client, start="17:00", end="09:00")

    assert response.status_code == 200
    assert "wizard-error" in response.text
    assert app.stores.appointments.list_slots_for_day(PROVIDER, DAY).value == []


def test_a_service_the_clinic_does_not_offer_is_refused(client: Any, app: Any) -> None:
    # Slots must be bookable for a configured service or the agent could never
    # match a caller's request to them.
    response = _publish(client, service="Rhinoplasty")

    assert response.status_code == 200
    assert "not one of the clinic" in response.text
    assert app.stores.appointments.list_slots_for_day(PROVIDER, DAY).value == []


def test_a_non_numeric_slot_length_is_reported(client: Any) -> None:
    response = _publish(client, minutes="half an hour")

    assert response.status_code == 200
    assert "must be a number" in response.text


def test_a_malformed_day_is_reported(client: Any) -> None:
    response = _publish(client, day="10-09-2026")

    assert response.status_code == 200
    assert "wizard-error" in response.text


# ---------------------------------------------------------------------------
# Empty and unconfigured states
# ---------------------------------------------------------------------------


def test_an_unpublished_day_says_the_agent_has_nothing_to_offer(client: Any) -> None:
    page = client.get(f"/slots?role=doctor&day={DAY}&provider_id={PROVIDER}").text

    assert "No slots published for this day" in page
    assert "day-schedule__grid" not in page


def test_a_clinic_with_no_provider_is_sent_to_onboarding() -> None:
    from clinic_front_desk.deployment.server import create_asgi_app

    client = TestClient(create_asgi_app(build_memory_application()))

    page = client.get("/slots?role=doctor").text

    assert "need a provider and at least one service" in page
    assert 'href="/onboarding"' in page


# ---------------------------------------------------------------------------
# Navigation keeps the role, so no button answers 403
# ---------------------------------------------------------------------------


def test_every_link_and_form_on_the_page_carries_the_role(client: Any) -> None:
    _publish(client)
    page = client.get(f"/slots?role=doctor&day={DAY}&provider_id={PROVIDER}").text

    for raw in re.findall(r'href="(/[^"]*)"', page):
        if raw.startswith("/static") or raw == "/onboarding":
            continue
        # The markup escapes "&" as "&amp;", which is correct HTML; a browser
        # unescapes before requesting, so the test has to as well or the role
        # arrives as part of the previous parameter's value.
        url = html.unescape(raw)
        assert "role=" in url, f"link drops the role: {url}"
        assert client.get(url).status_code in {200, 303}, f"broken link: {url}"

    for url in re.findall(r'action="(/[^"]*)"', page):
        # The day picker is a GET form carrying the role in a hidden field.
        assert "role=" in url or 'name="role"' in page, f"action drops the role: {url}"


# ---------------------------------------------------------------------------
# The point: a published slot becomes bookable by the agent
# ---------------------------------------------------------------------------


def test_a_published_slot_is_offered_to_a_caller(client: Any, app: Any) -> None:
    _publish(client, start="09:00", end="12:00")

    session = app.start_voice_session("slots-e2e")
    offered = session.toolset.check_availability(
        service="ENT Consultation", from_date=DAY, limit=3
    )

    assert offered.__class__.__name__ == "Ok"
    assert len(offered.value) == 3
    assert offered.value[0].start == f"{DAY}T09:00"


def test_nothing_is_offered_before_the_day_is_published(client: Any, app: Any) -> None:
    session = app.start_voice_session("slots-empty")

    offered = session.toolset.check_availability(
        service="ENT Consultation", from_date=DAY, limit=3
    )

    # Nothing to offer, and nothing invented: the agent cannot create availability
    # the doctor has not opened.
    assert offered.__class__.__name__ == "Ok"
    assert offered.value == []


# ---------------------------------------------------------------------------
# Blocking: the doctor takes time off the calendar
# ---------------------------------------------------------------------------


def _block(client: Any, **overrides: str) -> Any:
    form = {"day": DAY, "provider_id": PROVIDER, "blocked": "1"}
    form.update(overrides)
    return client.post("/slots/block?role=doctor", data=form)


def _slot_at(app: Any, clock: str) -> Any:
    slots = app.stores.appointments.list_slots_for_day(PROVIDER, DAY).value
    return next(s for s in slots if s.start.endswith(f"T{clock}"))


def test_blocking_a_single_slot_takes_it_off_the_calendar(
    client: Any, app: Any
) -> None:
    _publish(client)
    target = _slot_at(app, "13:00")

    response = _block(client, slot_id=target.id)

    assert response.status_code == 200
    assert "Blocked 1 slot." in response.text
    assert app.stores.appointments.get_slot(target.id).value.status is SlotStatus.BLOCKED


def test_a_blocked_slot_is_no_longer_offered_to_a_caller(client: Any, app: Any) -> None:
    _publish(client, start="09:00", end="10:00")
    target = _slot_at(app, "09:00")

    _block(client, slot_id=target.id)

    session = app.start_voice_session("blocked-e2e")
    offered = session.toolset.check_availability(
        service="ENT Consultation", from_date=DAY, limit=5
    )

    assert offered.__class__.__name__ == "Ok"
    assert [s.start for s in offered.value] == [f"{DAY}T09:30"]


def test_a_blocked_slot_stays_visible_on_the_doctors_calendar(
    client: Any, app: Any
) -> None:
    _publish(client)
    _block(client, slot_id=_slot_at(app, "13:00").id)

    page = client.get(f"/slots?role=doctor&day={DAY}&provider_id={PROVIDER}").text

    # Off the offerable set but still on the calendar, or the doctor could not see
    # what they had blocked.
    assert 'data-status="blocked"' in page
    assert _stat(page, "open") == 47
    assert _stat(page, "booked") == 0
    assert _stat(page, "blocked") == 1


def test_a_blocked_slot_can_be_reopened(client: Any, app: Any) -> None:
    _publish(client)
    target = _slot_at(app, "13:00")
    _block(client, slot_id=target.id)

    response = _block(client, slot_id=target.id, blocked="0")

    assert "Reopened 1 slot." in response.text
    assert app.stores.appointments.get_slot(target.id).value.status is SlotStatus.OPEN


def test_a_range_can_be_blocked_in_one_action(client: Any, app: Any) -> None:
    _publish(client)

    response = _block(client, start="13:00", end="14:00")

    # A lunch hour is two 30-minute slots.
    assert "Blocked 2 slots." in response.text
    assert _slot_at(app, "13:00").status is SlotStatus.BLOCKED
    assert _slot_at(app, "13:30").status is SlotStatus.BLOCKED
    # The boundary is exclusive, so 14:00 itself stays open.
    assert _slot_at(app, "14:00").status is SlotStatus.OPEN


def test_a_blocked_range_can_be_reopened_in_one_action(client: Any, app: Any) -> None:
    _publish(client)
    _block(client, start="13:00", end="14:00")

    response = _block(client, start="13:00", end="14:00", blocked="0")

    assert "Reopened 2 slots." in response.text
    assert _slot_at(app, "13:00").status is SlotStatus.OPEN


def test_blocking_never_touches_a_booked_slot(client: Any, app: Any) -> None:
    _publish(client)
    booked = _slot_at(app, "13:00")
    app.stores.appointments.set_slot_status(booked.id, SlotStatus.BOOKED)

    response = _block(client, start="13:00", end="14:00")

    # Blocking it would leave the patient holding an appointment on time the
    # calendar says is unavailable.
    assert app.stores.appointments.get_slot(booked.id).value.status is SlotStatus.BOOKED
    assert "1 booked slot left alone" in response.text
    assert "Blocked 1 slot." in response.text


def test_blocking_only_booked_slots_reports_that_nothing_changed(
    client: Any, app: Any
) -> None:
    _publish(client)
    booked = _slot_at(app, "13:00")
    app.stores.appointments.set_slot_status(booked.id, SlotStatus.BOOKED)

    response = _block(client, slot_id=booked.id)

    assert "wizard-error" in response.text
    assert "Cancel the appointment first" in response.text


def test_republishing_the_day_does_not_clear_a_block(client: Any, app: Any) -> None:
    _publish(client)
    target = _slot_at(app, "13:00")
    _block(client, slot_id=target.id)

    response = _publish(client)

    # Otherwise the time the doctor protected would quietly become bookable again.
    assert app.stores.appointments.get_slot(target.id).value.status is SlotStatus.BLOCKED
    assert "left as they are" in response.text


def test_an_empty_range_is_reported(client: Any) -> None:
    _publish(client)

    response = _block(client, start="14:00", end="13:00")

    assert response.status_code == 200
    assert "is empty" in response.text


def test_a_range_matching_no_slots_is_reported(client: Any) -> None:
    _publish(client, start="09:00", end="10:00")

    response = _block(client, start="20:00", end="21:00")

    assert "No slots fall between" in response.text


def test_a_block_with_neither_slot_nor_range_is_reported(client: Any) -> None:
    _publish(client)

    response = _block(client)

    assert "Choose a slot, or a time range" in response.text


def test_blocking_an_unknown_slot_is_a_404(client: Any) -> None:
    _publish(client)

    assert _block(client, slot_id="slot-nope").status_code == 404


def test_blocking_needs_a_role(client: Any) -> None:
    _publish(client)

    response = client.post(
        "/slots/block", data={"day": DAY, "provider_id": PROVIDER, "blocked": "1"}
    )

    assert response.status_code == 403


def test_the_block_range_form_and_per_slot_controls_are_rendered(client: Any) -> None:
    page = _publish(client).text

    assert "day-schedule__block" in page
    assert 'action="/slots/block?role=doctor"' in page
    assert page.count("day-schedule__cell-action") >= 48
