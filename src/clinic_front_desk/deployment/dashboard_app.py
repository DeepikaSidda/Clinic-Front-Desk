"""The dashboard web surface: full page + live partial endpoints (Req 14, 15).

The dashboard components in :mod:`clinic_front_desk.dashboard` are pure: each one
renders an HTML partial from a view-model and emits ``data-*-endpoint`` wiring
that its thin vanilla-JS controller re-fetches from. Nothing, until now, actually
*served* those endpoints or assembled the partials into a page — so the UI
existed as components without a running application.

This module is that missing layer. It sits in ``deployment`` rather than
``dashboard`` because it needs the composed
:class:`~clinic_front_desk.deployment.app.ClinicFrontDeskApplication` (one shared
Data_Layer, Req 16.1); keeping it here preserves the dependency direction —
``deployment`` composes ``dashboard``, never the reverse.

Surface (bound to routes in :mod:`clinic_front_desk.deployment.server`):

===========================  ==========================================
``GET /``                    The full role-scoped dashboard page.
``GET /dashboard/schedule``  ScheduleView partial for a provider + day.
``GET /dashboard/activity``  CallActivityLog partial.
``GET /dashboard/metrics``   ImpactMetricsStrip partial for a window.
``GET /dashboard/decisions`` DecisionsFeedView as JSON (the feed's re-fetch).
``POST /dashboard/decisions/{id}/{approve|dismiss}``  Resolve a Decision.
``GET /dashboard/events``    Server-sent ``ChangeEvent`` stream.
``GET /static/{file}``       The stylesheet and controller scripts.
===========================  ==========================================

Every handler is a plain synchronous method returning a string, so the whole
dashboard is testable without an HTTP client. Authorization runs through the
:class:`~clinic_front_desk.dashboard.role_gate.RoleGate` on *every* handler, and a
denial raises before any store read — so a viewer without a role never has data
gathered for them, let alone returned (Req 15.5, 15.7).
"""

from __future__ import annotations

import asyncio
import html
import json
import mimetypes
import os
import re
from collections.abc import AsyncIterator, Mapping
from urllib.parse import quote
from dataclasses import asdict
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from clinic_front_desk.data_layer.events import ChangeEvent

from clinic_front_desk.dashboard.components.activity_and_metrics import (
    DEFAULT_ACTIVITY_ENDPOINT,
    DEFAULT_METRICS_ENDPOINT,
    render_activity_log,
    render_metrics_strip,
)
from clinic_front_desk.dashboard.components.decisions_feed import (
    DEFAULT_DECISIONS_ENDPOINT,
    build_decisions_feed_from_result,
    render_decisions_feed,
)
from clinic_front_desk.dashboard.components.onboarding_wizard import (
    OnboardingWizardViewModel,
)
from clinic_front_desk.dashboard.decisions import (
    DecisionActionResult,
    DecisionActionService,
)
from clinic_front_desk.dashboard.metrics import (
    SUPPORTED_WINDOW_DAYS,
    ImpactMetrics,
    compute_impact_metrics,
)
from clinic_front_desk.dashboard.role_gate import (
    DashboardView,
    Role,
    RoleGate,
)
from clinic_front_desk.dashboard.schedule_view import (
    DEFAULT_SCHEDULE_ENDPOINT,
    build_schedule_view_model_from_result,
    render_schedule_view,
)
from clinic_front_desk.dashboard.shell import render_dashboard_shell
from clinic_front_desk.data_layer.interfaces import ClinicDocumentStore
from clinic_front_desk.models import DecisionStatus, is_err, is_ok

from .app import ClinicFrontDeskApplication

#: Directory holding the partials, stylesheet, and controller scripts.
_WEB_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "web"

#: Endpoint the SSE ``ChangeEvent`` stream is served from.
EVENTS_ENDPOINT = "/dashboard/events"

#: Prefix the stylesheet and controller scripts are served under.
STATIC_PREFIX = "/static/"

#: Static assets the page references. Restricting to an explicit allow-list keeps
#: the static route from becoming an arbitrary-file read.
STATIC_FILES: frozenset[str] = frozenset(
    {
        "dashboard.css",
        "dashboard_bootstrap.js",
        "decisions_feed.js",
        "schedule_view.js",
        "activity_metrics.js",
        "onboarding_wizard.js",
        "voice_client.js",
    }
)

#: Default reporting window for the metrics strip (Req 15.3).
DEFAULT_WINDOW_DAYS = 30

#: Default number of activity-log entries the page shows.
DEFAULT_ACTIVITY_LIMIT = 25

#: How far back to look for a call when serving its transcript. ``CallSessionStore``
#: offers only a recent-first listing, so this bounds that scan.
_CALL_LOOKBACK = 500


def format_sse_event(event: ChangeEvent) -> bytes:
    """Encode a :class:`ChangeEvent` as an SSE ``change`` message.

    The payload shape ``{entity, id, kind}`` is exactly what the client
    controllers expect from ``window.DashboardChannel`` / the ``dashboard:change``
    DOM event, so the wire format and the in-page format are the same object.
    """
    payload = json.dumps(
        {"entity": event.entity.value, "id": event.id, "kind": event.kind.value}
    )
    return f"event: change\ndata: {payload}\n\n".encode()


class ChangeEventStream:
    """Bridges the synchronous :class:`DashboardChannel` to one SSE client.

    The channel fans out *synchronously on the thread that performed the store
    mutation* — often a ``to_thread`` worker, never the event loop. This class is
    the boundary that crosses that thread edge safely: :meth:`offer` is called
    from the mutating thread and hands the event to the loop with
    ``call_soon_threadsafe``, while :meth:`events` is consumed by the response.

    Extracted from the route so the whole stream is testable with plain
    ``asyncio`` — an endless HTTP stream cannot be driven by a blocking test
    client without deadlocking on close.

    Args:
        channel: The shared change channel to subscribe to.
        loop: The running event loop the consumer lives on.
        max_queue: Bound on undelivered events for this client.
        heartbeat_seconds: Idle interval after which a comment frame is emitted.
    """

    def __init__(
        self,
        channel: Any,
        loop: asyncio.AbstractEventLoop,
        *,
        max_queue: int = 1000,
        heartbeat_seconds: float = 15.0,
    ) -> None:
        self._loop = loop
        self._heartbeat = heartbeat_seconds
        self._queue: asyncio.Queue[ChangeEvent] = asyncio.Queue(maxsize=max_queue)
        self.dropped = 0
        self._subscription = channel.subscribe(self._offer_threadsafe)

    def _offer_threadsafe(self, event: ChangeEvent) -> None:
        """Channel subscriber, invoked on the mutating thread."""
        self._loop.call_soon_threadsafe(self._offer, event)

    def _offer(self, event: ChangeEvent) -> None:
        """Enqueue on the loop thread, dropping if this client fell behind.

        Dropping rather than blocking is deliberate: the channel broadcasts
        synchronously inside a store write, so applying backpressure here would
        let one wedged browser tab stall a booking. The server stays
        authoritative, so a dropped frame costs that client one live refresh.
        """
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1

    def close(self) -> None:
        """Detach from the channel; safe to call repeatedly."""
        self._subscription.unsubscribe()

    async def events(self, *, max_frames: int | None = None) -> AsyncIterator[bytes]:
        """Yield SSE frames until the consumer stops (or ``max_frames`` is reached).

        Emits a leading comment so the client's ``open`` handler fires promptly,
        then one ``change`` frame per event, with a comment heartbeat whenever the
        stream is idle. ``max_frames`` bounds the iteration for tests.
        """
        frames = 0
        try:
            yield b": connected\n\n"
            frames += 1
            while max_frames is None or frames < max_frames:
                try:
                    event = await asyncio.wait_for(
                        self._queue.get(), timeout=self._heartbeat
                    )
                except TimeoutError:
                    # Keeps intermediaries from closing an idle connection and
                    # surfaces a vanished client as a write error.
                    yield b": ping\n\n"
                    frames += 1
                    continue
                yield format_sse_event(event)
                frames += 1
        finally:
            self.close()


class DashboardHttpError(Exception):
    """A dashboard request failure carrying the HTTP status to return.

    Kept independent of the AgentCore invocation error type so the dashboard
    layer does not depend on the runtime protocol layer; ``server.py`` maps this
    onto an HTTP response.
    """

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@lru_cache(maxsize=None)
def _read_web_file(name: str) -> str:
    """Read and cache a file from the ``web/`` directory."""
    return (_WEB_DIR / name).read_text(encoding="utf-8")


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


#: Matches an HTML comment. The partials carry substantial developer notes (task
#: numbers, requirement references, placeholder documentation) that are valuable
#: in the source tree but are dead weight — and internal detail — in a page served
#: to a browser, so the assembled page strips them.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

#: The decisions feed embeds its view-model as JSON in this script element. Its
#: contents are deliberately excluded from comment stripping: the renderer escapes
#: ``</`` but *not* ``<!--``, and decision summaries carry patient-influenced text
#: (a patient-named service reaches a Decision summary), so a summary containing
#: ``<!--`` followed later by ``-->`` would otherwise let the strip delete part of
#: the JSON and corrupt the feed's hydration.
_PROTECTED_RE = re.compile(
    r'(<script id="decisions-feed-data".*?</script>)', re.DOTALL
)


def _strip_comments(markup: str) -> str:
    """Remove HTML comments from an assembled page, preserving embedded JSON.

    ``re.split`` with a capturing group interleaves the protected blocks at odd
    indices, so stripping only the even segments leaves the embedded view-model
    byte-for-byte intact.
    """
    segments = _PROTECTED_RE.split(markup)
    return "".join(
        segment if index % 2 else _COMMENT_RE.sub("", segment)
        for index, segment in enumerate(segments)
    )


class DashboardWebApp:
    """Serves the dashboard page, its live partials, and the change stream.

    Args:
        app: The composed application whose shared Data_Layer every read goes
            through (Req 16.1).
        role_gate: Access-control policy (Req 15.5, 15.7).
        now: Injectable UTC clock; overridden in tests for determinism.
    """

    def __init__(
        self,
        app: ClinicFrontDeskApplication,
        *,
        role_gate: RoleGate | None = None,
        now: Any = None,
        sms_sender: Any = None,
    ) -> None:
        self.app = app
        self.role_gate = role_gate or RoleGate()
        self._now = now or (lambda: datetime.now(UTC))
        # Texts a patient whose appointment the clinic cancelled. Defaults to the
        # sender that does nothing and says so, rather than one that silently
        # pretends: an unconfigured deployment must not report patients as notified.
        if sms_sender is None:
            from clinic_front_desk.handover.live import default_region
            from clinic_front_desk.notifications import NullSmsSender, SnsSmsSender

            sms_sender = (
                SnsSmsSender(region=default_region())
                if os.environ.get("CLINIC_SMS_ENABLED", "").strip().lower()
                in {"1", "true", "yes", "on"}
                else NullSmsSender()
            )
        self.sms_sender = sms_sender
        self._decision_actions = DecisionActionService(
            decision_store=app.stores.decisions,
            appointment_store=app.stores.appointments,
            waitlist_store=app.stores.waitlist,
        )

    # -- authorization ------------------------------------------------------

    def require_view(self, role: str | None, view: DashboardView) -> None:
        """Raise unless ``role`` may see ``view`` (Req 15.5, 15.7).

        Called at the top of every handler, before any store read, so a denied
        viewer never has data gathered on their behalf.
        """
        decision = self.role_gate.resolve(role)
        if not decision.granted:
            raise DashboardHttpError(
                "Access denied: no role assigned for this dashboard.",
                status_code=403,
            )
        if view not in decision.permitted_views:
            raise DashboardHttpError(
                f"Access denied: role {role!r} may not view {view.value!r}.",
                status_code=403,
            )

    def permitted(self, role: str | None, view: DashboardView) -> bool:
        """Whether ``role`` may see ``view`` (no raise)."""
        return self.role_gate.is_permitted(role, view)

    # -- clinic context -----------------------------------------------------

    def today(self) -> str:
        """Today's date in UTC as ``YYYY-MM-DD`` (the schedule default, Req 15.1)."""
        return self._now().date().isoformat()

    def _knowledge_base(self) -> Any:
        result = self.app.stores.knowledge_base.get()
        return result.value if is_ok(result) else None

    def provider_ids(self) -> list[str]:
        """Configured Provider ids (empty when the clinic is unconfigured)."""
        kb = self._knowledge_base()
        return [provider.id for provider in kb.providers] if kb else []

    def service_names(self) -> list[str]:
        """Offered service names (used to gather open slots for the schedule)."""
        kb = self._knowledge_base()
        return [service.name for service in kb.services] if kb else []

    def is_configured(self) -> bool:
        """Whether clinic config exists (drives the onboarding redirect, Req 1.1)."""
        kb = self._knowledge_base()
        return bool(kb and kb.services and kb.providers)

    # -- partials -----------------------------------------------------------

    def schedule_partial(
        self,
        role: str | None,
        *,
        provider_id: str | None = None,
        day: str | None = None,
    ) -> str:
        """Render the ScheduleView partial for a provider and day (Req 15.1, 15.6).

        Defaults to the first configured provider and the current day; an explicit
        ``day`` serves the select-another-day path. Open slots are gathered for the
        clinic's offered services, which is the store's only open-slot access path.
        """
        self.require_view(role, DashboardView.SCHEDULE)
        providers = self.provider_ids()
        resolved_provider = provider_id or (providers[0] if providers else "")
        resolved_day = day or self.today()
        if not resolved_provider:
            # Unconfigured clinic: render the empty day rather than erroring, so
            # the region still appears with its controls.
            view_model = build_schedule_view_model_from_result(
                self.app.bff.schedule_for_day("", resolved_day, []),
                provider_id="",
                day=resolved_day,
            )
        else:
            view_model = build_schedule_view_model_from_result(
                self.app.bff.schedule_for_day(
                    resolved_provider, resolved_day, self.service_names()
                ),
                provider_id=resolved_provider,
                day=resolved_day,
                patient_names=self._patient_names(resolved_provider, resolved_day),
            )
        return render_schedule_view(
            view_model, schedule_endpoint=DEFAULT_SCHEDULE_ENDPOINT
        )

    def activity_partial(
        self, role: str | None, *, limit: int = DEFAULT_ACTIVITY_LIMIT
    ) -> str:
        """Render the CallActivityLog partial, most-recent-first (Req 15.2, 9.6)."""
        self.require_view(role, DashboardView.CALL_ACTIVITY)
        if limit < 0:
            raise DashboardHttpError("'limit' must be a non-negative integer.")
        result = self.app.bff.recent_activity(limit)
        entries = result.value if is_ok(result) else []
        return render_activity_log(
            entries, activity_endpoint=DEFAULT_ACTIVITY_ENDPOINT
        )

    def metrics_partial(
        self, role: str | None, *, window_days: int = DEFAULT_WINDOW_DAYS
    ) -> str:
        """Render the ImpactMetricsStrip partial for a window (Req 15.3)."""
        self.require_view(role, DashboardView.IMPACT_METRICS)
        metrics = self.impact_metrics(window_days)
        return render_metrics_strip(metrics, metrics_endpoint=DEFAULT_METRICS_ENDPOINT)

    def impact_metrics(self, window_days: int = DEFAULT_WINDOW_DAYS) -> ImpactMetrics:
        """Compute the impact metrics for ``window_days`` (Req 15.3).

        Sources every input through the shared Data_Layer:

        - **appointments / call sessions** come from
          :meth:`ClinicFrontDeskApplication.assemble_snapshot`, which already
          gathers both through the store interfaces. It is assembled over *twice*
          the window because the no-show-rate trend compares the current period
          against the immediately preceding equal-length one, so the preceding
          period's appointments must be in range too.
        - **recovered decisions** are the *approved* ``gap_fill`` Decisions, read
          via ``DecisionStore.list_by_status``. Approving a Decision moves it out
          of the open feed, so ``list_open`` cannot see them — this is exactly
          what that store method exists for.
        """
        if window_days not in SUPPORTED_WINDOW_DAYS:
            raise DashboardHttpError(
                f"'window' must be one of {sorted(SUPPORTED_WINDOW_DAYS)}."
            )
        now = self._now()
        # Cover the current *and* preceding period so the trend has data.
        snapshot = self.app.assemble_snapshot(
            now.date().isoformat(), window_days=window_days * 2
        )
        approved = self.app.stores.decisions.list_by_status(DecisionStatus.APPROVED)
        return compute_impact_metrics(
            appointments=snapshot.appointments,
            call_sessions=snapshot.call_sessions,
            recovered_decisions=approved.value if is_ok(approved) else [],
            window_days=window_days,
            now=now,
        )

    def call_record_json(self, role: str | None, call_session_id: str) -> str:
        """One call's transcript and a playback URL for its recording, as JSON.

        Part of the call-activity view, so it is gated on that view rather than
        being open: a transcript is the most sensitive thing the dashboard serves.
        The recording is returned as a short-lived playback URL rather than bytes,
        so the audio never passes through this process.
        """
        self.require_view(role, DashboardView.CALL_ACTIVITY)
        sessions = self.app.stores.call_sessions.list_recent(_CALL_LOOKBACK)
        if is_err(sessions):
            raise DashboardHttpError(
                f"failed to read call sessions: {sessions.error.detail}",
                status_code=500,
            )
        session = next(
            (s for s in sessions.value if s.id == call_session_id), None
        )
        if session is None:
            raise DashboardHttpError(
                f"no call session {call_session_id!r}", status_code=404
            )

        # Prefer a real presigned URL, so megabytes of audio go browser → S3
        # directly instead of through this process. Fall back to the streaming
        # route for backends that cannot presign (the in-memory store), which is
        # what makes a local demo playable.
        playback_url: str | None = None
        store = self.app.stores.recordings
        if store is not None and session.recording_uri:
            signed = store.playback_url(call_session_id)
            candidate = signed.value if is_ok(signed) else None
            if candidate and candidate.startswith(("http://", "https://")):
                playback_url = candidate
            else:
                playback_url = self.recording_stream_path(call_session_id)

        return json.dumps(
            {
                "call_session_id": session.id,
                "started_at": session.started_at,
                "ended_at": session.ended_at,
                "outcome": None if session.outcome is None else session.outcome.value,
                "transcript": session.transcript,
                "recording_uri": session.recording_uri,
                "playback_url": playback_url,
            }
        )

    @staticmethod
    def recording_stream_path(call_session_id: str) -> str:
        """Path of the route that streams a recording through this process."""
        return f"/dashboard/calls/{quote(call_session_id, safe='')}/recording"

    def recording_bytes(
        self, role: str | None, call_session_id: str
    ) -> tuple[str, bytes]:
        """Return ``(content_type, audio)`` for a call's recording.

        The fallback playback path for backends that cannot presign. Gated on the
        call-activity view like the transcript, since it is the same information in
        audio form.
        """
        self.require_view(role, DashboardView.CALL_ACTIVITY)
        store = self.app.stores.recordings
        if store is None:
            raise DashboardHttpError("call recording is not enabled", status_code=404)
        result = store.get(call_session_id)
        if is_err(result):
            raise DashboardHttpError(
                f"failed to read the recording: {result.error.detail}", status_code=500
            )
        if result.value is None:
            raise DashboardHttpError(
                f"no recording for call {call_session_id!r}", status_code=404
            )
        return "audio/wav", result.value

    def decisions_feed_json(self, role: str | None) -> str:
        """The DecisionsFeedView as JSON — what the feed controller re-fetches.

        Returned as JSON rather than HTML because ``decisions_feed.js`` hydrates
        and re-renders from the view-model (it owns optimistic removal and
        restore-on-failure, so it needs the data, not markup).
        """
        self.require_view(role, DashboardView.DECISIONS)
        feed = build_decisions_feed_from_result(self.app.bff.open_decisions())
        return json.dumps(asdict(feed))

    # -- decision actions ---------------------------------------------------

    def resolve_decision(
        self, role: str | None, decision_id: str, action: str
    ) -> dict[str, Any]:
        """Approve or dismiss a Decision (Req 14.3, 14.4, 14.6).

        Returns the shape ``decisions_feed.js`` expects: ``{outcome, error}``.
        The controller treats any outcome other than ``approved``/``dismissed`` as
        a failure and restores the optimistically removed card.
        """
        self.require_view(role, DashboardView.DECISIONS)
        if action == "approve":
            result: DecisionActionResult = self._decision_actions.approve(decision_id)
        elif action == "dismiss":
            result = self._decision_actions.dismiss(decision_id)
        else:
            raise DashboardHttpError(
                f"Unknown decision action {action!r}; expected 'approve' or 'dismiss'."
            )
        return {
            "decision_id": result.decision_id,
            "outcome": result.outcome.value,
            "error": result.error,
        }

    # -- the page -----------------------------------------------------------

    def page(
        self,
        role: str | None,
        *,
        window_days: int = DEFAULT_WINDOW_DAYS,
        activity_limit: int = DEFAULT_ACTIVITY_LIMIT,
    ) -> str:
        """Render the full role-scoped dashboard page.

        Renders the shell (which decides *which* regions exist, Req 15.5/15.7)
        then fills each permitted region with its component's partial, so the
        page arrives complete — no loading spinners, no client-side fetch waterfall
        on first paint. The controller scripts then take over for live updates.

        A viewer with no role gets the shell's access-denied state and no region
        is filled, because none was emitted.
        """
        shell = render_dashboard_shell(role, gate=self.role_gate)
        shell = self._inject_head(shell, role)

        # Fill each emitted region's mount point with its rendered partial. The
        # mount ids come from the shell (dashboard-view-<view>), so a region that
        # the gate did not emit simply has nothing to fill.
        fillers: list[tuple[DashboardView, str]] = []
        if self.permitted(role, DashboardView.SCHEDULE):
            fillers.append((DashboardView.SCHEDULE, self.schedule_partial(role)))
        if self.permitted(role, DashboardView.CALL_ACTIVITY):
            fillers.append(
                (
                    DashboardView.CALL_ACTIVITY,
                    self.activity_partial(role, limit=activity_limit),
                )
            )
        if self.permitted(role, DashboardView.IMPACT_METRICS):
            fillers.append(
                (
                    DashboardView.IMPACT_METRICS,
                    self.metrics_partial(role, window_days=window_days),
                )
            )
        if self.permitted(role, DashboardView.DECISIONS):
            fillers.append((DashboardView.DECISIONS, self._decisions_partial(role)))

        for view, partial in fillers:
            shell = self._fill_region(shell, view, partial)
        return _strip_comments(shell)

    def _decisions_partial(self, role: str | None) -> str:
        """Render the DecisionsFeed partial (Req 14.1, 14.2, 14.7)."""
        self.require_view(role, DashboardView.DECISIONS)
        feed = build_decisions_feed_from_result(self.app.bff.open_decisions())
        return render_decisions_feed(
            feed, decisions_endpoint=DEFAULT_DECISIONS_ENDPOINT
        )

    @staticmethod
    def _fill_region(shell: str, view: DashboardView, partial: str) -> str:
        """Insert ``partial`` into the shell region for ``view``.

        The shell emits, per permitted view,
        ``<section id="dashboard-view-<view>" ...><div data-role="view-mount">``.
        We locate that section by its id and replace the *first* empty mount div
        inside it, which is unambiguous because each section has exactly one.
        """
        mount_id = f"dashboard-view-{view.value.replace('_', '-')}"
        anchor = f'id="{mount_id}"'
        start = shell.find(anchor)
        if start == -1:
            return shell
        empty_mount = '<div class="dashboard-shell__view-body" data-role="view-mount"></div>'
        mount_at = shell.find(empty_mount, start)
        if mount_at == -1:
            return shell
        filled = (
            '<div class="dashboard-shell__view-body" data-role="view-mount">'
            f"{partial}</div>"
        )
        return shell[:mount_at] + filled + shell[mount_at + len(empty_mount) :]

    def _inject_head(self, shell: str, role: str | None) -> str:
        """Add the stylesheet, app bar, and controller scripts to the shell page.

        The shell partial is deliberately minimal (it is unit-tested for its
        role-gating behaviour, not its chrome), so the page-level concerns —
        stylesheet, top bar, script tags — are layered on here rather than baked
        into the tested template.
        """
        head = (
            f'<link rel="stylesheet" href="{STATIC_PREFIX}dashboard.css" />\n'
            '<meta name="color-scheme" content="light dark" />\n'
            f'<meta name="clinic-role" content="{_esc(role or "")}" />\n'
            f'<meta name="clinic-events-endpoint" content="{EVENTS_ENDPOINT}" />\n'
        )
        shell = shell.replace("</head>", f"{head}  </head>", 1)

        scripts = "".join(
            f'<script src="{STATIC_PREFIX}{name}" defer></script>\n    '
            for name in (
                "decisions_feed.js",
                "schedule_view.js",
                "activity_metrics.js",
                # The bootstrap wires the controllers to the live endpoints, so it
                # must load after them. `defer` preserves document order.
                "dashboard_bootstrap.js",
            )
        )
        shell = shell.replace("</body>", f"  {scripts}</body>", 1)
        return shell.replace(
            '<main class="dashboard-shell"', f"{self._app_bar(role)}\n    <main class=\"dashboard-shell\"", 1
        )

    def _app_bar(self, role: str | None) -> str:
        """The top chrome: brand, clinic status, role badge, live indicator."""
        role_label = (role or "no role").replace("_", " ")
        configured = (
            "Accepting calls" if self.is_configured() else "Not yet accepting calls"
        )
        # Only shown to a role that may actually use it, so the bar never offers a
        # link that answers 403.
        documents_link = (
            f'<a class="app-bar__link" href="/documents?role={_esc(role or "")}">'
            "Documents</a>"
            if self.permitted(role, DashboardView.DOCUMENTS)
            else ""
        )
        slots_link = (
            f'<a class="app-bar__link" href="/slots?role={_esc(role or "")}">Slots</a>'
            if self.permitted(role, DashboardView.SCHEDULE)
            else ""
        )
        return (
            '<header class="app-bar">'
            '<span class="app-bar__brand">'
            '<span class="app-bar__mark" aria-hidden="true">CF</span>'
            "Clinic Front Desk</span>"
            '<span class="app-bar__spacer"></span>'
            '<span class="app-bar__meta">'
            f"{slots_link}"
            f"{documents_link}"
            f'<span class="app-bar__clinic-status">{_esc(configured)}</span>'
            f'<span class="app-bar__role">{_esc(role_label)}</span>'
            '<span class="app-bar__status" data-role="connection-status" '
            'data-state="connecting" role="status">Connecting</span>'
            "</span>"
            "</header>"
        )

    # -- onboarding ---------------------------------------------------------

    def onboarding_page(self, view_model: OnboardingWizardViewModel | None = None) -> str:
        """Render the onboarding wizard page with the dashboard stylesheet.

        Presented on first access when no configuration exists (Req 1.1). The
        wizard's own renderer owns the form body and its per-field errors; this
        only attaches the stylesheet so it is not unstyled.
        """
        from clinic_front_desk.dashboard.components.onboarding_wizard import (
            build_view_model,
            render_html,
        )

        model = view_model or build_view_model(self.app.stores.knowledge_base)
        page = render_html(model)
        return page.replace(
            "</head>",
            f'  <link rel="stylesheet" href="{STATIC_PREFIX}dashboard.css" />\n  </head>',
            1,
        ).replace(
            'src="onboarding_wizard.js"', f'src="{STATIC_PREFIX}onboarding_wizard.js"'
        )

    def submit_onboarding(self, form: Mapping[str, str]) -> str:
        """Validate and save a submitted onboarding form, then render the result.

        The wizard template has always posted here; until now only ``GET`` was
        routed, so a real submission 405'd and the form could not be saved at all
        over HTTP. Deliberately ungated, like the ``GET``: onboarding is what runs
        *before* a clinic exists, so there is no configured practice to hold a role
        against, and gating it would lock the first user out of setup.
        """
        from clinic_front_desk.dashboard.components.onboarding_wizard import (
            handle_submit,
        )

        view = handle_submit(self.app.stores.knowledge_base, form)
        return self.onboarding_page(view)

    # -- appointment slots --------------------------------------------------

    def day_schedule_page(
        self,
        role: str | None,
        *,
        day: str | None = None,
        provider_id: str | None = None,
        message: str | None = None,
        error: str | None = None,
    ) -> str:
        """Render the doctor's day calendar (Req 15.1, 15.6).

        Gated on the schedule view, the same permission that governs seeing the
        day's appointments — publishing availability is managing that same
        calendar.
        """
        from clinic_front_desk.dashboard.components.day_schedule import (
            build_day_schedule_view_model,
            render_day_schedule_page,
        )

        self.require_view(role, DashboardView.SCHEDULE)
        providers = self.provider_ids()
        resolved_day = day or self.today()
        resolved_provider = provider_id or (providers[0] if providers else "")
        view = build_day_schedule_view_model(
            self.app.stores.appointments,
            day=resolved_day,
            provider_id=resolved_provider,
            providers=providers,
            services=self.service_names(),
            message=message,
            error=error,
            role=role,
            holders=self._slot_holders(resolved_provider, resolved_day),
        )
        page = render_day_schedule_page(view)
        return page.replace(
            "</head>",
            f'  <link rel="stylesheet" href="{STATIC_PREFIX}dashboard.css" />\n  </head>',
            1,
        )

    def publish_slots(
        self,
        role: str | None,
        *,
        day: str,
        provider_id: str,
        service: str,
        minutes: str = "",
        start: str = "",
        end: str = "",
        until: str = "",
        skip_closed: bool = True,
    ) -> tuple[str | None, str | None]:
        """Publish slots for one day, or every day through ``until``.

        Args:
            until: Inclusive last date. Empty publishes ``day`` alone.
            skip_closed: Skip weekdays the clinic has no configured hours for, so
                publishing a year does not open the days the clinic is shut.

        A malformed window is the doctor's to correct on the same page, so it comes
        back as an error string rather than an HTTP failure. Only a denied role is
        an HTTP error.
        """
        from clinic_front_desk.scheduling import (
            DAY_END,
            DAY_START,
            DEFAULT_SLOT_MINUTES,
            SlotGenerationError,
            generate_range_slots,
        )

        self.require_view(role, DashboardView.SCHEDULE)

        try:
            length = int(minutes) if minutes else DEFAULT_SLOT_MINUTES
        except ValueError:
            return None, f"Slot length must be a number, got {minutes!r}."

        if service not in self.service_names():
            # Slots must be bookable for a service the clinic actually offers, or
            # the agent could never match a caller's request to them.
            return None, f"{service!r} is not one of the clinic's offered services."

        last_day = until.strip() or day
        try:
            slots = generate_range_slots(
                day,
                last_day,
                provider_id,
                service,
                minutes=length,
                start=start or DAY_START,
                end=end or DAY_END,
                open_weekdays=self.open_weekdays() if skip_closed else None,
            )
        except SlotGenerationError as exc:
            return None, str(exc)

        result = self.app.stores.appointments.add_slots(slots)
        if is_err(result):
            raise DashboardHttpError(
                f"failed to publish slots: {result.error.detail}", status_code=500
            )

        written = len(result.value)
        skipped = len(slots) - written
        days = len({slot.start[:10] for slot in slots})
        span = f"{day}" if last_day == day else f"{day} to {last_day}"
        detail = (
            f"Published {written} slots of {length} minutes across {days} "
            f"day{'' if days == 1 else 's'} ({span})."
        )
        if skipped:
            detail += (
                f" {skipped} already-booked or blocked slot"
                f"{'' if skipped == 1 else 's'} were left as they are."
            )
        return detail, None

    def _patient_names(self, provider_id: str, day: str) -> dict[str, str]:
        """``patient id -> name`` for everyone booked on that day.

        The schedule region used to print the raw patient id, so a doctor's day
        read as a column of hex. Resolved here rather than in the view-model
        builder, which is pure by contract, and per render rather than copied onto
        the appointment, so correcting a misheard name fixes every row at once.

        A lookup that fails leaves that patient out and the row falls back to the
        id: a schedule showing the time is taken is worth more than no schedule.
        """
        if not provider_id:
            return {}
        booked = self.app.bff.schedule_for_day(provider_id, day, self.service_names())
        if is_err(booked):
            return {}

        names: dict[str, str] = {}
        for appointment in booked.value.appointments:
            patient_id = getattr(appointment, "patient_id", "")
            if not patient_id or patient_id in names:
                continue
            found = self.app.stores.patients.get(patient_id)
            if is_ok(found) and found.value is not None and found.value.name:
                names[patient_id] = found.value.name
        return names

    def _slot_holders(self, provider_id: str, day: str) -> dict[str, tuple[str, str]]:
        """``slot_id -> (patient name, patient id)`` for the day's booked slots.

        Names are resolved here rather than stored on the slot, so a patient who
        corrects their name is not left with an old one printed across their
        appointments. Failures degrade to an unnamed booked slot: the doctor still
        sees the time is taken, which is the part that must never be wrong.
        """
        if not provider_id:
            return {}
        booked = self.app.bff.schedule_for_day(provider_id, day, self.service_names())
        if is_err(booked):
            return {}

        holders: dict[str, tuple[str, str]] = {}
        for appointment in booked.value.appointments:
            slot_id = getattr(appointment, "slot_id", "")
            patient_id = getattr(appointment, "patient_id", "")
            if not slot_id:
                continue
            name = ""
            if patient_id:
                found = self.app.stores.patients.get(patient_id)
                if is_ok(found) and found.value is not None:
                    name = found.value.name
            holders[slot_id] = (name or patient_id, patient_id)
        return holders

    def patient_detail_page(
        self,
        role: str | None,
        patient_id: str,
        *,
        day: str | None = None,
        provider_id: str | None = None,
        message: str | None = None,
        error: str | None = None,
    ) -> str:
        """Render one patient's record, including the intake details (doctor only).

        Gated on the Decisions view, which only the doctor holds, rather than on
        the schedule view the assistant shares. Knowing that the 10:30 slot is
        taken is front-desk work; reading that patient's blood group and weight is
        not, and role-scoping it is the only thing that keeps that line.
        """
        from clinic_front_desk.dashboard.components.day_schedule import (
            PatientDetailViewModel,
            render_patient_detail_page,
        )

        self.require_view(role, DashboardView.DECISIONS)

        from clinic_front_desk.tools.patients import BLOOD_GROUPS

        view = PatientDetailViewModel(
            patient_id=patient_id,
            back_day=day or "",
            back_provider=provider_id or "",
            role=role,
            message=message,
            error=error,
            blood_group_choices=sorted(BLOOD_GROUPS),
        )

        found = self.app.stores.patients.get(patient_id)
        if is_err(found):
            view.error = f"Could not read the patient record: {found.error.detail}"
        elif found.value is not None:  # noqa: SIM102
            patient = found.value
            view.name = patient.name
            view.callback_phone = patient.callback_phone
            view.code = patient.code
            view.age = patient.age
            view.blood_group = patient.blood_group
            view.weight_kg = patient.weight_kg
            view.height_cm = patient.height_cm
            view.created_at = patient.created_at

            # Their appointments on the day the doctor came from. The store has no
            # per-patient appointment index, and adding one for this page would be
            # a new access path across both backends — so this answers the question
            # actually being asked ("what is this person here for") from a read
            # that already exists.
            if day and provider_id:
                scheduled = self.app.bff.schedule_for_day(
                    provider_id, day, self.service_names()
                )
                if is_ok(scheduled):
                    view.appointments = [
                        (day, a.time[:5], a.service)
                        for a in sorted(
                            scheduled.value.appointments, key=lambda a: a.time
                        )
                        if getattr(a, "patient_id", "") == patient_id
                    ]

        page = render_patient_detail_page(view)
        return page.replace(
            "</head>",
            f'  <link rel="stylesheet" href="{STATIC_PREFIX}dashboard.css" />\n  </head>',
            1,
        )

    def update_patient(
        self,
        role: str | None,
        patient_id: str,
        *,
        name: str,
        callback_phone: str,
        age: str = "",
        blood_group: str = "",
        weight_kg: str = "",
        height_cm: str = "",
    ) -> tuple[str | None, str | None]:
        """Correct a patient record from the doctor's page (doctor only).

        This exists because a phone line mishears names. A caller who said "Sidda
        Deepika" was recorded as "siddha devika", and with no way to amend the
        record the clinic was stuck with a name that would never match the ID she
        brings to reception. It is the safety net for every other transcription
        error too.

        Validation differs deliberately from the phone path. There, an implausible
        measurement is dropped in silence, because the agent cannot reliably hold a
        clarifying dialogue and a wrong number in a record reads as fact. Here the
        doctor is looking at the screen, so an out-of-range value is *refused with
        a reason* and nothing is written — dropping it quietly would leave them
        believing they had corrected something they had not.

        A cleared box clears that field. A form showing its current contents ought
        to mean what it shows, and a doctor deleting a wrong weight is asking for
        it to be removed rather than ignored.

        Gated on the Decisions view, the same doctor-only permission that governs
        *seeing* this record: the assistant cannot read a blood group, so it cannot
        rewrite one either.
        """
        from clinic_front_desk.tools.patients import (
            AGE_RANGE,
            BLOOD_GROUPS,
            HEIGHT_CM_RANGE,
            WEIGHT_KG_RANGE,
            normalize_blood_group,
        )

        self.require_view(role, DashboardView.DECISIONS)

        cleaned_name = name.strip()
        if not cleaned_name:
            return None, "A name is required — it is how the clinic identifies them."
        cleaned_phone = callback_phone.strip()
        if not cleaned_phone:
            return None, "A mobile number is required, so the clinic can call back."

        def number(raw: str, label: str, bounds: tuple[float, float]) -> float | None:
            text = raw.strip()
            if not text:
                return None
            try:
                value = float(text)
            except ValueError:
                raise DashboardHttpError(f"{label} must be a number, got {raw!r}.")
            low, high = bounds
            if not (low <= value <= high):
                raise DashboardHttpError(
                    f"{label} of {value:g} is outside the plausible range "
                    f"{low:g} to {high:g}. Nothing was saved."
                )
            return value

        try:
            parsed_age = number("" if not age.strip() else age, "Age", AGE_RANGE)
            parsed_weight = number(weight_kg, "Weight", WEIGHT_KG_RANGE)
            parsed_height = number(height_cm, "Height", HEIGHT_CM_RANGE)
        except DashboardHttpError as exc:
            return None, exc.message

        group: str | None = None
        if blood_group.strip():
            group = normalize_blood_group(blood_group)
            if group is None:
                return None, (
                    f"{blood_group!r} is not a blood group this clinic records. "
                    f"Choose one of {', '.join(sorted(BLOOD_GROUPS))}."
                )

        found = self.app.stores.patients.get(patient_id)
        if is_err(found):
            raise DashboardHttpError(
                f"failed to read the patient: {found.error.detail}", status_code=500
            )
        patient = found.value
        if patient is None:
            raise DashboardHttpError(f"no patient {patient_id!r}", status_code=404)

        changed = [
            label
            for label, before, after in (
                ("name", patient.name, cleaned_name),
                ("mobile", patient.callback_phone, cleaned_phone),
                ("age", patient.age, None if parsed_age is None else int(parsed_age)),
                ("blood group", patient.blood_group, group),
                ("weight", patient.weight_kg, parsed_weight),
                ("height", patient.height_cm, parsed_height),
            )
            if before != after
        ]
        if not changed:
            return "Nothing to change — the record already reads that way.", None

        patient.name = cleaned_name
        patient.callback_phone = cleaned_phone
        patient.age = None if parsed_age is None else int(parsed_age)
        patient.blood_group = group
        patient.weight_kg = parsed_weight
        patient.height_cm = parsed_height

        saved = self.app.stores.patients.update(patient)
        if is_err(saved):
            raise DashboardHttpError(
                f"failed to save the patient: {saved.error.detail}", status_code=500
            )
        return f"Updated {', '.join(changed)}.", None

    def open_weekdays(self) -> frozenset[int]:
        """Weekday indices the clinic has configured hours for (0 = Sunday).

        Used to skip closed days when publishing a range, so a clinic shut on
        Sundays does not spend the year offering Sunday appointments. Falls back to
        every day when no hours are configured — better to publish a day too many
        than to silently publish nothing.
        """
        kb = self._knowledge_base()
        if kb is None or not kb.hours:
            return frozenset(range(7))
        configured = frozenset(
            day for day, hours in kb.hours.items() if hours is not None
        )
        return configured or frozenset(range(7))

    def _appointment_id_for_slot(
        self, *, slot_id: str, day: str, provider_id: str
    ) -> str:
        """The booked appointment holding ``slot_id``, or ``""``.

        The day view knows slots, not appointments — a cell carries a ``slot_id``
        because that is what the calendar is made of. Resolving here keeps the page's
        view model unchanged and means the button can only ever cancel the booking
        that actually holds the time the doctor clicked.
        """
        if not (slot_id and day and provider_id):
            return ""
        listed = self.app.stores.appointments.list_by_provider_and_day(provider_id, day)
        if is_err(listed):
            return ""
        for appointment in listed.value:
            if getattr(appointment, "slot_id", "") == slot_id:
                return str(appointment.id)
        return ""

    def cancel_appointment(
        self,
        role: str | None,
        *,
        appointment_id: str = "",
        slot_id: str = "",
        day: str = "",
        provider_id: str = "",
    ) -> tuple[str | None, str | None]:
        """Cancel a booked appointment and tell the patient, returning (message, error).

        The deliberate act that ``set_slot_block`` refuses to do implicitly. Blocking
        a booked slot is rejected precisely so that freeing that time has to come
        through here, where the patient is notified.

        The order matters and is the whole point. The cancellation is written first,
        then the patient is texted. A failed text must never leave the clinic thinking
        a slot is still taken — but a successful cancellation with a failed text must
        be **visible**, because a doctor who assumes the patient was told will not ring
        them, and a patient who was not told arrives to a locked door.
        """
        self.require_view(role, DashboardView.SCHEDULE)

        if not appointment_id:
            appointment_id = self._appointment_id_for_slot(
                slot_id=slot_id, day=day, provider_id=provider_id
            )
        if not appointment_id:
            return None, "No appointment found for that slot."

        found = self.app.stores.appointments.get(appointment_id)
        if is_err(found) or found.value is None:
            return None, "That appointment no longer exists."
        appointment = found.value

        # Read the patient before cancelling: the name and number are what the text
        # needs, and they are easier to reach while the appointment is still whole.
        patient_name = ""
        patient_phone = ""
        if appointment.patient_id:
            patient = self.app.stores.patients.get(appointment.patient_id)
            if is_ok(patient) and patient.value is not None:
                patient_name = patient.value.name
                patient_phone = patient.value.callback_phone

        removed = self.app.stores.appointments.remove(appointment_id)
        if is_err(removed):
            return None, f"Could not cancel: {removed.error.detail}"

        told = self._notify_cancelled(
            appointment=appointment,
            patient_name=patient_name,
            patient_phone=patient_phone,
        )
        freed = f"{appointment.date} at {appointment.time}"
        return f"Cancelled {appointment.service} on {freed}. {told}", None

    def _notify_cancelled(
        self, *, appointment: Any, patient_name: str, patient_phone: str
    ) -> str:
        """Text the patient, and say plainly what happened either way.

        Returns a sentence for the doctor, not a boolean, because "not texted" is only
        actionable if she knows she has to ring them.
        """
        from clinic_front_desk.notifications import cancellation_message

        if not patient_phone:
            return "No mobile number on file — please contact them directly."

        contact = ""
        config = self.app.stores.knowledge_base.get()
        if is_ok(config) and config.value is not None:
            contact = config.value.contact_phone or ""

        body = cancellation_message(
            patient_name=patient_name,
            service=appointment.service,
            date=appointment.date,
            time=appointment.time,
            clinic_phone=contact,
        )
        outcome = self.sms_sender.send(patient_phone, body)
        if outcome.sent:
            return f"The patient has been texted on {outcome.to}."
        return (
            f"NOT texted ({outcome.detail}) — please ring {patient_phone} yourself."
        )

    def set_slot_block(
        self,
        role: str | None,
        *,
        day: str,
        provider_id: str,
        blocked: bool,
        slot_id: str = "",
        start: str = "",
        end: str = "",
    ) -> tuple[str | None, str | None]:
        """Block or reopen slots so the agent stops (or resumes) offering them.

        Blocking is how the doctor takes time off the calendar — surgery, lunch,
        leave — without deleting the day. A blocked slot leaves the offerable set
        immediately, because ``list_open_slots`` returns only ``open`` slots.

        Either one ``slot_id`` or a ``start``/``end`` range. A range is what makes
        this usable: blocking a lunch hour out of a 48-slot day one button at a
        time is not a feature anyone would use.

        **A booked slot is never touched.** Blocking it would leave a patient
        holding an appointment on time the calendar says is unavailable, with no
        record that anything changed. Freeing that time means cancelling their
        appointment, which is a deliberate, separate act.
        """
        from clinic_front_desk.models import SlotStatus

        self.require_view(role, DashboardView.SCHEDULE)

        result = self.app.stores.appointments.list_slots_for_day(provider_id, day)
        if is_err(result):
            raise DashboardHttpError(
                f"failed to read the day: {result.error.detail}", status_code=500
            )
        slots = result.value

        if slot_id:
            targets = [slot for slot in slots if slot.id == slot_id]
            if not targets:
                raise DashboardHttpError(f"no slot {slot_id!r}", status_code=404)
        elif start and end:
            if end <= start:
                return None, f"The range {start}-{end} is empty."
            targets = [
                slot
                for slot in slots
                if start <= slot.start.partition("T")[2][:5] < end
            ]
            if not targets:
                return None, f"No slots fall between {start} and {end}."
        else:
            return None, "Choose a slot, or a time range to block."

        wanted = SlotStatus.BLOCKED if blocked else SlotStatus.OPEN
        booked = sum(1 for slot in targets if slot.status == SlotStatus.BOOKED)
        to_change = [
            slot
            for slot in targets
            if slot.status != SlotStatus.BOOKED and slot.status != wanted
        ]

        # One batched write for the whole range. Per-slot updates resolved each id
        # by scanning the table, so closing the overnight hours on a single 48-slot
        # day meant 26 scans, and doing it across a published quarter meant 2,500.
        if to_change:
            updated = self.app.stores.appointments.set_slot_statuses(to_change, wanted)
            if is_err(updated):
                raise DashboardHttpError(
                    f"failed to update {len(to_change)} slots: {updated.error.detail}",
                    status_code=500,
                )
        changed = len(to_change)

        verb = "Blocked" if blocked else "Reopened"
        if not changed and booked:
            return None, (
                f"Nothing changed: {booked} of those slots are booked. Cancel the "
                "appointment first to free that time."
            )
        detail = f"{verb} {changed} slot" + ("" if changed == 1 else "s") + "."
        if booked:
            detail += (
                f" {booked} booked slot" + ("" if booked == 1 else "s") +
                " left alone — cancel the appointment to free that time."
            )
        return detail, None

    # -- clinic documents ---------------------------------------------------

    def _document_store(self) -> ClinicDocumentStore | None:
        """The document store, or ``None`` when uploads are not configured."""
        return self.app.stores.documents

    def _require_documents(self, role: str | None) -> ClinicDocumentStore:
        """Gate on the documents view and return a configured store, or raise."""
        self.require_view(role, DashboardView.DOCUMENTS)
        store = self._document_store()
        if store is None:
            raise DashboardHttpError(
                "document uploads are not enabled", status_code=404
            )
        return store

    def documents_page(
        self,
        role: str | None,
        *,
        message: str | None = None,
        error: str | None = None,
    ) -> str:
        """Render the clinic-documents page (doctor only).

        Gated but tolerant of an unconfigured store: the page explains how to
        enable uploads rather than 404ing, because "not switched on" is a setup
        state the doctor can act on, not a missing resource.
        """
        from clinic_front_desk.dashboard.components.document_library import (
            build_document_library_view_model,
            render_document_library_page,
        )

        self.require_view(role, DashboardView.DOCUMENTS)
        view = build_document_library_view_model(
            self._document_store(),
            retrieval_enabled=self.app.stores.embedder is not None,
            message=message,
            error=error,
            role=role,
        )
        page = render_document_library_page(view)
        return page.replace(
            "</head>",
            f'  <link rel="stylesheet" href="{STATIC_PREFIX}dashboard.css" />\n  </head>',
            1,
        )

    def upload_document(
        self, role: str | None, data: bytes, *, filename: str, content_type: str = ""
    ) -> tuple[str | None, str | None]:
        """Ingest an uploaded document, returning ``(message, error)``.

        Returns both rather than raising for a bad file: an unreadable upload is
        the doctor's mistake to correct on the same page, not an HTTP failure. Only
        a missing store or a denied role is an HTTP error.
        """
        from clinic_front_desk.documents import ingest_document

        store = self._require_documents(role)
        if not data:
            return None, "No file was received. Please choose a file and try again."

        result = ingest_document(
            store,
            data,
            filename=filename,
            content_type=content_type,
            embedder=self.app.stores.embedder,
        )
        if not result.ok:
            return None, f"{filename}: {result.error}"

        if self.app.stores.embedder is None:
            return (
                f"Stored {filename}. It is not searchable yet — no embedding model "
                "is configured.",
                None,
            )
        if result.partially_embedded:
            return (
                f"Stored {filename}, but only {result.chunks_embedded} of "
                f"{result.chunks_stored} passages are searchable. Uploading it "
                "again may fix the rest.",
                None,
            )
        return (
            f"Uploaded {filename}: {result.chunks_stored} passages the agent can "
            "now answer from.",
            None,
        )

    def delete_document(self, role: str | None, document_id: str) -> str:
        """Delete an uploaded document, returning a confirmation message."""
        store = self._require_documents(role)
        result = store.delete(document_id)
        if is_err(result):
            raise DashboardHttpError(
                f"failed to delete the document: {result.error.detail}",
                status_code=500,
            )
        return "Document deleted. The agent will no longer answer from it."

    def document_original(
        self, role: str | None, document_id: str
    ) -> tuple[str, bytes, str]:
        """Return ``(media_type, data, filename)`` for a document download."""
        store = self._require_documents(role)
        result = store.get_original(document_id)
        if is_err(result):
            raise DashboardHttpError(
                f"failed to read the document: {result.error.detail}", status_code=500
            )
        if result.value is None:
            raise DashboardHttpError(
                f"no document {document_id!r}", status_code=404
            )

        filename = document_id
        listed = store.list_documents()
        if is_ok(listed):
            for document in listed.value:
                if document.id == document_id:
                    filename = document.filename
                    break
        media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return media_type, result.value, filename

    def extract_config_page(self, role: str | None, document_id: str) -> str:
        """Read clinic details out of a document and render the pre-filled wizard.

        Renders the wizard with the values *proposed*; it saves nothing. The doctor
        reviewing and submitting is what persists them, through the same validation
        as typed input — a model's reading of a PDF must not silently become what
        the agent quotes to callers.
        """
        from clinic_front_desk.dashboard.components.document_library import (
            build_document_library_view_model,
            render_document_library_page,
        )
        from clinic_front_desk.dashboard.components.onboarding_wizard import (
            view_model_from_candidate,
        )
        from clinic_front_desk.documents import extract_clinic_config
        from clinic_front_desk.documents.text import DocumentExtractionError, extract_text

        store = self._require_documents(role)
        extractor = self.app.stores.config_extractor
        if extractor is None:
            raise DashboardHttpError(
                "reading clinic details from a document is not enabled",
                status_code=404,
            )

        original = store.get_original(document_id)
        if is_err(original):
            raise DashboardHttpError(
                f"failed to read the document: {original.error.detail}", status_code=500
            )
        if original.value is None:
            raise DashboardHttpError(f"no document {document_id!r}", status_code=404)

        filename = document_id
        listed = store.list_documents()
        if is_ok(listed):
            for document in listed.value:
                if document.id == document_id:
                    filename = document.filename
                    break

        try:
            text = extract_text(original.value, filename=filename)
        except DocumentExtractionError as exc:
            return self._documents_error(role, f"{filename}: {exc}")

        extracted = extract_clinic_config(
            text.full_text, extractor, source_label=filename
        )
        if not extracted.ok:
            # Nothing usable found: say so on the documents page rather than
            # showing an empty form that looks like a failed read of their data.
            message = extracted.error or "no clinic details could be found"
            return self._documents_error(role, f"{filename}: {message}")

        view = view_model_from_candidate(
            extracted.candidate,
            notes=extracted.notes,
            source_filename=filename,
        )
        return self.onboarding_page(view)

    def _documents_error(self, role: str | None, error: str) -> str:
        """Re-render the documents page carrying an error."""
        return self.documents_page(role, error=error)

    # -- voice client -------------------------------------------------------

    @staticmethod
    def voice_client_page() -> str:
        """The browser voice client: talk to the agent with a real microphone.

        Served as a page of its own rather than inside the dashboard because it is
        a *caller's* view, not a doctor's — it deliberately carries no clinic data
        and so needs no role. It connects to the same ``/ws`` transport AgentCore
        uses, so what you exercise here is the real Voice_Front_Desk: Nova Sonic
        speech-to-speech, the ten tools bound to the live Data_Layer, the
        guardrail, and Call_Session persistence.
        """
        return _read_web_file("voice_client.html")

    # -- static assets ------------------------------------------------------

    @staticmethod
    def static_asset(name: str) -> tuple[str, str]:
        """Return ``(media_type, body)`` for an allow-listed static file.

        The allow-list is the security boundary: without it, this route would be
        an arbitrary-file read rooted at the package directory.
        """
        if name not in STATIC_FILES:
            raise DashboardHttpError(f"Unknown static asset {name!r}.", status_code=404)
        media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.endswith(".js"):
            # Some environments map .js to text/plain, which browsers refuse.
            media_type = "text/javascript"
        return media_type, _read_web_file(name)

    # -- request-parameter helpers -----------------------------------------

    @staticmethod
    def parse_window(params: Mapping[str, str]) -> int:
        """Parse the metrics ``window`` query parameter (Req 15.3)."""
        raw = params.get("window")
        if raw is None or raw == "":
            return DEFAULT_WINDOW_DAYS
        try:
            window = int(raw)
        except ValueError:
            raise DashboardHttpError(f"'window' must be an integer, got {raw!r}.") from None
        if window not in SUPPORTED_WINDOW_DAYS:
            raise DashboardHttpError(
                f"'window' must be one of {sorted(SUPPORTED_WINDOW_DAYS)}."
            )
        return window

    @staticmethod
    def parse_limit(params: Mapping[str, str]) -> int:
        """Parse the activity-log ``limit`` query parameter."""
        raw = params.get("limit")
        if raw is None or raw == "":
            return DEFAULT_ACTIVITY_LIMIT
        try:
            limit = int(raw)
        except ValueError:
            raise DashboardHttpError(f"'limit' must be an integer, got {raw!r}.") from None
        if limit < 0:
            raise DashboardHttpError("'limit' must be a non-negative integer.")
        return limit

    @staticmethod
    def resolve_role(
        params: Mapping[str, str], header_role: str | None
    ) -> str | None:
        """Resolve the viewer's role from the header, falling back to the query.

        The header is what an authenticating proxy sets from the verified
        identity, so it wins. The ``?role=`` fallback exists for local runs and
        demos where no auth layer is in front; it is not a security control —
        access is still decided by the ``RoleGate``, and every role it accepts is
        one the gate knows.
        """
        return header_role or params.get("role") or None


def demo_roles() -> list[str]:
    """The roles a local/demo run can switch between (for the role picker)."""
    return [role.value for role in Role]


__all__ = [
    "EVENTS_ENDPOINT",
    "STATIC_PREFIX",
    "STATIC_FILES",
    "DEFAULT_WINDOW_DAYS",
    "DEFAULT_ACTIVITY_LIMIT",
    "DashboardHttpError",
    "DashboardWebApp",
    "demo_roles",
]
