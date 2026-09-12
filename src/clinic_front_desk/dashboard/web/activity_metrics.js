/*
 * CallActivityLog + ImpactMetricsStrip client behaviour (task 13.3, Req 15.2,
 * 15.3, 15.4).
 *
 * These components are server-rendered: Python owns all HTML shaping. This thin
 * vanilla-JS snippet (no build step) does only two things:
 *
 *   1. Real-time refresh (Req 15.4). It subscribes to the Dashboard change
 *      channel (the same WebSocket/SSE stream the BFF fans ChangeEvents out on)
 *      and, when a change relevant to a component arrives, re-fetches that
 *      component's server-rendered partial and swaps it in. Because the swap is
 *      driven by the ChangeEvent itself (no polling), the visible update lands
 *      well inside the 5 s budget.
 *
 *   2. Period selection (Req 15.3). The metrics strip's 7/30/90-day selector
 *      re-fetches the metrics partial for the chosen window and swaps it in.
 *
 * Both refreshes re-fetch the authoritative server-rendered HTML rather than
 * re-implementing rendering here, so there is a single source of truth.
 */
(function () {
  "use strict";

  // ChangeEvent entities that should refresh each component. The activity log
  // reflects call sessions and escalations (Req 15.2, 9.6); a booked/
  // rescheduled/cancelled appointment also alters it. The metrics strip is
  // derived from appointments, call sessions, and gap-fill decisions.
  var ACTIVITY_ENTITIES = ["call_session", "escalation", "appointment"];
  var METRICS_ENTITIES = ["appointment", "call_session", "decision", "waitlist"];

  function swapPartial(el, html) {
    // Replace the component node with the freshly rendered partial. Using the
    // outerHTML swap keeps the root element's data-* wiring in sync.
    var wrapper = document.createElement("div");
    wrapper.innerHTML = html.trim();
    var next = wrapper.firstElementChild;
    if (next && el.parentNode) {
      el.parentNode.replaceChild(next, el);
      return next;
    }
    return el;
  }

  function refetch(el, url) {
    if (!url) {
      return;
    }
    fetch(url, { headers: { Accept: "text/html" }, credentials: "same-origin" })
      .then(function (resp) {
        return resp.ok ? resp.text() : null;
      })
      .then(function (html) {
        if (html != null) {
          var current = document.querySelector('[data-component="' + el.getAttribute("data-component") + '"]');
          if (current) {
            wireComponents(swapPartial(current, html));
          }
        }
      })
      .catch(function () {
        /* transient fetch failure: the next ChangeEvent will retry the refresh */
      });
  }

  function activityEndpoint(el) {
    return el.getAttribute("data-activity-endpoint");
  }

  function metricsEndpoint(el, windowDays) {
    var base = el.getAttribute("data-metrics-endpoint");
    if (!base) {
      return null;
    }
    var sep = base.indexOf("?") === -1 ? "?" : "&";
    return base + sep + "window=" + encodeURIComponent(windowDays);
  }

  function wirePeriodSelector(strip) {
    var selector = strip.querySelector('[data-role="metrics-period-selector"]');
    if (!selector || selector.dataset.wired === "1") {
      return;
    }
    selector.dataset.wired = "1";
    selector.addEventListener("change", function () {
      refetch(strip, metricsEndpoint(strip, selector.value));
    });
  }

  function wireComponents(root) {
    var scope = root && root.querySelectorAll ? root : document;
    var strips = scope.querySelectorAll('[data-component="impact-metrics-strip"]');
    Array.prototype.forEach.call(strips, wirePeriodSelector);
  }

  function relevant(entities, entity) {
    return entities.indexOf(entity) !== -1;
  }

  function onChangeEvent(evt) {
    var entity = evt && evt.entity;
    if (!entity) {
      return;
    }
    var log = document.querySelector('[data-component="call-activity-log"]');
    if (log && relevant(ACTIVITY_ENTITIES, entity)) {
      refetch(log, activityEndpoint(log));
    }
    var strip = document.querySelector('[data-component="impact-metrics-strip"]');
    if (strip && relevant(METRICS_ENTITIES, entity)) {
      var days = strip.getAttribute("data-window-days");
      refetch(strip, metricsEndpoint(strip, days));
    }
  }

  // The Dashboard exposes its change channel to the page as a subscribe hook.
  // We stay transport-agnostic: whatever delivers ChangeEvents (WebSocket/SSE)
  // just needs to call this handler with a { entity, id, kind } object.
  function connect() {
    if (window.DashboardChannel && typeof window.DashboardChannel.subscribe === "function") {
      window.DashboardChannel.subscribe(onChangeEvent);
    }
    document.addEventListener("dashboard:change", function (e) {
      onChangeEvent(e.detail);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      wireComponents(document);
      connect();
    });
  } else {
    wireComponents(document);
    connect();
  }

  // Exposed for testing / manual invocation.
  window.ClinicActivityMetrics = {
    onChangeEvent: onChangeEvent,
    wireComponents: wireComponents,
  };
})();
