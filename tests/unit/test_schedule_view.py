"""Unit tests for the ScheduleView view-model + renderer (task 13.2, Req 15.1,
15.4, 15.6).

These exercise the pure Python view-model builder and server-side renderer in
``clinic_front_desk.dashboard.schedule_view``: appointments and open slots for
the day (Req 15.1), earliest-first ordering, the empty-state message, mapping a
failed schedule read to a recoverable error view, HTML escaping of
caller-supplied text, and the ``data-*`` wiring the client JS uses for real-time
refresh (Req 15.4) and day selection (Req 15.6).
"""

from __future__ import annotations

from clinic_front_desk.dashboard.bff import ScheduleView
from clinic_front_desk.dashboard.schedule_view import (
    SCHEDULE_EMPTY_MESSAGE,
    SCHEDULE_ERROR_MESSAGE,
    UNNAMED_PATIENT,
    build_schedule_view_model,
    build_schedule_view_model_from_result,
    render_schedule_view,
)
from clinic_front_desk.models import (
    Appointment,
    AppointmentStatus,
    Err,
    Ok,
    Slot,
    SlotStatus,
    StoreError,
    StoreErrorKind,
)

PROVIDER = "prov-1"
DAY = "2025-06-01"


def _appointment(
    id: str,
    time: str,
    *,
    service: str = "consult",
    patient_id: str = "pat-1",
    status: AppointmentStatus = AppointmentStatus.BOOKED,
) -> Appointment:
    return Appointment(
        id=id,
        provider_id=PROVIDER,
        patient_id=patient_id,
        service=service,
        slot_id=f"slot-of-{id}",
        date=DAY,
        time=time,
        status=status,
    )


def _slot(
    id: str,
    start: str,
    end: str,
    *,
    service: str = "consult",
) -> Slot:
    return Slot(
        id=id,
        provider_id=PROVIDER,
        service=service,
        start=start,
        end=end,
        status=SlotStatus.OPEN,
    )


def _view(appointments: list[Appointment], open_slots: list[Slot]) -> ScheduleView:
    return ScheduleView(
        provider_id=PROVIDER,
        day=DAY,
        appointments=appointments,
        open_slots=open_slots,
    )


def test_empty_day_shows_empty_state_message() -> None:
    # No appointments and no open slots -> empty-state message (Req 15.1).
    vm = build_schedule_view_model(_view([], []))

    assert vm.appointments == []
    assert vm.open_slots == []
    assert vm.is_empty is True
    assert vm.has_error is False
    assert vm.empty_message == SCHEDULE_EMPTY_MESSAGE
    assert vm.provider_id == PROVIDER
    assert vm.day == DAY


def test_appointments_ordered_earliest_first() -> None:
    # Supplied out of order; view-model sorts by clock time (Req 15.1).
    vm = build_schedule_view_model(
        _view(
            [_appointment("a-late", "15:30"), _appointment("a-early", "09:00")],
            [],
        )
    )

    assert [a.appointment_id for a in vm.appointments] == ["a-early", "a-late"]
    assert vm.appointments[0].time == "09:00"
    assert vm.is_empty is False


def test_open_slots_ordered_earliest_first_and_trimmed() -> None:
    # Slots sorted by ISO start; times trimmed to HH:MM (Req 15.1).
    vm = build_schedule_view_model(
        _view(
            [],
            [
                _slot("s-late", "2025-06-01T14:00:00Z", "2025-06-01T14:30:00Z"),
                _slot("s-early", "2025-06-01T08:00:00Z", "2025-06-01T08:30:00Z"),
            ],
        )
    )

    assert [s.slot_id for s in vm.open_slots] == ["s-early", "s-late"]
    assert vm.open_slots[0].start_time == "08:00"
    assert vm.open_slots[0].end_time == "08:30"


def test_appointment_status_labelled() -> None:
    vm = build_schedule_view_model(
        _view([_appointment("a1", "10:00", status=AppointmentStatus.RESCHEDULED)], [])
    )

    row = vm.appointments[0]
    assert row.status == "rescheduled"
    assert row.status_label == "Rescheduled"


def test_appointment_time_trimmed_to_hh_mm() -> None:
    vm = build_schedule_view_model(_view([_appointment("a1", "09:05:30")], []))

    assert vm.appointments[0].time == "09:05"


def test_from_result_error_maps_to_recoverable_error_view() -> None:
    # A failed schedule read renders a recoverable error, not an empty day.
    err: object = Err(
        StoreError(kind=StoreErrorKind.STORE_FAILURE, detail="backend down")
    )

    vm = build_schedule_view_model_from_result(
        err, provider_id=PROVIDER, day=DAY  # type: ignore[arg-type]
    )

    assert vm.has_error is True
    assert vm.is_empty is True
    assert vm.empty_message == SCHEDULE_ERROR_MESSAGE
    assert vm.provider_id == PROVIDER
    assert vm.day == DAY


def test_from_result_ok_builds_normal_view() -> None:
    ok = Ok(_view([_appointment("a1", "10:00")], []))

    vm = build_schedule_view_model_from_result(ok)

    assert vm.has_error is False
    assert [a.appointment_id for a in vm.appointments] == ["a1"]


def test_render_includes_rows_and_data_wiring() -> None:
    # The rendered partial carries the data-* wiring the JS uses for real-time
    # refresh (Req 15.4) and day selection (Req 15.6), plus the day's rows.
    html_out = render_schedule_view(
        _view(
            [_appointment("a1", "10:00", service="cleaning", patient_id="pat-9")],
            [_slot("s1", "2025-06-01T11:00:00Z", "2025-06-01T11:30:00Z")],
        ),
        schedule_endpoint="/dashboard/schedule",
    )

    assert 'data-component="schedule-view"' in html_out
    assert 'data-provider-id="prov-1"' in html_out
    assert 'data-day="2025-06-01"' in html_out
    assert 'data-schedule-endpoint="/dashboard/schedule"' in html_out
    # Rows rendered for the day (Req 15.1).
    assert 'data-appointment-id="a1"' in html_out
    assert 'data-slot-id="s1"' in html_out
    assert "cleaning" in html_out
    assert "pat-9" in html_out
    assert "11:00" in html_out


def test_render_empty_day_shows_message_and_hides_lists() -> None:
    html_out = render_schedule_view(_view([], []))

    assert SCHEDULE_EMPTY_MESSAGE in html_out
    # Lists container hidden, empty message visible when the day is empty.
    assert 'class="schedule-view__lists" hidden' in html_out


def test_render_escapes_caller_supplied_text() -> None:
    # Patient/service text must be HTML-escaped so it cannot inject markup.
    html_out = render_schedule_view(
        _view(
            [_appointment("a1", "10:00", patient_id="<script>alert(1)</script>")],
            [],
        )
    )

    assert "<script>alert(1)</script>" not in html_out
    assert "&lt;script&gt;" in html_out


def test_appointment_row_names_the_patient_when_a_name_is_supplied() -> None:
    # The doctor's day has to say who is arriving. Rendering the raw patient id
    # gave her a column of hex, which is what this guards against.
    vm = build_schedule_view_model(
        _view([_appointment("a-1", "09:00", patient_id="pat-9")], []),
        patient_names={"pat-9": "Sailaja Devi"},
    )

    assert vm.appointments[0].patient_display == "Sailaja Devi"
    # The id stays on the row: it is the link target and the reconciliation key.
    assert vm.appointments[0].patient_id == "pat-9"

    html_out = render_schedule_view(vm)
    assert "Sailaja Devi" in html_out
    # The id must still be reachable by the client, as an attribute not as text.
    assert 'data-patient-id="pat-9"' in html_out
    assert ">pat-9<" not in html_out


def test_appointment_row_falls_back_to_the_id_when_the_name_is_unknown() -> None:
    # A lookup that failed must not blank the row: the doctor still needs to see
    # the time is taken, and the id is enough to match against the patient list.
    vm = build_schedule_view_model(
        _view([_appointment("a-1", "09:00", patient_id="pat-9")], []),
        patient_names={"someone-else": "Not Them"},
    )

    assert vm.appointments[0].patient_display == "pat-9"


def test_appointment_row_says_unnamed_when_there_is_no_patient_id_at_all() -> None:
    # An empty cell reads as a rendering fault; this reads as missing data.
    vm = build_schedule_view_model(
        _view([_appointment("a-1", "09:00", patient_id="")], []),
    )

    assert vm.appointments[0].patient_display == UNNAMED_PATIENT


def test_patient_names_are_optional_so_the_builder_stays_callable_without_stores() -> None:
    # The builder is pure by contract. Omitting the map must not raise.
    vm = build_schedule_view_model(
        _view([_appointment("a-1", "09:00", patient_id="pat-9")], [])
    )

    assert vm.appointments[0].patient_display == "pat-9"


def test_a_patient_name_containing_markup_is_escaped() -> None:
    # Names come from speech transcription and from the doctor's own edits, so
    # they are caller-supplied text on a page the clinic trusts.
    vm = build_schedule_view_model(
        _view([_appointment("a-1", "09:00", patient_id="pat-9")], []),
        patient_names={"pat-9": '<script>alert("x")</script>'},
    )

    html_out = render_schedule_view(vm)
    assert "<script>" not in html_out
    assert "&lt;script&gt;" in html_out


def test_from_result_passes_patient_names_through() -> None:
    # The route builds from a StoreResult, so the map has to survive that path —
    # otherwise the fix works in the builder and not on the page.
    vm = build_schedule_view_model_from_result(
        Ok(_view([_appointment("a-1", "09:00", patient_id="pat-9")], [])),
        provider_id=PROVIDER,
        day=DAY,
        patient_names={"pat-9": "Sailaja Devi"},
    )

    assert vm.appointments[0].patient_display == "Sailaja Devi"
