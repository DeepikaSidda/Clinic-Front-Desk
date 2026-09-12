"""Unit tests for dashboard rendering states (task 13.6).

Covers three concrete rendering states called out by the requirements:

- decision controls rendering (Req 14.2) — every open Decision renders an
  approve and a dismiss control targeting its id,
- empty Decisions feed (Req 14.7) — with no open Decisions the feed shows its
  empty-state message and hides the card list,
- default schedule view (Req 15.1) — a provider's appointments and open slots
  for a day render as rows, and an empty day shows the empty-state message.

These exercise the server-side renderers in ``dashboard/components/decisions_feed.py``
and ``dashboard/schedule_view.py``. Helpers are defined locally so no existing
test file is touched.
"""

from __future__ import annotations

import pytest

from clinic_front_desk.dashboard.bff import ScheduleView
from clinic_front_desk.dashboard.components.decisions_feed import (
    EMPTY_STATE_MESSAGE,
    render_decisions_feed,
)
from clinic_front_desk.dashboard.schedule_view import (
    SCHEDULE_EMPTY_MESSAGE,
    render_schedule_view,
)
from clinic_front_desk.models import (
    Appointment,
    AppointmentStatus,
    Decision,
    DecisionKind,
    Slot,
    SlotStatus,
)

_PROVIDER = "prov1"
_SERVICE = "ent"
_DAY = "2025-06-01"


def _decision(id: str, *, generated_at: str) -> Decision:
    return Decision(
        id=id,
        kind=DecisionKind.NO_SHOW_TREND,
        finding_key=f"fk-{id}",
        summary="summary",
        recommended_action="do the thing",
        supporting_record_count=5,
        generated_at=generated_at,
    )


# ---------------------------------------------------------------------------
# Decision controls rendering (Req 14.2)
# ---------------------------------------------------------------------------


def test_decision_controls_render_approve_and_dismiss_per_card() -> None:
    """Each open Decision renders an approve and a dismiss control bound to its
    id (Req 14.2)."""
    html_out = render_decisions_feed(
        [
            _decision("d1", generated_at="2025-01-01T09:00:00+00:00"),
            _decision("d2", generated_at="2025-02-01T09:00:00+00:00"),
        ]
    )

    # One approve and one dismiss control per card (the per-card buttons carry
    # data-decision-id; the static <template> button does not, so it is not
    # counted).
    assert html_out.count('data-action="approve" data-decision-id=') == 2
    assert html_out.count('data-action="dismiss" data-decision-id=') == 2
    # Controls target the specific Decision ids.
    assert 'data-action="approve" data-decision-id="d1"' in html_out
    assert 'data-action="dismiss" data-decision-id="d1"' in html_out
    assert 'data-action="approve" data-decision-id="d2"' in html_out
    assert 'data-action="dismiss" data-decision-id="d2"' in html_out
    # Card list is visible, the empty state is hidden.
    assert 'id="decisions-feed-list" class="decisions-feed__list">' in html_out


# ---------------------------------------------------------------------------
# Empty Decisions feed (Req 14.7)
# ---------------------------------------------------------------------------


def test_empty_decisions_feed_shows_empty_state_and_hides_list() -> None:
    """With no open Decisions the feed shows the empty-state message and hides
    the card list (Req 14.7)."""
    html_out = render_decisions_feed([])

    assert EMPTY_STATE_MESSAGE in html_out
    # Empty-state region visible (no `hidden`), list region hidden.
    assert (
        'id="decisions-feed-empty" class="decisions-feed__empty" role="status">'
        in html_out
    )
    assert 'id="decisions-feed-list" class="decisions-feed__list" hidden>' in html_out
    # No decision cards were rendered: the only markup carrying a bound
    # data-decision-id is a rendered card control, of which there are none (the
    # static <template> button has no data-decision-id).
    assert html_out.count('data-action="approve" data-decision-id=') == 0
    assert html_out.count('data-action="dismiss" data-decision-id=') == 0


# ---------------------------------------------------------------------------
# Default schedule view (Req 15.1)
# ---------------------------------------------------------------------------


def _schedule_view(*, appointments: list[Appointment], open_slots: list[Slot]) -> ScheduleView:
    return ScheduleView(
        provider_id=_PROVIDER,
        day=_DAY,
        appointments=appointments,
        open_slots=open_slots,
    )


def test_default_schedule_view_renders_appointments_and_open_slots() -> None:
    """The day's appointments and open slots render as rows for the provider
    (Req 15.1)."""
    appointment = Appointment(
        id="appt1",
        provider_id=_PROVIDER,
        patient_id="patient1",
        service=_SERVICE,
        slot_id="slotA",
        date=_DAY,
        time="09:00",
        status=AppointmentStatus.BOOKED,
    )
    open_slot = Slot(
        id="slotB",
        provider_id=_PROVIDER,
        service=_SERVICE,
        start=f"{_DAY}T11:00:00Z",
        end=f"{_DAY}T11:30:00Z",
        status=SlotStatus.OPEN,
    )

    html_out = render_schedule_view(
        _schedule_view(appointments=[appointment], open_slots=[open_slot])
    )

    # Provider and day are embedded for the client's day picker / refetch wiring.
    assert f'data-provider-id="{_PROVIDER}"' in html_out
    assert f'data-day="{_DAY}"' in html_out
    # The booked appointment row renders with its identity and time.
    assert 'data-appointment-id="appt1"' in html_out
    assert "patient1" in html_out
    assert "09:00" in html_out
    # The open slot row renders.
    assert 'data-slot-id="slotB"' in html_out
    assert "11:00" in html_out
    # Not the empty state: the empty region is hidden and the lists are visible.
    assert 'data-role="schedule-empty" role="status" hidden>' in html_out
    assert 'class="schedule-view__lists">' in html_out


def test_default_schedule_view_orders_appointments_earliest_first() -> None:
    """Appointments render earliest-first within the day (Req 15.1)."""
    later = Appointment(
        id="late",
        provider_id=_PROVIDER,
        patient_id="p-late",
        service=_SERVICE,
        slot_id="s-late",
        date=_DAY,
        time="15:00",
        status=AppointmentStatus.BOOKED,
    )
    earlier = Appointment(
        id="early",
        provider_id=_PROVIDER,
        patient_id="p-early",
        service=_SERVICE,
        slot_id="s-early",
        date=_DAY,
        time="08:00",
        status=AppointmentStatus.BOOKED,
    )

    html_out = render_schedule_view(
        _schedule_view(appointments=[later, earlier], open_slots=[])
    )

    assert html_out.index('data-appointment-id="early"') < html_out.index(
        'data-appointment-id="late"'
    )


def test_empty_schedule_day_shows_empty_state() -> None:
    """A day with no appointments and no open slots shows the empty-state
    message (Req 15.1)."""
    html_out = render_schedule_view(
        _schedule_view(appointments=[], open_slots=[])
    )

    assert SCHEDULE_EMPTY_MESSAGE in html_out
    assert 'data-appointment-id=' not in html_out
    assert 'data-slot-id=' not in html_out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
