"""``ScheduleView`` component view-model + renderer (task 13.2, Req 15.1, 15.4, 15.6).

The dashboard's schedule view shows one Provider's booked Appointments and open
Slots for a single day — the current day by default (Req 15.1) — and reflects
booking/reschedule/cancel changes in real time (Req 15.4) and a doctor-selected
non-current day quickly (Req 15.6). This module is the **pure Python view-model
and server-side renderer** for that view. It takes the
:class:`~clinic_front_desk.dashboard.bff.ScheduleView` the BFF read through the
Data_Layer (see
:meth:`~clinic_front_desk.dashboard.bff.DashboardBFF.schedule_for_day`) and
shapes it into a render-ready view-model, then fills the
``web/schedule_view.html`` partial.

Design alignment
----------------
Same three-part pattern as the other dashboard components (view-model builder +
HTML partial + a thin vanilla-JS controller):

- **Builder** (:func:`build_schedule_view_model`) is pure and deterministic:
  given the same inputs it returns the same view-model, with no I/O, no clock
  read, and no mutation of the inputs. It orders Appointments and open Slots by
  start time so the day reads top-to-bottom.
- **Renderer** (:func:`render_schedule_view`) turns the view-model into an HTML
  string by filling the partial; all interpolated text (patient identifiers,
  service names) is HTML-escaped so caller-supplied data cannot inject markup.
- The JS controller (``web/schedule_view.js``) subscribes to the change channel
  and re-fetches this server-rendered partial on an appointment/slot
  ``ChangeEvent`` (Req 15.4) and on a day change (Req 15.6); rendering stays a
  single source of truth in Python.

Day handling
------------
The view-model renders whatever ``day`` the BFF was asked for. "Current day by
default" (Req 15.1) and "select another day" (Req 15.6) are the caller's/
client's concern: the server renders the requested day, and the JS controller
defaults the day picker to today and re-fetches when the doctor picks another.
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from clinic_front_desk.dashboard.bff import ScheduleView
from clinic_front_desk.models import (
    Appointment,
    AppointmentStatus,
    ISODate,
    Slot,
    StoreResult,
    is_err,
)

# Directory holding the HTML partials this module renders (sibling ``web/``).
_WEB_DIR = Path(__file__).resolve().parent / "web"

#: Default endpoint the client JS re-fetches the partial from (per-provider,
#: per-day). Callers may override when a deployment mounts the dashboard under a
#: different path.
DEFAULT_SCHEDULE_ENDPOINT = "/dashboard/schedule"

#: Shown when the selected day has no Appointments and no open Slots (Req 15.1).
SCHEDULE_EMPTY_MESSAGE = "No appointments or open slots for this day."

#: Shown when the schedule read itself failed, so the view renders a recoverable
#: error rather than a misleading empty day.
SCHEDULE_ERROR_MESSAGE = "Schedule could not be loaded. Please retry."

#: Human-readable labels for each appointment status (Req 15.1).
_STATUS_LABELS: dict[AppointmentStatus, str] = {
    AppointmentStatus.BOOKED: "Booked",
    AppointmentStatus.RESCHEDULED: "Rescheduled",
    AppointmentStatus.CANCELLED: "Cancelled",
    AppointmentStatus.COMPLETED: "Completed",
    AppointmentStatus.NO_SHOW: "No-show",
}


@lru_cache(maxsize=None)
def _load_template(name: str) -> str:
    """Read and cache an HTML partial from the ``web/`` directory."""
    return (_WEB_DIR / name).read_text(encoding="utf-8")


def _status_label(status: AppointmentStatus) -> str:
    """Return the display label for an appointment status."""
    return _STATUS_LABELS.get(status, str(status))


def _time_of(iso_datetime: str) -> str:
    """Extract the ``HH:MM`` portion of an ISO-8601 date-time.

    Slot ``start``/``end`` are ``YYYY-MM-DDTHH:MM[:SS][Z]``; the time is the
    ``HH:MM`` after the ``T``. Falls back to the raw string when there is no
    recognizable time component so display never crashes on odd input.
    """
    _, sep, rest = iso_datetime.partition("T")
    if not sep:
        return iso_datetime
    return rest[:5]


def _appointment_time(appointment: Appointment) -> str:
    """Return an ``HH:MM`` display time for an appointment.

    ``Appointment.time`` is a clock time (e.g. ``"14:30"`` or ``"14:30:00"``);
    trim to ``HH:MM`` for display, leaving anything else untouched.
    """
    return appointment.time[:5] if appointment.time else appointment.time


@dataclass(frozen=True)
class AppointmentRowViewModel:
    """One render-ready booked-appointment row (Req 15.1).

    Attributes:
        appointment_id: The appointment id (stable key for reconciliation).
        time: The appointment clock time, trimmed to ``HH:MM``.
        service: The booked service name.
        patient_id: The associated patient identifier, kept for linking and
            reconciliation rather than for display.
        patient_display: Who the row is shown as: the patient's name when the
            caller supplied one, else the id, else a placeholder. A doctor reading
            her day needs a name — a row saying
            ``c6026264f5b44f9b92e32349333e235d`` tells her nothing about who is
            arriving at nine.
        status: The raw appointment status value.
        status_label: Human-readable label for ``status``.
    """

    appointment_id: str
    time: str
    service: str
    patient_id: str
    status: str
    status_label: str
    patient_display: str = ""


@dataclass(frozen=True)
class OpenSlotRowViewModel:
    """One render-ready open-slot row (Req 15.1).

    Attributes:
        slot_id: The slot id (stable key for reconciliation).
        service: The service the slot is bookable for.
        start_time: The slot start, trimmed to ``HH:MM``.
        end_time: The slot end, trimmed to ``HH:MM``.
        start_iso: The full ISO-8601 start (for ordering / ``datetime`` attr).
    """

    slot_id: str
    service: str
    start_time: str
    end_time: str
    start_iso: str


@dataclass(frozen=True)
class ScheduleViewModel:
    """The schedule view's view-model for one provider on one day (Req 15.1).

    Attributes:
        provider_id: The provider whose calendar this is.
        day: The ISO date the view covers.
        appointments: Booked appointments, ordered earliest-first.
        open_slots: Open slots, ordered earliest-first.
        is_empty: ``True`` when there are no appointments and no open slots.
        empty_message: Message rendered when ``is_empty`` is ``True``.
        has_error: ``True`` when the schedule read failed; the client renders
            ``empty_message`` as a recoverable error.
    """

    provider_id: str
    day: str
    appointments: list[AppointmentRowViewModel] = field(default_factory=list)
    open_slots: list[OpenSlotRowViewModel] = field(default_factory=list)
    is_empty: bool = True
    empty_message: str = SCHEDULE_EMPTY_MESSAGE
    has_error: bool = False


#: Shown when an appointment has no patient id and no resolvable name. Better than
#: an empty cell, which reads as a rendering fault rather than missing data.
UNNAMED_PATIENT = "Unnamed patient"


def _appointment_row(
    appointment: Appointment,
    patient_names: Mapping[str, str] | None = None,
) -> AppointmentRowViewModel:
    """Shape a single :class:`Appointment` into a row view-model.

    ``patient_names`` maps patient id to name. Supplied by the caller rather than
    looked up here, because resolving a name needs the patient store and this
    builder is pure — the same reason
    :func:`~clinic_front_desk.dashboard.components.day_schedule.build_day_schedule_view_model`
    takes its ``holders`` argument. Names are resolved per render rather than
    copied onto the appointment, so a patient who corrects their name is not left
    with the old one printed across their bookings.
    """
    patient_id = appointment.patient_id
    name = (patient_names or {}).get(patient_id, "")
    return AppointmentRowViewModel(
        appointment_id=appointment.id,
        time=_appointment_time(appointment),
        service=appointment.service,
        patient_id=patient_id,
        status=str(appointment.status),
        status_label=_status_label(AppointmentStatus(appointment.status)),
        # Fall back to the id rather than to nothing: an unresolved name still has
        # to identify the row well enough to be matched against the patient list.
        patient_display=name or patient_id or UNNAMED_PATIENT,
    )


def _open_slot_row(slot: Slot) -> OpenSlotRowViewModel:
    """Shape a single open :class:`Slot` into a row view-model."""
    return OpenSlotRowViewModel(
        slot_id=slot.id,
        service=slot.service,
        start_time=_time_of(slot.start),
        end_time=_time_of(slot.end),
        start_iso=slot.start,
    )


def build_schedule_view_model(
    view: ScheduleView,
    *,
    patient_names: Mapping[str, str] | None = None,
) -> ScheduleViewModel:
    """Build the schedule view-model from a BFF :class:`ScheduleView` (Req 15.1).

    Orders appointments by their clock time and open slots by their ISO start so
    the day reads earliest-first, and sets the empty state when the day has
    neither appointments nor open slots. Pure and deterministic: no I/O, no clock
    read, no input mutation.

    Args:
        view: The :class:`ScheduleView` from ``DashboardBFF.schedule_for_day``.
        patient_names: Optional ``patient id -> name`` map so each row names the
            person rather than printing their id. Omitted, rows fall back to the
            id, which keeps this callable with no stores to hand.

    Returns:
        A :class:`ScheduleViewModel` ready for the HTML partial to render.
    """
    appointments = [
        _appointment_row(a, patient_names)
        for a in sorted(view.appointments, key=lambda a: a.time)
    ]
    open_slots = [
        _open_slot_row(s)
        for s in sorted(view.open_slots, key=lambda s: s.start)
    ]
    return ScheduleViewModel(
        provider_id=view.provider_id,
        day=view.day,
        appointments=appointments,
        open_slots=open_slots,
        is_empty=not appointments and not open_slots,
        empty_message=SCHEDULE_EMPTY_MESSAGE,
        has_error=False,
    )


def build_schedule_view_model_from_result(
    result: StoreResult[ScheduleView],
    *,
    provider_id: str = "",
    day: ISODate = "",
    patient_names: Mapping[str, str] | None = None,
) -> ScheduleViewModel:
    """Build the view-model from the BFF's ``schedule_for_day()`` result.

    Wraps :func:`build_schedule_view_model` and maps a read failure to an error
    view (``has_error=True``) so the client renders a recoverable error instead
    of a misleading empty day. ``provider_id`` and ``day`` are echoed into the
    error view so the client can keep its day picker / refetch wiring intact.

    Args:
        result: The :class:`StoreResult` from ``DashboardBFF.schedule_for_day``.
        provider_id: Provider id to carry into an error view.
        day: Day to carry into an error view.

    Returns:
        The schedule view-model, or an error view when the read failed.
    """
    if is_err(result):
        return ScheduleViewModel(
            provider_id=provider_id,
            day=day,
            appointments=[],
            open_slots=[],
            is_empty=True,
            empty_message=SCHEDULE_ERROR_MESSAGE,
            has_error=True,
        )
    return build_schedule_view_model(result.value, patient_names=patient_names)


def _render_appointment_row(row: AppointmentRowViewModel) -> str:
    """Render one appointment ``<li>`` with all interpolated text escaped."""
    return (
        '<li class="schedule-view__appointment" '
        f'data-appointment-id="{html.escape(row.appointment_id, quote=True)}" '
        f'data-status="{html.escape(row.status, quote=True)}">'
        f'<span class="schedule-view__time">{html.escape(row.time)}</span>'
        f'<span class="schedule-view__service">{html.escape(row.service)}</span>'
        f'<span class="schedule-view__patient" '
        f'data-patient-id="{html.escape(row.patient_id, quote=True)}">'
        f"{html.escape(row.patient_display or row.patient_id)}</span>"
        f'<span class="schedule-view__status">{html.escape(row.status_label)}</span>'
        "</li>"
    )


def _render_open_slot_row(row: OpenSlotRowViewModel) -> str:
    """Render one open-slot ``<li>`` with all interpolated text escaped."""
    return (
        '<li class="schedule-view__slot" '
        f'data-slot-id="{html.escape(row.slot_id, quote=True)}">'
        f'<time class="schedule-view__time" '
        f'datetime="{html.escape(row.start_iso, quote=True)}">'
        f"{html.escape(row.start_time)}\u2013{html.escape(row.end_time)}</time>"
        f'<span class="schedule-view__service">{html.escape(row.service)}</span>'
        "</li>"
    )


def render_schedule_view(
    view: ScheduleView | ScheduleViewModel,
    *,
    schedule_endpoint: str = DEFAULT_SCHEDULE_ENDPOINT,
) -> str:
    """Render the ``ScheduleView`` partial to an HTML string (Req 15.1, 15.4, 15.6).

    Builds the view-model (when given a BFF :class:`ScheduleView`), renders one
    row per appointment and open slot (earliest-first), and fills the
    ``web/schedule_view.html`` partial. When the day is empty an empty-state
    message is shown instead of the lists. ``schedule_endpoint``,
    ``provider_id`` and ``day`` are embedded as ``data-*`` attributes so the
    client JS can re-fetch the partial on a ChangeEvent (Req 15.4) or when the
    doctor selects another day (Req 15.6).

    Args:
        view: A BFF :class:`ScheduleView` (shaped here) or a pre-built
            :class:`ScheduleViewModel` (e.g. an error view).
        schedule_endpoint: Base URL the client JS re-fetches the partial from.

    Returns:
        The rendered HTML partial as a string.
    """
    view_model = (
        view
        if isinstance(view, ScheduleViewModel)
        else build_schedule_view_model(view)
    )

    if view_model.appointments:
        appointment_rows = "\n      ".join(
            _render_appointment_row(r) for r in view_model.appointments
        )
    else:
        appointment_rows = (
            '<li class="schedule-view__empty-row" data-role="appointments-empty">'
            "No appointments</li>"
        )

    if view_model.open_slots:
        slot_rows = "\n      ".join(
            _render_open_slot_row(r) for r in view_model.open_slots
        )
    else:
        slot_rows = (
            '<li class="schedule-view__empty-row" data-role="slots-empty">'
            "No open slots</li>"
        )

    hidden = " hidden"
    template = _load_template("schedule_view.html")
    return template.format(
        schedule_endpoint=html.escape(schedule_endpoint, quote=True),
        provider_id=html.escape(view_model.provider_id, quote=True),
        day=html.escape(view_model.day, quote=True),
        has_error="true" if view_model.has_error else "false",
        empty_message=html.escape(view_model.empty_message),
        empty_hidden="" if view_model.is_empty else hidden,
        lists_hidden=hidden if view_model.is_empty else "",
        appointment_rows=appointment_rows,
        slot_rows=slot_rows,
    )


__all__ = [
    "DEFAULT_SCHEDULE_ENDPOINT",
    "SCHEDULE_EMPTY_MESSAGE",
    "SCHEDULE_ERROR_MESSAGE",
    "AppointmentRowViewModel",
    "OpenSlotRowViewModel",
    "ScheduleViewModel",
    "build_schedule_view_model",
    "build_schedule_view_model_from_result",
    "render_schedule_view",
]
