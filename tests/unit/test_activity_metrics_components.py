"""Unit tests for the ``CallActivityLog`` and ``ImpactMetricsStrip`` components
(task 13.3, Req 15.2, 15.3, 15.4).

Covers :mod:`clinic_front_desk.dashboard.components.activity_and_metrics`:

- The activity-log view-model maps each entry to its interaction label, display
  timestamp, and patient identifier, preserving the upstream most-recent-first
  ordering (Req 15.2).
- The rendered activity-log partial contains one row per entry, HTML-escapes
  patient-supplied text, shows an empty state, and embeds the re-fetch endpoint
  the real-time JS uses (Req 15.2, 15.4).
- The metrics view-model formats hours saved, the waitlist-recovered count, the
  no-show rate and its signed trend, and builds a 7/30/90-day period selector
  with the active window marked (Req 15.3).
- The rendered metrics partial contains those values, the period selector, and
  the re-fetch endpoint/window the real-time JS uses (Req 15.3, 15.4).

These are example/edge unit tests; the property tests for the underlying
aggregation and metrics computation live in tasks 12.9 and 12.7.
"""

from __future__ import annotations

from clinic_front_desk.dashboard.activity_log import (
    ActivityLogEntry,
    InteractionType,
)
from clinic_front_desk.dashboard.components.activity_and_metrics import (
    ACTIVITY_EMPTY_MESSAGE,
    DEFAULT_ACTIVITY_ENDPOINT,
    build_activity_log_view_model,
    build_metrics_strip_view_model,
    render_activity_log,
    render_metrics_strip,
)
from clinic_front_desk.dashboard.metrics import SUPPORTED_WINDOW_DAYS, ImpactMetrics


def _entry(
    interaction_type: InteractionType,
    *,
    timestamp: str,
    patient_identifier: str | None = None,
    source: str = "call_session",
    source_id: str = "src-1",
) -> ActivityLogEntry:
    return ActivityLogEntry(
        interaction_type=interaction_type,
        timestamp=timestamp,
        patient_identifier=patient_identifier,
        source=source,
        source_id=source_id,
    )


def _metrics(
    *,
    window_days: int = 30,
    hours_saved: float = 12.34,
    waitlist_recovered: int = 4,
    no_show_rate: float = 0.25,
    trend: float = 0.05,
) -> ImpactMetrics:
    return ImpactMetrics(
        window_days=window_days,
        front_desk_hours_saved=hours_saved,
        waitlist_recovered_count=waitlist_recovered,
        no_show_rate=no_show_rate,
        no_show_rate_trend=trend,
        handled_call_count=0,
        no_show_count=0,
        attended_appointment_count=0,
        preceding_no_show_rate=no_show_rate - trend,
    )


# ---------------------------------------------------------------------------
# CallActivityLog view-model (Req 15.2)
# ---------------------------------------------------------------------------


def test_activity_view_model_maps_interaction_labels() -> None:
    entries = [
        _entry(InteractionType.BOOKED, timestamp="2025-06-01T09:00:00Z"),
        _entry(InteractionType.RESCHEDULED, timestamp="2025-06-01T08:00:00Z"),
        _entry(InteractionType.CANCELLED, timestamp="2025-06-01T07:00:00Z"),
        _entry(
            InteractionType.ESCALATED,
            timestamp="2025-06-01T06:00:00Z",
            source="escalation",
        ),
    ]
    vm = build_activity_log_view_model(entries)
    labels = [row.interaction_label for row in vm.rows]
    assert labels == ["Booked", "Rescheduled", "Cancelled", "Escalated"]
    assert not vm.is_empty


def test_activity_view_model_preserves_upstream_order() -> None:
    """Req 15.2: the view-model keeps the most-recent-first order it is given."""
    entries = [
        _entry(InteractionType.BOOKED, timestamp="2025-06-01T14:00:00Z", source_id="a"),
        _entry(InteractionType.CANCELLED, timestamp="2025-06-01T09:00:00Z", source_id="b"),
    ]
    vm = build_activity_log_view_model(entries)
    assert [row.timestamp_iso for row in vm.rows] == [
        "2025-06-01T14:00:00Z",
        "2025-06-01T09:00:00Z",
    ]


def test_activity_view_model_shows_identifier_and_unknown_placeholder() -> None:
    known = build_activity_log_view_model(
        [_entry(InteractionType.BOOKED, timestamp="2025-06-01T09:00:00Z", patient_identifier="p1")]
    )
    assert known.rows[0].patient_display == "p1"

    unknown = build_activity_log_view_model(
        [_entry(InteractionType.BOOKED, timestamp="2025-06-01T09:00:00Z")]
    )
    assert unknown.rows[0].patient_identifier is None
    assert unknown.rows[0].patient_display != ""


def test_activity_view_model_empty() -> None:
    vm = build_activity_log_view_model([])
    assert vm.is_empty
    assert vm.rows == []
    assert vm.empty_message == ACTIVITY_EMPTY_MESSAGE


def test_activity_timestamp_display_is_human_readable_and_keeps_iso() -> None:
    vm = build_activity_log_view_model(
        [_entry(InteractionType.BOOKED, timestamp="2025-06-01T09:05:00Z")]
    )
    row = vm.rows[0]
    assert row.timestamp_iso == "2025-06-01T09:05:00Z"
    assert "2025" in row.timestamp_display and row.timestamp_display != row.timestamp_iso


# ---------------------------------------------------------------------------
# CallActivityLog rendering (Req 15.2, 15.4)
# ---------------------------------------------------------------------------


def test_render_activity_log_contains_rows_and_endpoint() -> None:
    html = render_activity_log(
        [
            _entry(InteractionType.BOOKED, timestamp="2025-06-01T09:00:00Z", patient_identifier="p1"),
            _entry(
                InteractionType.ESCALATED,
                timestamp="2025-06-01T08:00:00Z",
                patient_identifier="p2",
                source="escalation",
            ),
        ]
    )
    assert 'data-component="call-activity-log"' in html
    assert html.count('class="call-activity-log__row"') == 2
    assert "Booked" in html and "Escalated" in html
    assert "p1" in html and "p2" in html
    # Real-time re-fetch endpoint is embedded for the JS (Req 15.4).
    assert DEFAULT_ACTIVITY_ENDPOINT in html


def test_render_activity_log_empty_state() -> None:
    html = render_activity_log([])
    assert 'data-role="activity-empty"' in html
    assert ACTIVITY_EMPTY_MESSAGE in html
    assert 'class="call-activity-log__row"' not in html


def test_render_activity_log_escapes_patient_identifier() -> None:
    """Patient-supplied text must not inject markup (Req 15.2 safety)."""
    html = render_activity_log(
        [
            _entry(
                InteractionType.BOOKED,
                timestamp="2025-06-01T09:00:00Z",
                patient_identifier="<script>alert(1)</script>",
            )
        ]
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_activity_log_accepts_custom_endpoint() -> None:
    html = render_activity_log([], activity_endpoint="/clinic/activity")
    assert "/clinic/activity" in html


# ---------------------------------------------------------------------------
# ImpactMetricsStrip view-model (Req 15.3)
# ---------------------------------------------------------------------------


def test_metrics_view_model_formats_values() -> None:
    vm = build_metrics_strip_view_model(
        _metrics(hours_saved=12.34, waitlist_recovered=4, no_show_rate=0.25)
    )
    assert vm.hours_saved_display == "12.3"
    assert vm.waitlist_recovered_display == "4"
    assert vm.no_show_rate_display == "25.0%"


def test_metrics_view_model_trend_direction_up_down_flat() -> None:
    worsening = build_metrics_strip_view_model(_metrics(trend=0.05))
    assert worsening.trend_direction == "up"
    assert worsening.no_show_trend_display.startswith("+")

    improving = build_metrics_strip_view_model(_metrics(trend=-0.05))
    assert improving.trend_direction == "down"
    assert improving.no_show_trend_display.startswith("-")

    flat = build_metrics_strip_view_model(_metrics(trend=0.0))
    assert flat.trend_direction == "flat"


def test_metrics_view_model_period_options_mark_selected() -> None:
    vm = build_metrics_strip_view_model(_metrics(window_days=30))
    assert [o.days for o in vm.period_options] == list(SUPPORTED_WINDOW_DAYS)
    selected = [o for o in vm.period_options if o.selected]
    assert len(selected) == 1
    assert selected[0].days == 30


# ---------------------------------------------------------------------------
# ImpactMetricsStrip rendering (Req 15.3, 15.4)
# ---------------------------------------------------------------------------


def test_render_metrics_strip_contains_values_and_selector() -> None:
    html = render_metrics_strip(
        _metrics(window_days=90, hours_saved=8.0, waitlist_recovered=3, no_show_rate=0.1, trend=-0.02)
    )
    assert 'data-component="impact-metrics-strip"' in html
    assert "8.0" in html  # hours saved
    assert ">3<" in html  # waitlist recovered count
    assert "10.0%" in html  # no-show rate
    assert 'data-role="metrics-period-selector"' in html
    # The active window is embedded for the JS and selected in the dropdown.
    assert 'data-window-days="90"' in html
    assert 'value="90" selected' in html


def test_render_metrics_strip_trend_direction_class() -> None:
    up = render_metrics_strip(_metrics(trend=0.05))
    assert 'data-trend-direction="up"' in up
    assert "impact-metrics-strip__trend--up" in up

    down = render_metrics_strip(_metrics(trend=-0.05))
    assert 'data-trend-direction="down"' in down


def test_render_metrics_strip_lists_all_supported_periods() -> None:
    html = render_metrics_strip(_metrics(window_days=7))
    for days in SUPPORTED_WINDOW_DAYS:
        assert f'value="{days}"' in html
    assert 'value="7" selected' in html


def test_render_metrics_strip_accepts_custom_endpoint() -> None:
    html = render_metrics_strip(_metrics(), metrics_endpoint="/clinic/metrics")
    assert "/clinic/metrics" in html
