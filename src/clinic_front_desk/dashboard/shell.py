"""Dashboard shell: role-gated composition of the dashboard views (task 13.5).

The dashboard is a single role-aware SPA (design "Dashboard"). Its *shell* is the
outer page that composes the individual view components — the schedule view, the
call-activity log, the impact-metrics strip, and the doctor-facing decisions feed
— into one screen. Task 13.5 enforces, in the client/UI layer, the two
role-scoping rules the :class:`~clinic_front_desk.dashboard.role_gate.RoleGate`
already decides:

- **Req 15.5** — *where a viewer is assigned a role*, the shell renders **only**
  the views permitted for that role (schedule, call-activity, impact-metrics),
  never the rest.
- **Req 15.7** — *if a viewer has no assigned role*, the shell renders an
  access-denied state and **no** schedule, call-activity, or metrics regions at
  all — there is nothing for those data payloads to mount into.

This module is a thin, **pure** presentation layer over ``RoleGate``: it holds no
data and performs no I/O beyond reading the sibling HTML partial. It never re-
implements the access decision; it asks the gate (via
:meth:`RoleGate.permitted_views`) which views are allowed and shapes exactly
those into a view-model, then fills ``web/dashboard_shell.html``. Because the
denied state produces an empty ``visible_views`` tuple, a viewer without a role
can never have a data region rendered for them — the "return no data when
denied" guarantee is mechanical, mirroring
:meth:`RoleGate.filter_data` on the BFF side.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from clinic_front_desk.dashboard.role_gate import (
    AccessDenied,
    DashboardView,
    Role,
    RoleGate,
)

# Directory holding the HTML partial this module renders (sibling ``web/``).
_WEB_DIR = Path(__file__).resolve().parent / "web"

#: Canonical left-to-right order the shell presents views in, and the human
#: label for each. Rendering follows this order (filtered to the permitted set)
#: so a role's visible views are always laid out deterministically regardless of
#: the permission map's iteration order.
VIEW_ORDER: tuple[DashboardView, ...] = (
    DashboardView.SCHEDULE,
    DashboardView.CALL_ACTIVITY,
    DashboardView.IMPACT_METRICS,
    DashboardView.DECISIONS,
)

#: Human-readable heading for each dashboard view region.
VIEW_LABELS: dict[DashboardView, str] = {
    DashboardView.SCHEDULE: "Schedule",
    DashboardView.CALL_ACTIVITY: "Call activity",
    DashboardView.IMPACT_METRICS: "Impact metrics",
    DashboardView.DECISIONS: "Decisions to make",
}

#: Shown in the access-denied state (Req 15.7). Deliberately generic: it names no
#: data because a denied viewer is shown none.
ACCESS_DENIED_MESSAGE = (
    "Access denied. You do not have a role assigned for this dashboard. "
    "Ask the practice owner to grant you access."
)

#: Token in the shell partial replaced with the rendered shell body.
_SHELL_BODY_TOKEN = "{{SHELL_BODY}}"


@dataclass(frozen=True)
class DashboardViewRegion:
    """One render-ready view region the shell will present (Req 15.5).

    Attributes:
        view: The dashboard view this region hosts.
        label: The human-readable heading for the region.
        mount_id: Stable DOM id the corresponding component mounts into.
    """

    view: DashboardView
    label: str
    mount_id: str


@dataclass(frozen=True)
class DashboardShellViewModel:
    """The dashboard shell's view-model (Req 15.5, 15.7).

    Pure data describing *what the shell should render* for a given viewer role,
    with the access decision already resolved by the gate.

    Attributes:
        role: The resolved role's value when access is granted, else ``None``.
        granted: ``True`` when the viewer has access; ``False`` when denied.
        denied_reason: The gate's machine-readable denial reason when denied
            (e.g. ``"no_role_assigned"``), else ``None``.
        visible_views: The views the shell will render, in :data:`VIEW_ORDER`.
            Always empty when access is denied (Req 15.7).
        regions: One :class:`DashboardViewRegion` per visible view, in order.
        access_denied_message: The message shown in the denied state.
    """

    role: str | None
    granted: bool
    denied_reason: str | None
    visible_views: tuple[DashboardView, ...]
    regions: tuple[DashboardViewRegion, ...]
    access_denied_message: str = ACCESS_DENIED_MESSAGE

    @property
    def denied(self) -> bool:
        """``True`` when access is denied (convenience inverse of ``granted``)."""
        return not self.granted


#: Matches an HTML comment (non-greedy, spanning newlines).
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


@lru_cache(maxsize=None)
def _load_template(name: str) -> str:
    """Read and cache an HTML partial from the ``web/`` directory, comments removed.

    Stripping comments is **correctness**, not just payload trimming: the shell
    template's own documentation comment mentions the ``{{SHELL_BODY}}`` token by
    name, and :meth:`str.replace` substitutes *every* occurrence — so leaving the
    comment in place emitted the entire role-scoped body twice, the first copy
    inside the comment. Anything that then post-processed the rendered shell (for
    example filling each view region with its component's partial) would target
    the commented copy and leave the visible regions empty.

    Removing comments before substitution guarantees exactly one token, and keeps
    internal task/requirement notes out of the page sent to a browser.
    """
    raw = (_WEB_DIR / name).read_text(encoding="utf-8")
    return _COMMENT_RE.sub("", raw)


def build_dashboard_shell_view_model(
    role: Role | str | None,
    *,
    gate: RoleGate | None = None,
) -> DashboardShellViewModel:
    """Compute the shell view-model for a viewer's role (Req 15.5, 15.7).

    Delegates the access decision entirely to ``gate`` (a default
    :class:`RoleGate` when none is given) so this layer never re-implements
    role-scoping. When the gate grants access, ``visible_views`` is exactly the
    permitted set laid out in :data:`VIEW_ORDER` (Req 15.5). When the gate denies
    access — including any viewer without an assigned role — ``granted`` is
    ``False`` and ``visible_views``/``regions`` are empty, so no schedule,
    call-activity, or metrics region is produced (Req 15.7).

    Pure and deterministic: no I/O, no clock read, no mutation of inputs.
    """
    gate = gate if gate is not None else RoleGate()
    decision = gate.resolve(role)
    permitted = decision.permitted_views
    visible = tuple(view for view in VIEW_ORDER if view in permitted)
    regions = tuple(
        DashboardViewRegion(
            view=view,
            label=VIEW_LABELS[view],
            mount_id=f"dashboard-view-{view.value.replace('_', '-')}",
        )
        for view in visible
    )
    denied_reason = (
        decision.reason if isinstance(decision, AccessDenied) else None
    )
    return DashboardShellViewModel(
        role=None if not decision.granted else decision.role.value,
        granted=decision.granted,
        denied_reason=denied_reason,
        visible_views=visible,
        regions=regions,
    )


def _render_region(region: DashboardViewRegion) -> str:
    """Render one empty, gated view region the component JS mounts into.

    The region carries the view id and mount id but no data: the shell only
    decides *whether* a region exists (the role gate), while the individual
    components (rendered by other tasks) fill it.
    """
    return (
        '<section class="dashboard-shell__view" '
        f'data-view="{html.escape(region.view.value, quote=True)}" '
        f'id="{html.escape(region.mount_id, quote=True)}" '
        f'aria-label="{html.escape(region.label, quote=True)}">'
        f'<h2 class="dashboard-shell__view-title">{html.escape(region.label)}</h2>'
        '<div class="dashboard-shell__view-body" data-role="view-mount"></div>'
        "</section>"
    )


def _render_denied(view_model: DashboardShellViewModel) -> str:
    """Render the access-denied state with no data regions (Req 15.7)."""
    reason = view_model.denied_reason or "access_denied"
    return (
        '<section class="dashboard-shell__denied" '
        'data-role="access-denied" role="alert" '
        f'data-denied-reason="{html.escape(reason, quote=True)}">'
        '<h2 class="dashboard-shell__denied-title">Access denied</h2>'
        '<p class="dashboard-shell__denied-message">'
        f"{html.escape(view_model.access_denied_message)}</p>"
        "</section>"
    )


def render_dashboard_shell(
    role: Role | str | None,
    *,
    gate: RoleGate | None = None,
) -> str:
    """Render the dashboard shell to an HTML string (Req 15.5, 15.7).

    Builds the view-model and fills ``web/dashboard_shell.html``. When access is
    granted, the shell body is one region per permitted view in
    :data:`VIEW_ORDER` (Req 15.5); when access is denied, the body is the
    access-denied state alone and no view regions are emitted (Req 15.7).
    """
    view_model = build_dashboard_shell_view_model(role, gate=gate)
    if view_model.granted:
        body = "\n      ".join(
            _render_region(region) for region in view_model.regions
        )
    else:
        body = _render_denied(view_model)
    template = _load_template("dashboard_shell.html")
    return template.replace(_SHELL_BODY_TOKEN, body)


__all__ = [
    "VIEW_ORDER",
    "VIEW_LABELS",
    "ACCESS_DENIED_MESSAGE",
    "DashboardViewRegion",
    "DashboardShellViewModel",
    "build_dashboard_shell_view_model",
    "render_dashboard_shell",
]
