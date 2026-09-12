"""``CallActivityLog`` and ``ImpactMetricsStrip`` components (task 13.3).

Two server-rendered dashboard components, both built with the same three-part
pattern (see this package's ``__init__``): a pure view-model builder, an HTML
partial under :mod:`clinic_front_desk.dashboard.web`, and a shared vanilla-JS
snippet (``web/activity_metrics.js``) that keeps them fresh in real time.

``CallActivityLog`` (Req 15.2, 15.4)
    Renders the call-activity log — each entry's interaction type, date-time,
    and patient identifier — ordered most-recent-first. It consumes the entries
    produced by :func:`clinic_front_desk.dashboard.activity_log.build_activity_log`
    (served by ``DashboardBFF.recent_activity``), so ordering and content are
    already established upstream; this component only shapes them for display.
    The JS snippet re-fetches the partial on the relevant ChangeEvents, so the
    log reflects booked/rescheduled/cancelled/escalated changes within 5 s.

``ImpactMetricsStrip`` (Req 15.3, 15.4)
    Renders front-desk hours saved, the waitlist-recovered appointment count,
    and the no-show rate together with its trend (the change versus the
    immediately preceding equal-length period), plus a 7/30/90-day period
    selector. It consumes the :class:`~clinic_front_desk.dashboard.metrics.ImpactMetrics`
    produced by ``compute_impact_metrics`` (served via ``DashboardBFF.impact_metrics``).
    The JS snippet re-fetches the partial when the selected period changes and
    when a relevant ChangeEvent arrives, keeping it fresh within 5 s.

Every builder here is **pure and deterministic**: given the same inputs it
returns the same view-model, with no I/O, no clock read, and no mutation of the
inputs. The ``render_*`` functions turn a view-model into an HTML string by
filling the corresponding partial; all interpolated text is HTML-escaped so
patient-supplied identifiers cannot inject markup.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from clinic_front_desk.dashboard.activity_log import (
    ActivityLogEntry,
    InteractionType,
)
from clinic_front_desk.dashboard.metrics import (
    SUPPORTED_WINDOW_DAYS,
    ImpactMetrics,
)

# Directory holding the HTML partials this module renders (sibling ``web/``).
_WEB_DIR = Path(__file__).resolve().parent.parent / "web"

#: Default endpoints the client JS re-fetches from. Callers may override when a
#: deployment mounts the dashboard under a different path.
DEFAULT_ACTIVITY_ENDPOINT = "/dashboard/activity"
DEFAULT_METRICS_ENDPOINT = "/dashboard/metrics"

#: Human-readable labels for each interaction type (Req 15.2).
_INTERACTION_LABELS: dict[InteractionType, str] = {
    InteractionType.BOOKED: "Booked",
    InteractionType.RESCHEDULED: "Rescheduled",
    InteractionType.CANCELLED: "Cancelled",
    InteractionType.ESCALATED: "Escalated",
}

#: Shown when an entry has no captured patient identifier (Req 15.2).
_UNKNOWN_PATIENT = "Unknown patient"

#: Shown when the activity log has no entries.
ACTIVITY_EMPTY_MESSAGE = "No call activity yet."


@lru_cache(maxsize=None)
def _load_template(name: str) -> str:
    """Read and cache an HTML partial from the ``web/`` directory."""
    return (_WEB_DIR / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# CallActivityLog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActivityRowViewModel:
    """One render-ready row of the call-activity log (Req 15.2).

    Attributes:
        interaction_type: The raw interaction type (booked/rescheduled/
            cancelled/escalated).
        interaction_label: Human-readable label for the interaction type.
        timestamp_iso: The entry's ISO-8601 UTC date-time (for ``<time
            datetime>`` and machine ordering).
        timestamp_display: A human-readable date-time for display.
        patient_identifier: The associated patient identifier, or ``None``.
        patient_display: ``patient_identifier`` when known, else a placeholder.
        source: ``"call_session"`` or ``"escalation"`` (the originating store).
    """

    interaction_type: InteractionType
    interaction_label: str
    timestamp_iso: str
    timestamp_display: str
    patient_identifier: str | None
    patient_display: str
    source: str


@dataclass(frozen=True)
class ActivityLogViewModel:
    """The call-activity log's view-model (Req 15.2)."""

    rows: list[ActivityRowViewModel]
    is_empty: bool
    empty_message: str = ACTIVITY_EMPTY_MESSAGE


def _format_timestamp(iso: str) -> str:
    """Render an ISO-8601 UTC timestamp for display, falling back to the raw
    string when it cannot be parsed."""
    try:
        parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return parsed.strftime("%b %d, %Y %H:%M UTC")


def build_activity_log_view_model(
    entries: list[ActivityLogEntry],
) -> ActivityLogViewModel:
    """Shape aggregated activity-log entries into a display view-model (Req 15.2).

    Pure and deterministic: preserves the upstream most-recent-first ordering
    (the entries are already ordered by
    :func:`~clinic_front_desk.dashboard.activity_log.build_activity_log`) and
    maps each entry to its interaction label, display timestamp, and patient
    identifier without any I/O or input mutation.
    """
    rows = [
        ActivityRowViewModel(
            interaction_type=entry.interaction_type,
            interaction_label=_INTERACTION_LABELS[entry.interaction_type],
            timestamp_iso=entry.timestamp,
            timestamp_display=_format_timestamp(entry.timestamp),
            patient_identifier=entry.patient_identifier,
            patient_display=entry.patient_identifier or _UNKNOWN_PATIENT,
            source=entry.source,
        )
        for entry in entries
    ]
    return ActivityLogViewModel(rows=rows, is_empty=not rows)


def _render_activity_row(row: ActivityRowViewModel) -> str:
    """Render one activity-log ``<li>`` with all interpolated text escaped."""
    return (
        '<li class="call-activity-log__row" '
        f'data-interaction="{html.escape(row.interaction_type.value, quote=True)}" '
        f'data-source="{html.escape(row.source, quote=True)}">'
        f'<span class="call-activity-log__type">{html.escape(row.interaction_label)}</span>'
        f'<time class="call-activity-log__time" '
        f'datetime="{html.escape(row.timestamp_iso, quote=True)}">'
        f"{html.escape(row.timestamp_display)}</time>"
        f'<span class="call-activity-log__patient">{html.escape(row.patient_display)}</span>'
        "</li>"
    )


def render_activity_log(
    entries: list[ActivityLogEntry],
    *,
    activity_endpoint: str = DEFAULT_ACTIVITY_ENDPOINT,
) -> str:
    """Render the ``CallActivityLog`` partial to an HTML string (Req 15.2, 15.4).

    Builds the view-model, renders one row per entry (most-recent-first), and
    fills the ``web/activity_log.html`` partial. When there are no entries an
    empty-state row is rendered instead. ``activity_endpoint`` is embedded so the
    client JS knows where to re-fetch the partial on a ChangeEvent (Req 15.4).
    """
    view_model = build_activity_log_view_model(entries)
    if view_model.is_empty:
        rows_html = (
            '<li class="call-activity-log__empty" data-role="activity-empty">'
            f"{html.escape(view_model.empty_message)}</li>"
        )
    else:
        rows_html = "\n    ".join(_render_activity_row(r) for r in view_model.rows)
    template = _load_template("activity_log.html")
    return template.format(
        rows=rows_html,
        activity_endpoint=html.escape(activity_endpoint, quote=True),
    )


# ---------------------------------------------------------------------------
# ImpactMetricsStrip
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PeriodOptionViewModel:
    """One option of the 7/30/90-day period selector (Req 15.3)."""

    days: int
    label: str
    selected: bool


@dataclass(frozen=True)
class MetricsStripViewModel:
    """The impact-metrics strip's view-model (Req 15.3).

    Attributes:
        window_days: The selected reporting window (7/30/90).
        hours_saved_display: Front-desk hours saved, formatted.
        waitlist_recovered_display: Waitlist-recovered appointment count.
        no_show_rate_display: No-show rate as a percentage string.
        no_show_trend_display: Trend as signed percentage points versus the
            immediately preceding equal-length period.
        trend_direction: ``"up"`` (worsening), ``"down"`` (improving), or
            ``"flat"`` (no change), for styling and accessibility.
        period_options: The selectable reporting periods with the current one
            marked selected.
    """

    window_days: int
    hours_saved_display: str
    waitlist_recovered_display: str
    no_show_rate_display: str
    no_show_trend_display: str
    trend_direction: str
    period_options: list[PeriodOptionViewModel] = field(default_factory=list)


def _format_hours(hours: float) -> str:
    """Format hours saved to one decimal place."""
    return f"{hours:.1f}"


def _format_rate(rate: float) -> str:
    """Format a ``[0, 1]`` rate as a one-decimal percentage."""
    return f"{rate * 100:.1f}%"


def _format_trend(trend: float) -> tuple[str, str]:
    """Format the no-show-rate trend as signed percentage points and a direction.

    Returns ``(display, direction)`` where direction is ``"up"`` when the rate
    worsened (positive trend), ``"down"`` when it improved (negative trend), and
    ``"flat"`` when unchanged. The threshold guards against float noise below a
    tenth of a percentage point (the display resolution).
    """
    points = trend * 100
    if points > 0.05:
        return f"+{points:.1f} pts", "up"
    if points < -0.05:
        return f"{points:.1f} pts", "down"
    return "0.0 pts", "flat"


def build_metrics_strip_view_model(metrics: ImpactMetrics) -> MetricsStripViewModel:
    """Shape computed :class:`ImpactMetrics` into a display view-model (Req 15.3).

    Pure and deterministic: formats the three metrics and the no-show-rate trend,
    and builds the 7/30/90-day period options with ``metrics.window_days`` marked
    selected. No I/O, no clock read, no input mutation.
    """
    trend_display, trend_direction = _format_trend(metrics.no_show_rate_trend)
    options = [
        PeriodOptionViewModel(
            days=days,
            label=f"Last {days} days",
            selected=(days == metrics.window_days),
        )
        for days in SUPPORTED_WINDOW_DAYS
    ]
    return MetricsStripViewModel(
        window_days=metrics.window_days,
        hours_saved_display=_format_hours(metrics.front_desk_hours_saved),
        waitlist_recovered_display=str(metrics.waitlist_recovered_count),
        no_show_rate_display=_format_rate(metrics.no_show_rate),
        no_show_trend_display=trend_display,
        trend_direction=trend_direction,
        period_options=options,
    )


def _render_period_option(option: PeriodOptionViewModel) -> str:
    """Render one ``<option>`` of the period selector."""
    selected_attr = " selected" if option.selected else ""
    return (
        f'<option value="{option.days}"{selected_attr}>'
        f"{html.escape(option.label)}</option>"
    )


def render_metrics_strip(
    metrics: ImpactMetrics,
    *,
    metrics_endpoint: str = DEFAULT_METRICS_ENDPOINT,
) -> str:
    """Render the ``ImpactMetricsStrip`` partial to an HTML string (Req 15.3, 15.4).

    Builds the view-model and fills the ``web/metrics_strip.html`` partial,
    including the 7/30/90-day period selector with the active window selected.
    ``metrics_endpoint`` is embedded so the client JS can re-fetch the partial on
    a period change or a relevant ChangeEvent (Req 15.4).
    """
    view_model = build_metrics_strip_view_model(metrics)
    options_html = "\n      ".join(
        _render_period_option(option) for option in view_model.period_options
    )
    template = _load_template("metrics_strip.html")
    return template.format(
        period_options=options_html,
        hours_saved=html.escape(view_model.hours_saved_display),
        waitlist_recovered=html.escape(view_model.waitlist_recovered_display),
        no_show_rate=html.escape(view_model.no_show_rate_display),
        no_show_trend=html.escape(view_model.no_show_trend_display),
        trend_direction=html.escape(view_model.trend_direction, quote=True),
        window_days=view_model.window_days,
        metrics_endpoint=html.escape(metrics_endpoint, quote=True),
    )


__all__ = [
    "DEFAULT_ACTIVITY_ENDPOINT",
    "DEFAULT_METRICS_ENDPOINT",
    "ACTIVITY_EMPTY_MESSAGE",
    "ActivityRowViewModel",
    "ActivityLogViewModel",
    "build_activity_log_view_model",
    "render_activity_log",
    "PeriodOptionViewModel",
    "MetricsStripViewModel",
    "build_metrics_strip_view_model",
    "render_metrics_strip",
]
