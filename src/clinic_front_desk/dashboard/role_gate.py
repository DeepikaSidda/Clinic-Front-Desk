"""Role-scoped access control for the Dashboard (task 12.4, Req 15.5, 15.7).

The dashboard is a *role-aware* web app (design "Dashboard"). Before any
schedule, call-activity, or impact-metrics data is shaped for a viewer, the
``RoleGate`` decides which views that viewer may see:

- **Req 15.5** — *where a viewer is assigned a role*, the dashboard presents
  only the views permitted for that role.
- **Req 15.7** — *if a viewer has no assigned role*, access is denied and **no**
  schedule, call-activity, or metrics data is returned.

The gate is expressed as a **pure, deterministic** class over an immutable
role→views permission map: given the same viewer role it always returns the same
:class:`AccessDecision`, with no I/O and no hidden state. This is exactly what
Property 23 (task 12.5) exercises — for any assigned role the presented set of
views equals the permitted set, and for any viewer without an assigned role
access is denied and no data is returned.

The gate never reads or holds the underlying data itself; the BFF calls
:meth:`RoleGate.resolve` (or :meth:`RoleGate.filter_data`) to learn which views
are allowed and only *then* reads the corresponding stores. :meth:`filter_data`
makes the "return no data when denied" guarantee mechanical: a denied viewer
yields an empty mapping regardless of what was offered.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar, Literal, TypeVar

T = TypeVar("T")


class Role(StrEnum):
    """A viewer role the dashboard recognizes (design "Dashboard").

    The demo clinic is a solo practice: the :attr:`DOCTOR` is the primary
    viewer, with an optional single :attr:`ASSISTANT`. A viewer with no role at
    all is represented by ``None`` (not a member here) and is denied under
    Req 15.7.
    """

    DOCTOR = "doctor"
    ASSISTANT = "assistant"


class DashboardView(StrEnum):
    """A presentable dashboard view whose access is role-scoped.

    The three views named by Req 15.5 (:attr:`SCHEDULE`, :attr:`CALL_ACTIVITY`,
    :attr:`IMPACT_METRICS`) plus the doctor-facing :attr:`DECISIONS` feed
    (Req 14, design "Dashboard"). These are the identifiers a caller uses to key
    the data payloads passed to :meth:`RoleGate.filter_data`.
    """

    SCHEDULE = "schedule"
    CALL_ACTIVITY = "call_activity"
    IMPACT_METRICS = "impact_metrics"
    DECISIONS = "decisions"
    #: Managing the uploaded documents the agent answers callers from. Not a
    #: dashboard region (it is absent from ``VIEW_ORDER``) but a settings page of
    #: its own, because uploading is clinic setup like onboarding rather than
    #: something to watch during the day.
    DOCUMENTS = "documents"


# Default role → permitted-view mapping (design "Dashboard": doctor view is
# primary, with an optional assistant). The doctor sees every view; the
# assistant sees the operational schedule and call-activity views but not the
# doctor-facing Decisions feed or the business impact-metrics strip. Every Role
# member MUST appear here so that an assigned-but-unmapped role can be told
# apart from a genuinely unknown role (see :meth:`RoleGate.resolve`).
DEFAULT_ROLE_VIEWS: Mapping[Role, frozenset[DashboardView]] = {
    Role.DOCTOR: frozenset(DashboardView),
    # The assistant's set is enumerated rather than subtracted, so a view added
    # later is not silently granted to them. Notably DOCUMENTS is withheld:
    # uploads become what the agent tells callers about the practice, which is the
    # practice owner's decision to make.
    Role.ASSISTANT: frozenset(
        {DashboardView.SCHEDULE, DashboardView.CALL_ACTIVITY}
    ),
}


@dataclass(frozen=True)
class AccessGranted:
    """Access is granted; ``permitted_views`` is exactly the allowed set (15.5).

    Attributes:
        role: The resolved role the decision was made for.
        permitted_views: The exact set of views this role may see. May be empty
            if a role is configured with no views, but access itself is still
            granted (the viewer *has* a role).
    """

    role: Role
    permitted_views: frozenset[DashboardView]
    granted: ClassVar[Literal[True]] = True


@dataclass(frozen=True)
class AccessDenied:
    """Access is denied; no view is permitted and no data may be returned (15.7).

    ``permitted_views`` is always empty so callers can treat granted/denied
    uniformly (both expose ``permitted_views``) while a denied viewer can never
    accidentally be shown data.

    Attributes:
        reason: A stable machine-readable reason for the denial
            (``"no_role_assigned"`` or ``"unknown_role"``).
    """

    reason: str
    permitted_views: frozenset[DashboardView] = field(default=frozenset())
    granted: ClassVar[Literal[False]] = False


# An access decision is either a grant or a denial. Both carry ``permitted_views``
# and a class-level ``granted`` flag so callers can branch on ``decision.granted``
# or narrow via ``isinstance``.
AccessDecision = AccessGranted | AccessDenied


class RoleGate:
    """Pure role-scoped access control for the dashboard (Req 15.5, 15.7).

    Constructed with an immutable role→permitted-views map (defaulting to
    :data:`DEFAULT_ROLE_VIEWS`); its methods perform no I/O and hold no mutable
    state, so every call is deterministic in its inputs.
    """

    def __init__(
        self,
        role_views: Mapping[Role, frozenset[DashboardView]] = DEFAULT_ROLE_VIEWS,
    ) -> None:
        # Defensively copy into frozensets so the gate is immutable regardless
        # of what the caller passes (and so later mutation of the argument can't
        # change access decisions).
        self._role_views: dict[Role, frozenset[DashboardView]] = {
            role: frozenset(views) for role, views in role_views.items()
        }

    @staticmethod
    def _coerce_role(role: Role | str | None) -> Role | None:
        """Normalize an incoming role to a :class:`Role`, or ``None``.

        Accepts a :class:`Role`, its string value, or ``None``. Any value that
        is not a recognized role string returns ``None`` so it is treated as an
        unrecognized role by :meth:`resolve` rather than raising.
        """
        if role is None or isinstance(role, Role):
            return role
        try:
            return Role(role)
        except ValueError:
            return None

    def resolve(self, role: Role | str | None) -> AccessDecision:
        """Decide access for a viewer's role.

        - ``None`` (no assigned role) → :class:`AccessDenied` with reason
          ``"no_role_assigned"`` and no permitted views (Req 15.7).
        - A role string that is not a recognized :class:`Role` → denied with
          reason ``"unknown_role"`` (also unassigned in practice).
        - A recognized role not present in the permission map → denied with
          reason ``"unknown_role"`` (no views configured for it).
        - A recognized, mapped role → :class:`AccessGranted` with exactly the
          views permitted for that role (Req 15.5).
        """
        if role is None:
            return AccessDenied(reason="no_role_assigned")
        coerced = self._coerce_role(role)
        if coerced is None:
            return AccessDenied(reason="unknown_role")
        views = self._role_views.get(coerced)
        if views is None:
            return AccessDenied(reason="unknown_role")
        return AccessGranted(role=coerced, permitted_views=views)

    def permitted_views(
        self, role: Role | str | None
    ) -> frozenset[DashboardView]:
        """Return the set of views the role may see (empty when denied).

        Convenience over :meth:`resolve` for callers that only need the view
        set; a denied viewer always yields an empty set (Req 15.7).
        """
        return self.resolve(role).permitted_views

    def is_permitted(
        self, role: Role | str | None, view: DashboardView
    ) -> bool:
        """Return ``True`` iff ``role`` is permitted to see ``view``."""
        return view in self.permitted_views(role)

    def filter_data(
        self,
        role: Role | str | None,
        data: Mapping[DashboardView, T],
    ) -> dict[DashboardView, T]:
        """Keep only the data payloads for views this role may see.

        Given a mapping of view → payload (e.g. schedule/activity/metrics data
        the BFF is about to return), returns a new mapping containing only the
        permitted views. A denied viewer — including any viewer without an
        assigned role — yields an empty mapping, guaranteeing no schedule,
        call-activity, or metrics data is returned (Req 15.7). Does not mutate
        the input.
        """
        permitted = self.permitted_views(role)
        return {
            view: payload
            for view, payload in data.items()
            if view in permitted
        }


__all__ = [
    "Role",
    "DashboardView",
    "DEFAULT_ROLE_VIEWS",
    "AccessGranted",
    "AccessDenied",
    "AccessDecision",
    "RoleGate",
]
