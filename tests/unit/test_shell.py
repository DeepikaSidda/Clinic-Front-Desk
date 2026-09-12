"""Unit tests for the dashboard shell role gating (task 13.5, Req 15.5, 15.7).

Covers :mod:`clinic_front_desk.dashboard.shell`:

- A viewer with an assigned role sees a shell composed of exactly the views the
  RoleGate permits for that role, in canonical order (Req 15.5).
- A viewer without an assigned role gets an access-denied shell with no schedule,
  call-activity, or metrics regions at all (Req 15.7).
- The rendered HTML mirrors the view-model: granted shells emit one region per
  permitted view; denied shells emit only the access-denied state.
- The shell defers entirely to the injected RoleGate (custom permission maps are
  honoured) and never re-implements the decision.
"""

from __future__ import annotations

import pytest

from clinic_front_desk.dashboard.role_gate import (
    DashboardView,
    Role,
    RoleGate,
)
from clinic_front_desk.dashboard.shell import (
    ACCESS_DENIED_MESSAGE,
    VIEW_ORDER,
    DashboardShellViewModel,
    build_dashboard_shell_view_model,
    render_dashboard_shell,
)


# --- Req 15.5: assigned role sees exactly its permitted views ---------------


def test_doctor_shell_shows_all_views_in_canonical_order() -> None:
    vm = build_dashboard_shell_view_model(Role.DOCTOR)

    assert vm.granted is True
    assert vm.denied is False
    assert vm.role == "doctor"
    assert vm.denied_reason is None
    # Doctor is permitted every view; order follows VIEW_ORDER exactly.
    assert vm.visible_views == VIEW_ORDER
    assert tuple(region.view for region in vm.regions) == VIEW_ORDER


def test_assistant_shell_shows_only_permitted_subset() -> None:
    vm = build_dashboard_shell_view_model(Role.ASSISTANT)

    assert vm.granted is True
    assert vm.visible_views == (
        DashboardView.SCHEDULE,
        DashboardView.CALL_ACTIVITY,
    )
    # The doctor-facing views are not rendered for the assistant.
    assert DashboardView.DECISIONS not in vm.visible_views
    assert DashboardView.IMPACT_METRICS not in vm.visible_views


def test_visible_views_follow_canonical_order_not_permission_order() -> None:
    # A gate whose map lists views out of canonical order must still render in
    # VIEW_ORDER.
    gate = RoleGate(
        {
            Role.DOCTOR: frozenset(
                {
                    DashboardView.DECISIONS,
                    DashboardView.SCHEDULE,
                    DashboardView.IMPACT_METRICS,
                }
            )
        }
    )

    vm = build_dashboard_shell_view_model(Role.DOCTOR, gate=gate)

    assert vm.visible_views == (
        DashboardView.SCHEDULE,
        DashboardView.IMPACT_METRICS,
        DashboardView.DECISIONS,
    )


def test_shell_defers_to_injected_gate_permission_map() -> None:
    gate = RoleGate({Role.ASSISTANT: frozenset({DashboardView.SCHEDULE})})

    vm = build_dashboard_shell_view_model(Role.ASSISTANT, gate=gate)

    assert vm.granted is True
    assert vm.visible_views == (DashboardView.SCHEDULE,)


def test_accepts_role_as_string() -> None:
    vm = build_dashboard_shell_view_model("doctor")

    assert vm.granted is True
    assert vm.role == "doctor"


# --- Req 15.7: no assigned role is denied with no data regions --------------


def test_no_role_is_denied_with_no_regions() -> None:
    vm = build_dashboard_shell_view_model(None)

    assert vm.granted is False
    assert vm.denied is True
    assert vm.role is None
    assert vm.denied_reason == "no_role_assigned"
    assert vm.visible_views == ()
    assert vm.regions == ()
    assert vm.access_denied_message == ACCESS_DENIED_MESSAGE


def test_unknown_role_is_denied_with_no_regions() -> None:
    vm = build_dashboard_shell_view_model("receptionist")

    assert vm.granted is False
    assert vm.denied_reason == "unknown_role"
    assert vm.visible_views == ()
    assert vm.regions == ()


def test_role_without_configured_views_is_denied() -> None:
    # Assistant is a valid role but unmapped in this gate.
    gate = RoleGate({Role.DOCTOR: frozenset({DashboardView.SCHEDULE})})

    vm = build_dashboard_shell_view_model(Role.ASSISTANT, gate=gate)

    assert vm.granted is False
    assert vm.visible_views == ()


# --- rendering mirrors the view-model ---------------------------------------


def test_render_granted_shell_emits_one_region_per_view() -> None:
    html_out = render_dashboard_shell(Role.ASSISTANT)

    assert 'data-view="schedule"' in html_out
    assert 'data-view="call_activity"' in html_out
    assert 'id="dashboard-view-schedule"' in html_out
    assert 'id="dashboard-view-call-activity"' in html_out
    # Not-permitted views and the denied state are absent.
    assert 'data-view="impact_metrics"' not in html_out
    assert 'data-view="decisions"' not in html_out
    assert 'data-role="access-denied"' not in html_out
    # The shell token is fully substituted.
    assert "{{SHELL_BODY}}" not in html_out


def test_render_denied_shell_has_no_view_regions() -> None:
    html_out = render_dashboard_shell(None)

    assert 'data-role="access-denied"' in html_out
    assert 'data-denied-reason="no_role_assigned"' in html_out
    assert ACCESS_DENIED_MESSAGE in html_out
    # Crucially, NO schedule / activity / metrics region is rendered (Req 15.7).
    assert 'class="dashboard-shell__view"' not in html_out
    assert 'data-view="schedule"' not in html_out
    assert 'data-view="call_activity"' not in html_out
    assert 'data-view="impact_metrics"' not in html_out
    assert "{{SHELL_BODY}}" not in html_out


def test_render_doctor_shell_emits_all_views() -> None:
    html_out = render_dashboard_shell(Role.DOCTOR)

    for view in VIEW_ORDER:
        assert f'data-view="{view.value}"' in html_out
    assert 'data-role="access-denied"' not in html_out


def test_view_model_is_frozen() -> None:
    vm = build_dashboard_shell_view_model(Role.DOCTOR)

    with pytest.raises(Exception):
        vm.granted = False  # type: ignore[misc]


def test_returns_shell_view_model_type() -> None:
    vm = build_dashboard_shell_view_model(Role.DOCTOR)

    assert isinstance(vm, DashboardShellViewModel)
