"""The doctor's day calendar: publish slots, and see the whole day at a glance.

Distinct from :mod:`~clinic_front_desk.dashboard.schedule_view`, which answers "what
is happening today" as a list inside the dashboard. This is the doctor's *control*
over the calendar: it shows every slot including the booked ones, and it is where
availability is created.

Showing booked slots is the point. ``list_open_slots`` deliberately hides them
because its job is to answer "what can I offer this caller", but a calendar that
hid them would show a fully-booked morning as an empty one.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from urllib.parse import quote

from clinic_front_desk.data_layer.interfaces import AppointmentStore
from clinic_front_desk.models import ISODate, Slot, SlotStatus, is_err
from clinic_front_desk.scheduling import DAY_END, DAY_START, DEFAULT_SLOT_MINUTES

#: Where the calendar and its publish action live.
SLOTS_ENDPOINT = "/slots"

#: Slot lengths offered in the portal.
SLOT_LENGTH_CHOICES = (15, 20, 30, 45, 60)


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def _clock(iso_datetime: str) -> str:
    """The ``HH:MM`` of an ISO date-time, or the raw value if it has no time."""
    _, separator, rest = iso_datetime.partition("T")
    return rest[:5] if separator else iso_datetime


#: The parts of a day slots are grouped under, as ``(heading, first, last)`` on a
#: 24-hour clock. A published day can hold 48 slots; as one flat run they are a
#: wall of times nobody scans. Grouping is how "have I got anything after lunch"
#: becomes a glance instead of a count.
DAY_PARTS: tuple[tuple[str, str, str], ...] = (
    ("Overnight", "00:00", "06:00"),
    ("Morning", "06:00", "12:00"),
    ("Afternoon", "12:00", "17:00"),
    ("Evening", "17:00", "24:00"),
)


def _page_header(view: object, active: str) -> str:
    """The bar that stops these pages feeling like orphans.

    The calendar and the patient record were reachable only by a text link at the
    very bottom, with no indication of where you were or how to get anywhere else.
    Both are pages the doctor sits on, so they get the same identity and the same
    navigation as the dashboard.
    """
    url = getattr(view, "url", None)
    role = getattr(view, "role", "") or ""
    if url is None:
        return ""

    links = (("Dashboard", "/"), ("Slots", SLOTS_ENDPOINT), ("Documents", "/documents"))
    items = "".join(
        f'<a class="clinic-header__link'
        + (" clinic-header__link--active" if label == active else "")
        + f'" href="{_esc(url(path))}">{_esc(label)}</a>'
        for label, path in links
    )
    badge = (
        f'<span class="clinic-header__role">{_esc(role)}</span>' if role else ""
    )
    return (
        '<header class="clinic-header">'
        '<span class="clinic-header__brand">'
        '<span class="clinic-header__mark">CF</span>'
        '<span class="clinic-header__name">Clinic Front Desk</span>'
        "</span>"
        f'<nav class="clinic-header__nav">{items}</nav>'
        f"{badge}"
        "</header>"
    )


@dataclass
class DaySlotCell:
    """One slot as the calendar grid shows it."""

    slot_id: str
    start_time: str
    end_time: str
    service: str
    status: str
    #: Who holds this slot, when it is booked. Shown on the cell so the doctor can
    #: read their day without opening anything.
    patient_name: str = ""
    patient_id: str = ""

    @property
    def bookable(self) -> bool:
        return self.status == SlotStatus.OPEN.value

    @property
    def blocked(self) -> bool:
        return self.status == SlotStatus.BLOCKED.value

    @property
    def booked(self) -> bool:
        return self.status == SlotStatus.BOOKED.value


@dataclass
class DayScheduleViewModel:
    """Everything the day-calendar page needs.

    Attributes:
        day: The ISO date shown.
        provider_id: The provider whose calendar it is.
        providers: Configured provider ids, for the picker.
        services: Configured service names, for the publish form.
        cells: The day's slots, earliest-first.
        message / error: Outcome of a publish action.
        store_error: Set when the calendar read itself failed.
        role: Carried into every link and form so actions keep their role.
    """

    day: ISODate
    provider_id: str = ""
    providers: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    cells: list[DaySlotCell] = field(default_factory=list)
    message: str | None = None
    error: str | None = None
    store_error: str | None = None
    role: str | None = None

    @property
    def published(self) -> bool:
        return bool(self.cells)

    @property
    def open_count(self) -> int:
        return sum(1 for cell in self.cells if cell.bookable)

    @property
    def booked_count(self) -> int:
        return sum(1 for cell in self.cells if cell.booked)

    @property
    def blocked_count(self) -> int:
        return sum(1 for cell in self.cells if cell.blocked)

    @property
    def configured(self) -> bool:
        """Whether the clinic has a provider and a service to publish against."""
        return bool(self.providers and self.services)

    def url(self, path: str, **params: str) -> str:
        """``path`` with the role and any parameters preserved."""
        query = {**params}
        if self.role:
            query["role"] = self.role
        if not query:
            return path
        encoded = "&".join(
            f"{key}={quote(value, safe='')}" for key, value in query.items()
        )
        return f"{path}?{encoded}"


def _cell(
    slot: Slot, holders: dict[str, tuple[str, str]] | None = None
) -> DaySlotCell:
    name, patient_id = (holders or {}).get(slot.id, ("", ""))
    return DaySlotCell(
        slot_id=slot.id,
        start_time=_clock(slot.start),
        end_time=_clock(slot.end),
        service=slot.service,
        status=slot.status.value if hasattr(slot.status, "value") else str(slot.status),
        patient_name=name,
        patient_id=patient_id,
    )


def build_day_schedule_view_model(
    store: AppointmentStore,
    *,
    day: ISODate,
    provider_id: str,
    providers: list[str],
    services: list[str],
    message: str | None = None,
    error: str | None = None,
    role: str | None = None,
    holders: dict[str, tuple[str, str]] | None = None,
) -> DayScheduleViewModel:
    """Build the day-calendar view-model, reading the provider's whole day.

    Args:
        holders: ``slot_id -> (patient name, patient id)`` for booked slots, so a
            booked cell says *who* holds it. Supplied by the caller because
            resolving names needs the appointment and patient stores, which this
            pure builder does not take.
    """
    view = DayScheduleViewModel(
        day=day,
        provider_id=provider_id,
        providers=providers,
        services=services,
        message=message,
        error=error,
        role=role,
    )
    if not provider_id:
        # No provider configured yet: render the page with its explanation rather
        # than an empty grid that looks like a fully-booked day.
        return view

    result = store.list_slots_for_day(provider_id, day)
    if is_err(result):
        view.store_error = result.error.detail
        return view
    view.cells = [_cell(slot, holders) for slot in result.value]
    return view


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_publish_form(view: DayScheduleViewModel) -> str:
    services = "".join(
        f'<option value="{_esc(name)}">{_esc(name)}</option>' for name in view.services
    )
    lengths = "".join(
        f'<option value="{minutes}"'
        + (" selected" if minutes == DEFAULT_SLOT_MINUTES else "")
        + f">{minutes} minutes</option>"
        for minutes in SLOT_LENGTH_CHOICES
    )
    return (
        f'<form class="day-schedule__publish" method="post" '
        f'action="{_esc(view.url(SLOTS_ENDPOINT))}">'
        f'<input type="hidden" name="day" value="{_esc(view.day)}">'
        f'<input type="hidden" name="provider_id" value="{_esc(view.provider_id)}">'
        f'<label>Service<select name="service" required>{services}</select></label>'
        f'<label>Slot length<select name="minutes">{lengths}</select></label>'
        f'<label>From<input type="time" name="start" value="{DAY_START}"></label>'
        # Left empty on purpose: an <input type="time"> cannot hold "24:00", so
        # there is no way to express "to the end of the day" in the field itself.
        # Empty means midnight, which is what publishes a full 24 hours.
        '<label>To<input type="time" name="end" '
        'aria-describedby="end-hint"></label>'
        f'<label>Repeat until<input type="date" name="until" '
        f'min="{_esc(view.day)}"></label>'
        '<label class="day-schedule__check">'
        '<input type="checkbox" name="skip_closed" value="1" checked>'
        "Skip days the clinic is closed</label>"
        '<button type="submit" class="day-schedule__publish-submit">'
        "Publish</button>"
        '<p class="day-schedule__hint" id="end-hint">Leave <em>To</em> empty to '
        "publish through to midnight, and <em>Repeat until</em> empty for this day "
        "only. Publishing again is safe: it fills any gaps and leaves booked or "
        "blocked slots alone.</p>"
        "</form>"
    )


def _render_cell(
    view: DayScheduleViewModel, cell: DaySlotCell, *, show_service: bool = True
) -> str:
    """One slot, with a block/reopen control unless a patient holds it.

    ``show_service`` is off when the whole day is one service, which is the normal
    case — printing "ENT Consultation" forty-seven times says nothing and buries
    the times and the one name that matter.
    """
    label = "Open" if cell.bookable else cell.status.replace("_", " ").title()

    if cell.booked:
        # No block control at all: freeing this time means cancelling the
        # patient's appointment, which is a separate and deliberate act. Instead
        # the cell links to who holds it.
        # Cancelling is offered here and blocking still is not. Blocking would hide
        # the time while leaving the patient expecting it; cancelling ends the
        # appointment and texts them. Guarded by a confirm(), because it cannot be
        # undone and it sends a message to a real person.
        cancel = (
            f'<form method="post" action="{_esc(view.url(SLOTS_ENDPOINT + "/cancel"))}" '
            'onsubmit="return confirm(\'Cancel this appointment and text the '
            'patient? This cannot be undone.\')">'
            f'<input type="hidden" name="day" value="{_esc(view.day)}">'
            f'<input type="hidden" name="provider_id" value="{_esc(view.provider_id)}">'
            f'<input type="hidden" name="slot_id" value="{_esc(cell.slot_id)}">'
            '<button type="submit" class="day-schedule__cell-action">'
            "Cancel &amp; notify</button>"
            "</form>"
        )
        if cell.patient_id:
            action = (
                f'<a class="day-schedule__cell-action" '
                f'href="{_esc(view.url(SLOTS_ENDPOINT + "/patient/" + quote(cell.patient_id, safe=""), day=view.day, provider_id=view.provider_id))}">'
                "View patient</a>" + cancel
            )
        else:
            action = (
                '<span class="day-schedule__cell-locked">booked</span>' + cancel
            )
    else:
        blocking = not cell.blocked
        action = (
            f'<form method="post" action="{_esc(view.url(SLOTS_ENDPOINT + "/block"))}">'
            f'<input type="hidden" name="day" value="{_esc(view.day)}">'
            f'<input type="hidden" name="provider_id" value="{_esc(view.provider_id)}">'
            f'<input type="hidden" name="slot_id" value="{_esc(cell.slot_id)}">'
            f'<input type="hidden" name="blocked" value="{"1" if blocking else "0"}">'
            '<button type="submit" class="day-schedule__cell-action">'
            f'{"Block" if blocking else "Reopen"}</button>'
            "</form>"
        )

    if cell.patient_name:
        subtitle = (
            f'<span class="day-schedule__cell-patient">{_esc(cell.patient_name)}</span>'
        )
    elif show_service:
        subtitle = (
            f'<span class="day-schedule__cell-service">{_esc(cell.service)}</span>'
        )
    else:
        subtitle = ""

    return (
        f'<li class="day-schedule__cell" data-status="{_esc(cell.status)}" '
        f'data-slot-id="{_esc(cell.slot_id)}">'
        f'<span class="day-schedule__cell-time">{_esc(cell.start_time)}</span>'
        f'<span class="day-schedule__cell-status">{_esc(label)}</span>'
        f"{subtitle}{action}"
        "</li>"
    )


def _render_grid(view: DayScheduleViewModel) -> str:
    """The day's slots, grouped by part of day, with the redundancy removed."""
    distinct_services = {cell.service for cell in view.cells}
    show_service = len(distinct_services) > 1

    sections: list[str] = []
    placed = 0
    for heading, first, last in DAY_PARTS:
        part = [cell for cell in view.cells if first <= cell.start_time < last]
        if not part:
            continue
        placed += len(part)
        open_here = sum(1 for cell in part if cell.bookable)
        cells = "".join(
            _render_cell(view, cell, show_service=show_service) for cell in part
        )
        sections.append(
            '<section class="day-schedule__part">'
            f'<h2 class="day-schedule__part-title">{_esc(heading)}'
            f'<span class="day-schedule__part-count">{open_here} open</span></h2>'
            f'<ul class="day-schedule__grid">{cells}</ul>'
            "</section>"
        )

    # Any slot whose time did not fall in a named part still has to appear. A slot
    # the doctor cannot see is worse than an ugly heading.
    if placed < len(view.cells):
        named = {
            cell.slot_id
            for _, first, last in DAY_PARTS
            for cell in view.cells
            if first <= cell.start_time < last
        }
        rest = [cell for cell in view.cells if cell.slot_id not in named]
        cells = "".join(
            _render_cell(view, cell, show_service=show_service) for cell in rest
        )
        sections.append(
            '<section class="day-schedule__part">'
            '<h2 class="day-schedule__part-title">Other times</h2>'
            f'<ul class="day-schedule__grid">{cells}</ul>'
            "</section>"
        )

    if not show_service and distinct_services:
        sections.insert(
            0,
            '<p class="day-schedule__hint">Every slot below is published as '
            f"{_esc(next(iter(distinct_services)))}. A caller is offered this time "
            "for whichever service they ask for.</p>",
        )
    return "".join(sections)


def _render_block_form(view: DayScheduleViewModel) -> str:
    """Block or reopen a whole range at once.

    The per-slot buttons are fine for a stray half hour, but blocking a lunch break
    out of a 48-slot day one button at a time is not something anyone would do
    twice.
    """
    return (
        f'<form class="day-schedule__block" method="post" '
        f'action="{_esc(view.url(SLOTS_ENDPOINT + "/block"))}">'
        '<span class="day-schedule__block-title">Block a time range</span>'
        f'<input type="hidden" name="day" value="{_esc(view.day)}">'
        f'<input type="hidden" name="provider_id" value="{_esc(view.provider_id)}">'
        '<label>From<input type="time" name="start" required></label>'
        '<label>To<input type="time" name="end" required></label>'
        '<button type="submit" name="blocked" value="1" '
        'class="day-schedule__block-submit">Block</button>'
        '<button type="submit" name="blocked" value="0" '
        'class="day-schedule__block-submit day-schedule__block-submit--undo">'
        "Reopen</button>"
        '<p class="day-schedule__hint">Blocked time is not offered to callers. '
        "Booked slots are never changed \u2014 cancel the appointment first.</p>"
        "</form>"
    )


def render_day_schedule(view: DayScheduleViewModel) -> str:
    """Render the day-calendar body."""
    parts = [
        _page_header(view, "Slots"),
        '<h1 class="day-schedule__title">Appointment slots</h1>',
    ]

    if view.message:
        parts.append(f'<p class="wizard-success" role="status">{_esc(view.message)}</p>')
    if view.error:
        parts.append(f'<p class="wizard-error" role="alert">{_esc(view.error)}</p>')

    if not view.configured:
        parts.append(
            '<div class="day-schedule__empty" role="status">'
            "<p>Slots need a provider and at least one service before they can be "
            "published.</p>"
            f'<p><a href="/onboarding">Complete the clinic setup form</a> first.</p>'
            "</div>"
        )
        return "\n".join(parts)

    # Day navigation. Plain links, so it works without scripting.
    from datetime import UTC, datetime, timedelta

    try:
        current = datetime.strptime(view.day, "%Y-%m-%d").replace(tzinfo=UTC)
        previous_day = (current - timedelta(days=1)).strftime("%Y-%m-%d")
        next_day = (current + timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        previous_day = next_day = view.day

    provider_options = "".join(
        f'<option value="{_esc(pid)}"'
        + (" selected" if pid == view.provider_id else "")
        + f">{_esc(pid)}</option>"
        for pid in view.providers
    )
    parts.append(
        '<div class="day-schedule__bar">'
        f'<a class="day-schedule__nav" '
        f'href="{_esc(view.url(SLOTS_ENDPOINT, day=previous_day, provider_id=view.provider_id))}">'
        "\u2190 Previous day</a>"
        f'<form class="day-schedule__picker" method="get" action="{SLOTS_ENDPOINT}">'
        + (
            f'<input type="hidden" name="role" value="{_esc(view.role)}">'
            if view.role
            else ""
        )
        + f'<label>Day<input type="date" name="day" value="{_esc(view.day)}"></label>'
        f'<label>Provider<select name="provider_id">{provider_options}</select></label>'
        '<button type="submit">Show</button>'
        "</form>"
        f'<a class="day-schedule__nav" '
        f'href="{_esc(view.url(SLOTS_ENDPOINT, day=next_day, provider_id=view.provider_id))}">'
        "Next day \u2192</a>"
        "</div>"
    )

    # Both forms fold away. They are setup actions taken occasionally, and having
    # them stacked above the calendar meant the doctor scrolled past two forms
    # every time they wanted to answer "what does my day look like".
    parts.append(
        '<details class="day-schedule__tools">'
        "<summary>Publish slots</summary>"
        f"{_render_publish_form(view)}"
        "</details>"
    )

    if view.store_error:
        parts.append(
            '<p class="wizard-error" role="alert">'
            f"Could not read the calendar: {_esc(view.store_error)}</p>"
        )
    elif not view.published:
        parts.append(
            '<div class="day-schedule__empty" role="status">'
            "<p>No slots published for this day, so the agent has nothing to offer "
            "a caller.</p><p>Publish the day above to open it for booking.</p>"
            "</div>"
        )
    else:
        parts.append(
            '<ul class="day-schedule__summary">'
            f'<li class="day-schedule__stat day-schedule__stat--open">'
            f'<strong>{view.open_count}</strong><span>open</span></li>'
            f'<li class="day-schedule__stat day-schedule__stat--booked">'
            f'<strong>{view.booked_count}</strong><span>booked</span></li>'
            f'<li class="day-schedule__stat day-schedule__stat--blocked">'
            f'<strong>{view.blocked_count}</strong><span>blocked</span></li>'
            f'<li class="day-schedule__stat">'
            f"<strong>{len(view.cells)}</strong><span>total</span></li>"
            "</ul>"
        )
        parts.append(
            '<details class="day-schedule__tools">'
            "<summary>Block a time range</summary>"
            f"{_render_block_form(view)}"
            "</details>"
        )
        parts.append(_render_grid(view))

    return "\n".join(parts)


def render_day_schedule_page(view: DayScheduleViewModel) -> str:
    """Render the day calendar as a complete HTML document."""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "  <head>\n"
        '    <meta charset="utf-8" />\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        '    <meta name="color-scheme" content="light dark" />\n'
        "    <title>Appointment slots</title>\n"
        "  </head>\n"
        "  <body>\n"
        '    <main class="day-schedule">\n'
        f"      {render_day_schedule(view)}\n"
        "    </main>\n"
        "  </body>\n"
        "</html>\n"
    )


__all__ = [
    "SLOTS_ENDPOINT",
    "SLOT_LENGTH_CHOICES",
    "DaySlotCell",
    "DayScheduleViewModel",
    "build_day_schedule_view_model",
    "render_day_schedule",
    "render_day_schedule_page",
    "PatientDetailViewModel",
    "render_patient_detail",
    "render_patient_detail_page",
]


# ---------------------------------------------------------------------------
# Patient detail: what the doctor sees on clicking a booked slot
# ---------------------------------------------------------------------------


@dataclass
class PatientDetailViewModel:
    """One patient's record and their appointments at this clinic.

    Health details are shown here and nowhere else. They are not on the calendar
    grid, not in the call activity log, and never spoken by the agent — a doctor
    asking for them is a deliberate act, which is the point at which showing them
    is appropriate.
    """

    patient_id: str
    name: str = ""
    callback_phone: str = ""
    #: The short code the caller was given on the phone, e.g. ``SA901``.
    code: str = ""
    age: int | None = None
    blood_group: str | None = None
    weight_kg: float | None = None
    height_cm: float | None = None
    created_at: str = ""
    appointments: list[tuple[str, str, str]] = field(default_factory=list)
    back_day: str = ""
    back_provider: str = ""
    message: str | None = None
    error: str | None = None
    role: str | None = None
    #: Blood groups the correction form offers. Passed in rather than imported so
    #: this component stays free of the tool layer; a closed list also means the
    #: doctor cannot reintroduce the free-text spellings the phone path avoids.
    blood_group_choices: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.name)

    @property
    def has_health_details(self) -> bool:
        return any(
            value is not None
            for value in (self.age, self.blood_group, self.weight_kg, self.height_cm)
        )

    @property
    def bmi(self) -> str:
        """Body-mass index, or an empty string when it cannot be computed.

        Shown as a plain number with no interpretation attached. Deriving it saves
        the doctor arithmetic; saying what it *means* would be a clinical judgment,
        which is theirs to make and not this system's to offer.
        """
        if self.weight_kg is None or self.height_cm is None or self.height_cm <= 0:
            return ""
        metres = self.height_cm / 100
        return f"{self.weight_kg / (metres * metres):.1f}"

    def url(self, path: str, **params: str) -> str:
        query = {**params}
        if self.role:
            query["role"] = self.role
        if not query:
            return path
        encoded = "&".join(
            f"{key}={quote(value, safe='')}" for key, value in query.items()
        )
        return f"{path}?{encoded}"


def _row(label: str, value: str) -> str:
    return (
        '<div class="patient-detail__row">'
        f'<dt class="patient-detail__label">{_esc(label)}</dt>'
        f'<dd class="patient-detail__value">{_esc(value)}</dd>'
        "</div>"
    )


def _number_field(
    label: str, name: str, value: object, *, step: str = "1", hint: str = ""
) -> str:
    """One optional numeric input, empty when the record has no value."""
    shown = "" if value is None else f"{value:g}" if isinstance(value, float) else str(value)
    return (
        '<label class="patient-detail__field">'
        f"<span>{_esc(label)}</span>"
        f'<input type="number" name="{_esc(name)}" step="{_esc(step)}" '
        f'inputmode="decimal" value="{_esc(shown)}">'
        + (f'<small class="patient-detail__hint">{_esc(hint)}</small>' if hint else "")
        + "</label>"
    )


def _render_patient_edit_form(view: PatientDetailViewModel) -> str:
    """The correction form.

    Speech recognition mishears names — proper nouns have no dictionary to fall
    back on, and one misheard vowel is enough. Observed live: a caller who said
    "Sidda Deepika" was recorded as "siddha devika", and until this form existed
    there was no way to put it right. The record was wrong permanently, and the
    name on the calendar would never match the ID she brings to reception.

    Pre-filled with what is stored, so it reads as a correction rather than a
    fresh entry. **A cleared field clears the record**, which is what a form that
    shows you its current contents ought to mean — the doctor deleting a wrong
    weight is asking for it to be removed, not ignored.
    """
    action = view.url(
        f"{SLOTS_ENDPOINT}/patient/{quote(view.patient_id, safe='')}",
        day=view.back_day,
        provider_id=view.back_provider,
    )
    options = ['<option value="">Not given</option>']
    for group in view.blood_group_choices:
        selected = " selected" if group == (view.blood_group or "") else ""
        options.append(f'<option value="{_esc(group)}"{selected}>{_esc(group)}</option>')

    return (
        f'<form class="patient-detail__form" method="post" action="{_esc(action)}">'
        '<p class="patient-detail__hint">Names are the least reliable thing a '
        "phone line captures. Fix anything that was misheard \u2014 the calendar "
        "and every appointment update with it.</p>"
        '<label class="patient-detail__field"><span>Name</span>'
        f'<input type="text" name="name" required value="{_esc(view.name)}"></label>'
        '<label class="patient-detail__field"><span>Mobile</span>'
        f'<input type="tel" name="callback_phone" required '
        f'value="{_esc(view.callback_phone)}"></label>'
        + _number_field("Age", "age", view.age)
        + '<label class="patient-detail__field"><span>Blood group</span>'
        f'<select name="blood_group">{"".join(options)}</select></label>'
        + _number_field("Weight (kg)", "weight_kg", view.weight_kg, step="0.1")
        + _number_field("Height (cm)", "height_cm", view.height_cm, step="0.1")
        + '<button type="submit" class="patient-detail__save">Save corrections</button>'
        '<p class="patient-detail__hint">Clearing a box removes that detail from '
        "the record.</p>"
        "</form>"
    )


def render_patient_detail(view: PatientDetailViewModel) -> str:
    """Render one patient's details for the doctor."""
    parts = [
        _page_header(view, "Slots"),
        f'<h1 class="patient-detail__title">{_esc(view.name or "Patient")}</h1>',
    ]

    if view.message:
        parts.append(f'<p class="wizard-success" role="status">{_esc(view.message)}</p>')
    if view.error:
        parts.append(f'<p class="wizard-error" role="alert">{_esc(view.error)}</p>')
    if not view.found:
        parts.append(
            '<div class="day-schedule__empty" role="status">'
            "<p>No record found for this patient.</p></div>"
        )
    else:
        rows = [_row("Name", view.name), _row("Mobile", view.callback_phone)]
        if view.code:
            # The code the caller was read out on the phone. Shown so reception can
            # match what a patient quotes at the desk.
            rows.append(_row("Patient code", view.code))
        if view.created_at:
            rows.append(_row("First seen", view.created_at[:10]))
        # An initial rather than a photo. The page was a bare definition list with
        # nothing to anchor it, and a record about a person should look like one.
        initial = next((ch for ch in view.name if ch.isalnum()), "?").upper()
        parts.append(
            '<section class="patient-detail__identity">'
            f'<span class="patient-detail__avatar" aria-hidden="true">'
            f"{_esc(initial)}</span>"
            f'<dl class="patient-detail__list">{"".join(rows)}</dl>'
            "</section>"
        )

        parts.append('<h2 class="patient-detail__subtitle">Intake details</h2>')
        if view.has_health_details:
            health = []
            if view.age is not None:
                health.append(_row("Age", f"{view.age}"))
            if view.blood_group:
                health.append(_row("Blood group", view.blood_group))
            if view.weight_kg is not None:
                health.append(_row("Weight", f"{view.weight_kg:g} kg"))
            if view.height_cm is not None:
                health.append(_row("Height", f"{view.height_cm:g} cm"))
            if view.bmi:
                health.append(_row("BMI", view.bmi))
            parts.append(
                f'<dl class="patient-detail__list patient-detail__list--health">'
                f'{"".join(health)}</dl>'
            )
            # Said plainly, because a value collected over the phone is what the
            # caller stated, not something the clinic measured.
            parts.append(
                '<p class="patient-detail__provenance">As given by the caller when '
                "booking. Not measured at the clinic.</p>"
            )
        else:
            parts.append(
                '<p class="patient-detail__none">The caller did not give any of '
                "these. They are optional, so a booking is never held up for them."
                "</p>"
            )

        parts.append('<h2 class="patient-detail__subtitle">Appointments this day</h2>')
        if view.appointments:
            items = "".join(
                f'<li class="patient-detail__appointment">'
                f"<strong>{_esc(date)}</strong> {_esc(time)} \u00b7 {_esc(service)}"
                "</li>"
                for date, time, service in view.appointments
            )
            parts.append(f'<ul class="patient-detail__appointments">{items}</ul>')
        else:
            parts.append(
                '<p class="patient-detail__none">No appointments on record.</p>'
            )

        # Folded away by default. Correcting a record is the exception, not what
        # the doctor opened this page to do — but it opens itself when the last
        # attempt failed, so an error is never reported above a hidden form.
        opened = " open" if view.error else ""
        parts.append(
            f'<details class="patient-detail__editor"{opened}>'
            "<summary>Correct this record</summary>"
            f"{_render_patient_edit_form(view)}"
            "</details>"
        )

    back = view.url(
        SLOTS_ENDPOINT, day=view.back_day, provider_id=view.back_provider
    ) if view.back_day else view.url(SLOTS_ENDPOINT)
    parts.append(
        f'<p class="day-schedule__links"><a href="{_esc(back)}">'
        "\u2190 Back to the day</a></p>"
    )
    return "\n".join(parts)


def render_patient_detail_page(view: PatientDetailViewModel) -> str:
    """Render the patient detail as a complete HTML document."""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "  <head>\n"
        '    <meta charset="utf-8" />\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        '    <meta name="color-scheme" content="light dark" />\n'
        # Health details must not follow the doctor into a browser cache or a
        # shared history entry on a clinic machine.
        '    <meta name="robots" content="noindex, nofollow" />\n'
        "    <title>Patient</title>\n"
        "  </head>\n"
        "  <body>\n"
        '    <main class="patient-detail">\n'
        f"      {render_patient_detail(view)}\n"
        "    </main>\n"
        "  </body>\n"
        "</html>\n"
    )
