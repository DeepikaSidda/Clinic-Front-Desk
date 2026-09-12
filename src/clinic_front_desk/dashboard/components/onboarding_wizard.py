"""``OnboardingWizard`` dashboard component (task 13.4, Req 1.1–1.6).

The onboarding wizard is the doctor's first-run experience: when the dashboard
is opened and no clinic configuration exists yet, this component presents the
onboarding form (Req 1.1) that collects clinic hours, location, offered services
(with prep instructions and price), accepted insurance, and providers
(Req 1.2, 1.3). On submit it wires straight into the clinic-config domain logic
(:func:`clinic_front_desk.config.save_clinic_config`), and on rejection it maps
each :class:`~clinic_front_desk.config.ConfigViolation` back to a per-field error
while **retaining every value the doctor already entered** (Req 1.4, 1.5, 1.6).

This is a server-rendered component: it is a *pure Python view-model builder*
plus an HTML renderer. It holds the raw strings the doctor typed (not the parsed
model), so a re-render after a rejected save shows exactly what they entered,
annotated with the specific problems. The companion static template
(``dashboard/web/onboarding_wizard.html``) provides the page shell and pulls in a
thin vanilla-JS enhancement (``dashboard/web/onboarding_wizard.js``) for adding
and removing service/provider rows; the form works without JavaScript too.

Field naming convention
------------------------
Form ``name`` attributes and error keys use the **same bracket-path notation**
as :class:`ConfigViolation.field`, so a violation maps to an input with zero
translation:

- ``location``
- ``hours[<day>].open`` / ``hours[<day>].close`` / ``hours[<day>].closed``
- ``services[<i>].name`` / ``services[<i>].prep_instructions`` / ``services[<i>].price``
- ``accepted_insurance``
- ``providers[<i>].id`` / ``providers[<i>].name`` / ``providers[<i>].specialty``
  / ``providers[<i>].days`` / ``providers[<i>].start`` / ``providers[<i>].end``

Top-level required-field violations (``hours``, ``location``, ``services``,
``providers``) surface both in a summary list (Req 1.5: "identify each missing
required field") and inline on the relevant section.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from clinic_front_desk.config import (
    ConfigSaveResult,
    save_clinic_config,
)
from clinic_front_desk.config.save import Clock
from clinic_front_desk.data_layer.interfaces import ClinicKnowledgeBaseStore
from clinic_front_desk.models import (
    ClinicKnowledgeBase,
    DayHours,
    Provider,
    ScheduleRule,
    ServiceConfig,
    is_err,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Weekday labels indexed 0 (Sunday) .. 6 (Saturday), matching the model's
#: ``ScheduleRule.day_of_week`` / ``ClinicKnowledgeBase.hours`` key convention.
WEEKDAY_NAMES: tuple[str, ...] = (
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
)
_NUM_DAYS = len(WEEKDAY_NAMES)

#: Location of the static page-shell template and JS, next to this package.
_WEB_DIR = Path(__file__).resolve().parent.parent / "web"
TEMPLATE_PATH = _WEB_DIR / "onboarding_wizard.html"

#: Token in the template that the rendered form body is substituted into.
_BODY_TOKEN = "{{WIZARD_BODY}}"

# An indexed form key such as ``services[2].price`` or ``hours[0].open``.
_INDEXED_KEY = re.compile(
    r"^(?P<coll>services|providers|hours)\[(?P<idx>\d+)\]\.(?P<attr>\w+)$"
)


# ---------------------------------------------------------------------------
# View-model row types (raw, as-typed strings — so values are retained verbatim)
# ---------------------------------------------------------------------------


@dataclass
class HoursRow:
    """One weekday's opening/closing inputs as typed."""

    day_index: int
    day_name: str
    open: str = ""
    close: str = ""
    closed: bool = False


@dataclass
class ServiceRow:
    """One offered service's inputs as typed."""

    index: int
    name: str = ""
    prep_instructions: str = ""
    price: str = ""


@dataclass
class ProviderRow:
    """One provider's inputs as typed."""

    index: int
    id: str = ""
    name: str = ""
    specialty: str = ""
    days: str = ""  # comma/space separated weekday indices
    start: str = ""
    end: str = ""


@dataclass
class OnboardingWizardViewModel:
    """Everything the template needs to render the wizard.

    Holds the raw, as-entered values plus a ``field -> message`` error map so a
    re-render after a rejected save reproduces the doctor's input exactly with
    per-field annotations (Req 1.5, 1.6).
    """

    present: bool
    location: str = ""
    hours: list[HoursRow] = field(default_factory=list)
    services: list[ServiceRow] = field(default_factory=list)
    accepted_insurance: str = ""
    providers: list[ProviderRow] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    saved: bool = False
    save_failed: bool = False
    message: str | None = None
    #: ``True`` when these values were read from an uploaded document rather than
    #: typed. Nothing is saved in that state — it changes the wording so the
    #: doctor knows they are confirming a machine's reading, not reviewing their
    #: own entry.
    prefilled: bool = False
    #: Plain-language remarks about the pre-fill: what was found, what was
    #: discarded, what still needs entering. Kept separate from :attr:`errors`
    #: because these are not field rejections — putting them in ``errors`` would
    #: make :attr:`missing_required_fields` report a failed save that never
    #: happened.
    review_notes: list[str] = field(default_factory=list)

    @property
    def missing_required_fields(self) -> list[str]:
        """Top-level required fields flagged missing (Req 1.5), in a stable order."""
        order = ("hours", "location", "services", "providers")
        return [f for f in order if f in self.errors]

    def field_error(self, field_name: str) -> str | None:
        """The error message for ``field_name`` if any."""
        return self.errors.get(field_name)


# ---------------------------------------------------------------------------
# First-access detection (Req 1.1)
# ---------------------------------------------------------------------------


def should_present_onboarding(store: ClinicKnowledgeBaseStore) -> bool:
    """Return ``True`` when the onboarding wizard should be shown (Req 1.1).

    The wizard is presented on first access when no usable clinic configuration
    exists: the store has nothing saved yet (``Ok(None)``, Req 16.4) or the
    saved configuration is not yet marked ``configured``. If the store read
    itself fails we cannot confirm a configuration exists, so we present
    onboarding rather than silently hide it.
    """
    result = store.get()
    if is_err(result):
        return True
    kb = result.value
    if kb is None:
        return True
    return not kb.configured


# ---------------------------------------------------------------------------
# Form parsing helpers
# ---------------------------------------------------------------------------


def _get(form: Mapping[str, str], key: str) -> str:
    """Return a trimmed string value for ``key`` (empty string when absent)."""
    value = form.get(key)
    return value.strip() if isinstance(value, str) else ""


def _checkbox(form: Mapping[str, str], key: str) -> bool:
    """Return ``True`` when a checkbox-style field is present and non-empty."""
    value = form.get(key)
    if value is None:
        return False
    return str(value).strip().lower() not in ("", "0", "false", "off", "no")


def _max_index(form: Mapping[str, str], coll: str) -> int:
    """Return the highest submitted index for ``coll`` (``-1`` if none)."""
    highest = -1
    for key in form:
        match = _INDEXED_KEY.match(key)
        if match is not None and match.group("coll") == coll:
            highest = max(highest, int(match.group("idx")))
    return highest


def _slug(text: str) -> str:
    """Lowercase, hyphenated identifier derived from ``text`` (may be empty)."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _split_days(raw: str) -> list[int]:
    """Parse a ``"1,3, 5"``-style weekday list into valid day indices (0–6)."""
    days: list[int] = []
    for piece in re.split(r"[,\s]+", raw.strip()):
        if not piece:
            continue
        try:
            day = int(piece)
        except ValueError:
            continue
        if 0 <= day < _NUM_DAYS and day not in days:
            days.append(day)
    return days


def rows_from_form(form: Mapping[str, str]) -> OnboardingWizardViewModel:
    """Build a view-model of raw, as-typed values from a submitted form.

    This is what makes value retention (Req 1.5, 1.6) mechanical: the returned
    rows mirror the doctor's input verbatim, independent of whether it parses
    into a valid model.
    """
    hours: list[HoursRow] = []
    for day in range(_NUM_DAYS):
        hours.append(
            HoursRow(
                day_index=day,
                day_name=WEEKDAY_NAMES[day],
                open=_get(form, f"hours[{day}].open"),
                close=_get(form, f"hours[{day}].close"),
                closed=_checkbox(form, f"hours[{day}].closed"),
            )
        )

    services: list[ServiceRow] = []
    for i in range(_max_index(form, "services") + 1):
        services.append(
            ServiceRow(
                index=i,
                name=_get(form, f"services[{i}].name"),
                prep_instructions=_get(form, f"services[{i}].prep_instructions"),
                price=_get(form, f"services[{i}].price"),
            )
        )
    if not services:
        services.append(ServiceRow(index=0))

    providers: list[ProviderRow] = []
    for i in range(_max_index(form, "providers") + 1):
        providers.append(
            ProviderRow(
                index=i,
                id=_get(form, f"providers[{i}].id"),
                name=_get(form, f"providers[{i}].name"),
                specialty=_get(form, f"providers[{i}].specialty"),
                days=_get(form, f"providers[{i}].days"),
                start=_get(form, f"providers[{i}].start"),
                end=_get(form, f"providers[{i}].end"),
            )
        )
    if not providers:
        providers.append(ProviderRow(index=0))

    return OnboardingWizardViewModel(
        present=True,
        location=_get(form, "location"),
        hours=hours,
        services=services,
        accepted_insurance=_get(form, "accepted_insurance"),
        providers=providers,
    )


def parse_form(
    form: Mapping[str, str],
) -> tuple[ClinicKnowledgeBase, dict[str, str]]:
    """Parse a submitted form into a :class:`ClinicKnowledgeBase`.

    Returns the parsed configuration together with a ``field -> message`` map of
    *parse* errors (values that could not be interpreted, e.g. a non-numeric
    price). These parse errors are merged with the domain validation violations
    by :func:`handle_submit`, so the doctor sees a single, complete error set.

    Empty service/provider rows (every input blank) are dropped so trailing
    template rows do not create bogus entries. Providers are given a stable id
    (derived from the name, falling back to ``provider-<n>``) so the write
    satisfies the store's provider-id requirement (Req 16.7).
    """
    parse_errors: dict[str, str] = {}

    # --- Hours -------------------------------------------------------------
    hours: dict[int, DayHours | None] = {}
    for day in range(_NUM_DAYS):
        closed = _checkbox(form, f"hours[{day}].closed")
        open_ = _get(form, f"hours[{day}].open")
        close = _get(form, f"hours[{day}].close")
        if not closed and open_ and close:
            hours[day] = DayHours(open=open_, close=close)
        else:
            hours[day] = None

    # --- Services ----------------------------------------------------------
    services: list[ServiceConfig] = []
    for i in range(_max_index(form, "services") + 1):
        name = _get(form, f"services[{i}].name")
        prep = _get(form, f"services[{i}].prep_instructions")
        price_raw = _get(form, f"services[{i}].price")
        if not name and not prep and not price_raw:
            continue  # skip an entirely-empty row
        price: float | None = None
        if price_raw:
            try:
                price = float(price_raw)
            except ValueError:
                parse_errors[f"services[{i}].price"] = "price must be a number"
        services.append(
            ServiceConfig(
                name=name,
                prep_instructions=prep or None,
                price=price,
            )
        )

    # --- Providers ---------------------------------------------------------
    providers: list[Provider] = []
    for i in range(_max_index(form, "providers") + 1):
        pid = _get(form, f"providers[{i}].id")
        name = _get(form, f"providers[{i}].name")
        specialty = _get(form, f"providers[{i}].specialty")
        days_raw = _get(form, f"providers[{i}].days")
        start = _get(form, f"providers[{i}].start")
        end = _get(form, f"providers[{i}].end")
        if not (pid or name or specialty or days_raw or start or end):
            continue  # skip an entirely-empty row
        schedule: list[ScheduleRule] = []
        if start and end:
            for day in _split_days(days_raw):
                schedule.append(ScheduleRule(day_of_week=day, start=start, end=end))
        provider_id = pid or _slug(name) or f"provider-{i + 1}"
        providers.append(
            Provider(
                id=provider_id,
                name=name,
                specialty=specialty,
                schedule=schedule,
            )
        )

    kb = ClinicKnowledgeBase(
        location=_get(form, "location"),
        hours=hours,
        services=services,
        accepted_insurance=_split_insurance(_get(form, "accepted_insurance")),
        providers=providers,
    )
    return kb, parse_errors


def _split_insurance(raw: str) -> list[str]:
    """Split accepted-insurance input (commas or newlines) into a clean list."""
    return [p.strip() for p in re.split(r"[,\n]+", raw) if p.strip()]


# ---------------------------------------------------------------------------
# Initial (GET) view-model
# ---------------------------------------------------------------------------


def _view_model_from_kb(
    kb: ClinicKnowledgeBase | None, *, present: bool
) -> OnboardingWizardViewModel:
    """Build a view-model prefilled from an existing config (or blank defaults)."""
    hours: list[HoursRow] = []
    for day in range(_NUM_DAYS):
        dh = kb.hours.get(day) if kb is not None else None
        hours.append(
            HoursRow(
                day_index=day,
                day_name=WEEKDAY_NAMES[day],
                open=dh.open if dh is not None else "",
                close=dh.close if dh is not None else "",
                closed=dh is None,
            )
        )

    services: list[ServiceRow] = []
    if kb is not None and kb.services:
        for i, svc in enumerate(kb.services):
            services.append(
                ServiceRow(
                    index=i,
                    name=svc.name,
                    prep_instructions=svc.prep_instructions or "",
                    price="" if svc.price is None else f"{svc.price:g}",
                )
            )
    else:
        services.append(ServiceRow(index=0))

    providers: list[ProviderRow] = []
    if kb is not None and kb.providers:
        for i, prov in enumerate(kb.providers):
            days = ",".join(str(rule.day_of_week) for rule in prov.schedule)
            start = prov.schedule[0].start if prov.schedule else ""
            end = prov.schedule[0].end if prov.schedule else ""
            providers.append(
                ProviderRow(
                    index=i,
                    id=prov.id,
                    name=prov.name,
                    specialty=prov.specialty,
                    days=days,
                    start=start,
                    end=end,
                )
            )
    else:
        providers.append(ProviderRow(index=0))

    return OnboardingWizardViewModel(
        present=present,
        location=kb.location if kb is not None else "",
        hours=hours,
        services=services,
        accepted_insurance=", ".join(kb.accepted_insurance) if kb is not None else "",
        providers=providers,
    )


def view_model_from_candidate(
    candidate: ClinicKnowledgeBase,
    *,
    notes: Sequence[str] = (),
    source_filename: str = "",
) -> OnboardingWizardViewModel:
    """Build a wizard view-model pre-filled from a document-derived candidate.

    Renders the form with values *proposed*, not saved. Nothing here touches the
    store: the doctor pressing "Save configuration" runs the ordinary
    :func:`handle_submit` path, so a machine's reading of a PDF goes through the
    same validation and the same explicit human action as typed input.

    ``present`` stays ``True`` even when the candidate looks complete — a complete
    candidate is exactly the case where skipping straight to a saved state would be
    most damaging, because the doctor would never see the values now driving what
    the agent quotes and books.

    Args:
        candidate: The extracted configuration to show.
        notes: Remarks from extraction, shown as a review list.
        source_filename: The document the values came from, named in the notice so
            the doctor knows what they are checking against.
    """
    view = _view_model_from_kb(candidate, present=True)
    view.prefilled = True
    view.review_notes = list(notes)
    source = f" from {source_filename}" if source_filename else ""
    view.message = (
        f"These details were read{source} and have not been saved. "
        "Check each one — especially service names and prices, which the agent "
        "quotes to callers — then press Save configuration."
    )
    return view


def build_view_model(store: ClinicKnowledgeBaseStore) -> OnboardingWizardViewModel:
    """Build the initial wizard view-model for a dashboard GET (Req 1.1).

    ``present`` is ``True`` when onboarding should be shown. Any partially-saved
    configuration is used to prefill the form so the doctor can complete it.
    """
    result = store.get()
    kb = None if is_err(result) else result.value
    present = should_present_onboarding(store)
    return _view_model_from_kb(kb, present=present)


# ---------------------------------------------------------------------------
# Submit handling (Req 1.4, 1.5, 1.6)
# ---------------------------------------------------------------------------


def handle_submit(
    store: ClinicKnowledgeBaseStore,
    form: Mapping[str, str],
    *,
    clock: Clock | None = None,
) -> OnboardingWizardViewModel:
    """Validate and save a submitted onboarding form, returning a view-model.

    On success (Req 1.4) the returned view-model has ``saved=True`` and
    ``present=False`` (the clinic is now configured, so the wizard steps aside).
    On a validation failure (Req 1.5) or a persistence failure (Req 1.6) the
    view-model retains every entered value, carries a per-field error map (each
    :class:`ConfigViolation` keyed by its ``field`` plus any parse errors), and
    keeps ``present=True`` so the doctor can correct and resubmit without losing
    their work.
    """
    view = rows_from_form(form)
    kb, parse_errors = parse_form(form)

    save_kwargs = {} if clock is None else {"clock": clock}
    result: ConfigSaveResult = save_clinic_config(store, kb, **save_kwargs)

    errors: dict[str, str] = dict(parse_errors)
    for violation in result.validation.violations:
        # Keep an existing (parse) error for the field if present; otherwise use
        # the domain violation's detail.
        errors.setdefault(violation.field, violation.detail)

    view.errors = errors

    # A parse error (e.g. a non-numeric price) is a real problem even if the
    # domain validator could not see it (the bad value became ``None``).
    ok = result.ok and not parse_errors

    if ok:
        view.saved = True
        view.present = False
        view.message = "Clinic configuration saved."
        return view

    view.saved = False
    view.present = True
    if result.failed_persistence:
        view.save_failed = True
        detail = result.store_error.detail if result.store_error else "unknown error"
        view.message = f"Save failed and was not applied: {detail}"
    elif parse_errors and result.ok:
        view.message = "Some values could not be understood. Please correct them."
    else:
        view.message = "Please correct the highlighted fields."
    return view


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


def _esc(value: str) -> str:
    """HTML-escape a value for safe attribute/text interpolation."""
    return html.escape(value, quote=True)


def _error_html(view: OnboardingWizardViewModel, field_name: str) -> str:
    message = view.field_error(field_name)
    if not message:
        return ""
    return (
        f'<p class="field-error" data-field="{_esc(field_name)}">'
        f"{_esc(message)}</p>"
    )


def _render_body(view: OnboardingWizardViewModel) -> str:
    parts: list[str] = []

    if view.saved:
        parts.append('<p class="wizard-success" role="status">Clinic configuration saved.</p>')
    if view.message and not view.saved:
        if view.save_failed:
            cls = "wizard-error"
        elif view.prefilled:
            cls = "wizard-review"
        else:
            cls = "wizard-notice"
        parts.append(f'<p class="{cls}" role="alert">{_esc(view.message)}</p>')

    if view.review_notes:
        items = "".join(f"<li>{_esc(note)}</li>" for note in view.review_notes)
        parts.append(
            '<div class="review-notes" role="status">'
            "<p>Before you save, note:</p>"
            f"<ul>{items}</ul></div>"
        )

    missing = view.missing_required_fields
    if missing:
        items = "".join(f"<li>{_esc(name)}</li>" for name in missing)
        parts.append(
            '<div class="required-summary" role="alert">'
            "<p>Please provide the following required information:</p>"
            f"<ul>{items}</ul></div>"
        )

    # Location
    parts.append('<fieldset><legend>Clinic location</legend>')
    parts.append(
        '<label>Location'
        f'<input type="text" name="location" value="{_esc(view.location)}"></label>'
    )
    parts.append(_error_html(view, "location"))
    parts.append("</fieldset>")

    # Hours
    parts.append('<fieldset><legend>Clinic hours</legend>')
    parts.append(_error_html(view, "hours"))
    parts.append('<table class="hours-table"><tbody>')
    for hours_row in view.hours:
        d = hours_row.day_index
        checked = " checked" if hours_row.closed else ""
        parts.append(
            "<tr>"
            f"<th scope=\"row\">{_esc(hours_row.day_name)}</th>"
            f'<td><input type="time" name="hours[{d}].open" value="{_esc(hours_row.open)}"></td>'
            f'<td><input type="time" name="hours[{d}].close" value="{_esc(hours_row.close)}"></td>'
            f'<td><label><input type="checkbox" name="hours[{d}].closed"{checked}> Closed</label></td>'
            "</tr>"
        )
    parts.append("</tbody></table></fieldset>")

    # Services
    parts.append('<fieldset id="services-fieldset"><legend>Offered services</legend>')
    parts.append(_error_html(view, "services"))
    parts.append('<div id="services-list">')
    for service_row in view.services:
        parts.append(_render_service_row(view, service_row))
    parts.append("</div>")
    parts.append('<button type="button" data-action="add-service">Add service</button>')
    parts.append("</fieldset>")

    # Insurance
    parts.append('<fieldset><legend>Accepted insurance</legend>')
    parts.append(
        '<label>Accepted insurance (one per line or comma-separated)'
        f'<textarea name="accepted_insurance">{_esc(view.accepted_insurance)}</textarea>'
        "</label>"
    )
    parts.append("</fieldset>")

    # Providers
    parts.append('<fieldset id="providers-fieldset"><legend>Providers</legend>')
    parts.append(_error_html(view, "providers"))
    parts.append('<div id="providers-list">')
    for provider_row in view.providers:
        parts.append(_render_provider_row(view, provider_row))
    parts.append("</div>")
    parts.append('<button type="button" data-action="add-provider">Add provider</button>')
    parts.append("</fieldset>")

    label = "Confirm and save" if view.prefilled else "Save configuration"
    parts.append(
        f'<button type="submit" class="wizard-submit">{label}</button>'
    )
    return "\n".join(parts)


def _render_service_row(view: OnboardingWizardViewModel, row: ServiceRow) -> str:
    i = row.index
    return (
        f'<div class="service-row" data-index="{i}">'
        f'<label>Name<input type="text" name="services[{i}].name" '
        f'value="{_esc(row.name)}"></label>'
        f'<label>Preparation instructions'
        f'<textarea name="services[{i}].prep_instructions">'
        f"{_esc(row.prep_instructions)}</textarea></label>"
        f"{_error_html(view, f'services[{i}].prep_instructions')}"
        f'<label>Price<input type="text" inputmode="decimal" '
        f'name="services[{i}].price" value="{_esc(row.price)}"></label>'
        f"{_error_html(view, f'services[{i}].price')}"
        '<button type="button" data-action="remove-service">Remove</button>'
        "</div>"
    )


def _render_provider_row(view: OnboardingWizardViewModel, row: ProviderRow) -> str:
    i = row.index
    return (
        f'<div class="provider-row" data-index="{i}">'
        f'<input type="hidden" name="providers[{i}].id" value="{_esc(row.id)}">'
        f'<label>Name<input type="text" name="providers[{i}].name" '
        f'value="{_esc(row.name)}"></label>'
        f"{_error_html(view, f'providers[{i}].name')}"
        f'<label>Specialty<input type="text" name="providers[{i}].specialty" '
        f'value="{_esc(row.specialty)}"></label>'
        f'<label>Available days (0=Sun … 6=Sat)'
        f'<input type="text" name="providers[{i}].days" value="{_esc(row.days)}"></label>'
        f'<label>Start<input type="time" name="providers[{i}].start" '
        f'value="{_esc(row.start)}"></label>'
        f'<label>End<input type="time" name="providers[{i}].end" '
        f'value="{_esc(row.end)}"></label>'
        '<button type="button" data-action="remove-provider">Remove</button>'
        "</div>"
    )


def _template_shell() -> str:
    """Return the page-shell template, or a minimal built-in fallback."""
    try:
        return TEMPLATE_PATH.read_text(encoding="utf-8")
    except OSError:
        return (
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
            "<title>Clinic Onboarding</title></head><body>"
            '<form id="onboarding-wizard" method="post">'
            f"{_BODY_TOKEN}</form>"
            '<script src="onboarding_wizard.js"></script>'
            "</body></html>"
        )


def render_html(view: OnboardingWizardViewModel) -> str:
    """Render the wizard to a full HTML page.

    When ``view.present`` is ``False`` (the clinic is already configured, e.g.
    right after a successful save) a short confirmation page is returned instead
    of the form, so the onboarding workflow is only shown on first access
    (Req 1.1).
    """
    if not view.present:
        banner = (
            '<p class="wizard-success" role="status">Clinic configuration saved.</p>'
            if view.saved
            else '<p class="wizard-notice">Clinic configuration is complete.</p>'
        )
        body = f'<div class="wizard-done">{banner}</div>'
    else:
        body = _render_body(view)
    return _template_shell().replace(_BODY_TOKEN, body)


__all__ = [
    "WEEKDAY_NAMES",
    "TEMPLATE_PATH",
    "HoursRow",
    "ServiceRow",
    "ProviderRow",
    "OnboardingWizardViewModel",
    "should_present_onboarding",
    "rows_from_form",
    "parse_form",
    "build_view_model",
    "view_model_from_candidate",
    "handle_submit",
    "render_html",
]
