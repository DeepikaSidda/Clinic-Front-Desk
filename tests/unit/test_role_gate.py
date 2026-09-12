"""Unit tests for the dashboard RoleGate access control (task 12.4, Req 15.5, 15.7).

Covers :mod:`clinic_front_desk.dashboard.role_gate`:

- A viewer with an assigned role is granted exactly the views permitted for
  that role (Req 15.5).
- A viewer without an assigned role (``None``) is denied access and gets no
  permitted views and no data (Req 15.7).
- An unrecognized role string is treated as denied.
- ``filter_data`` returns only permitted views' payloads, and nothing at all for
  a denied viewer (Req 15.7).
- The gate is pure/immutable: mutating the source permission map after
  construction does not change decisions.

The property test for role-scoped access lives in task 12.5.
"""

from __future__ import annotations

import pytest

from clinic_front_desk.dashboard.role_gate import (
    DEFAULT_ROLE_VIEWS,
    AccessDenied,
    AccessGranted,
    DashboardView,
    Role,
    RoleGate,
)


@pytest.fixture
def gate() -> RoleGate:
    return RoleGate()


# --- Req 15.5: assigned role sees exactly its permitted views ---------------


def test_doctor_is_granted_all_views(gate: RoleGate) -> None:
    decision = gate.resolve(Role.DOCTOR)

    assert isinstance(decision, AccessGranted)
    assert decision.granted is True
    assert decision.role is Role.DOCTOR
    assert decision.permitted_views == frozenset(DashboardView)


def test_assistant_is_granted_only_its_subset(gate: RoleGate) -> None:
    decision = gate.resolve(Role.ASSISTANT)

    assert isinstance(decision, AccessGranted)
    assert decision.permitted_views == frozenset(
        {DashboardView.SCHEDULE, DashboardView.CALL_ACTIVITY}
    )
    # The doctor-facing decisions feed and metrics strip are not permitted.
    assert DashboardView.DECISIONS not in decision.permitted_views
    assert DashboardView.IMPACT_METRICS not in decision.permitted_views


def test_resolve_accepts_role_as_string(gate: RoleGate) -> None:
    decision = gate.resolve("doctor")

    assert isinstance(decision, AccessGranted)
    assert decision.role is Role.DOCTOR


def test_is_permitted_reflects_role_scope(gate: RoleGate) -> None:
    assert gate.is_permitted(Role.DOCTOR, DashboardView.DECISIONS) is True
    assert gate.is_permitted(Role.ASSISTANT, DashboardView.SCHEDULE) is True
    assert (
        gate.is_permitted(Role.ASSISTANT, DashboardView.IMPACT_METRICS) is False
    )


# --- Req 15.7: no assigned role is denied and returns no data ---------------


def test_no_role_is_denied_with_no_views(gate: RoleGate) -> None:
    decision = gate.resolve(None)

    assert isinstance(decision, AccessDenied)
    assert decision.granted is False
    assert decision.reason == "no_role_assigned"
    assert decision.permitted_views == frozenset()


def test_unknown_role_string_is_denied(gate: RoleGate) -> None:
    decision = gate.resolve("receptionist")

    assert isinstance(decision, AccessDenied)
    assert decision.reason == "unknown_role"
    assert decision.permitted_views == frozenset()


def test_role_without_configured_views_is_denied() -> None:
    # Only the doctor is mapped; the assistant is a valid role but unmapped.
    gate = RoleGate({Role.DOCTOR: frozenset({DashboardView.SCHEDULE})})

    assert isinstance(gate.resolve(Role.ASSISTANT), AccessDenied)
    assert gate.permitted_views(Role.ASSISTANT) == frozenset()


def test_permitted_views_empty_for_no_role(gate: RoleGate) -> None:
    assert gate.permitted_views(None) == frozenset()


# --- filter_data: only permitted payloads, nothing when denied --------------


def test_filter_data_keeps_only_permitted_views(gate: RoleGate) -> None:
    data = {
        DashboardView.SCHEDULE: "schedule-data",
        DashboardView.CALL_ACTIVITY: "activity-data",
        DashboardView.IMPACT_METRICS: "metrics-data",
        DashboardView.DECISIONS: "decisions-data",
    }

    filtered = gate.filter_data(Role.ASSISTANT, data)

    assert filtered == {
        DashboardView.SCHEDULE: "schedule-data",
        DashboardView.CALL_ACTIVITY: "activity-data",
    }


def test_filter_data_returns_nothing_for_no_role(gate: RoleGate) -> None:
    data = {
        DashboardView.SCHEDULE: "schedule-data",
        DashboardView.CALL_ACTIVITY: "activity-data",
        DashboardView.IMPACT_METRICS: "metrics-data",
    }

    assert gate.filter_data(None, data) == {}


def test_filter_data_does_not_mutate_input(gate: RoleGate) -> None:
    data = {
        DashboardView.SCHEDULE: "schedule-data",
        DashboardView.DECISIONS: "decisions-data",
    }

    gate.filter_data(Role.ASSISTANT, data)

    assert data == {
        DashboardView.SCHEDULE: "schedule-data",
        DashboardView.DECISIONS: "decisions-data",
    }


# --- purity / immutability --------------------------------------------------


def test_gate_is_immutable_to_source_map_mutation() -> None:
    source: dict[Role, frozenset[DashboardView]] = {
        Role.DOCTOR: frozenset({DashboardView.SCHEDULE}),
    }
    gate = RoleGate(source)

    # Mutating the source after construction must not change decisions.
    source[Role.DOCTOR] = frozenset(DashboardView)
    source[Role.ASSISTANT] = frozenset({DashboardView.DECISIONS})

    assert gate.permitted_views(Role.DOCTOR) == frozenset(
        {DashboardView.SCHEDULE}
    )
    assert isinstance(gate.resolve(Role.ASSISTANT), AccessDenied)


def test_default_role_views_covers_every_role() -> None:
    # Guards the invariant relied on by resolve(): every Role is mapped.
    assert set(DEFAULT_ROLE_VIEWS) == set(Role)
