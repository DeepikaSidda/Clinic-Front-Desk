/*
 * ScheduleView client behaviour (task 13.2, Req 15.1, 15.4, 15.6).
 *
 * The schedule view is server-rendered: Python owns all HTML shaping
 * (clinic_front_desk.dashboard.schedule_view.render_schedule_view). This thin
 * vanilla-JS controller (no build step) does only three things:
 *
 *   1. Default to the current day (Req 15.1). On load, if the day picker has no
 *      value it is set to today, matching the "current day by default" default
 *      the doctor sees when opening the view.
 *
 *   2. Real-time refresh (Req 15.4). It subscribes to the Dashboard change
 *      channel (the same WebSocket/SSE stream the BFF fans ChangeEvents out on)
 *      and, on an appointment/slot ChangeEvent, re-fetches the server-rendered
 *      partial for the currently displayed day and swaps it in. Because the swap
 *      is driven by the ChangeEvent itself (no polling), the visible update
 *      lands well inside the 5 s budget.
 *
 *   3. Day selection (Req 15.6). Picking a date, or using Previous/Today/Next,
 *      re-fetches the partial for that day and swaps it in — a single network
 *      round-trip, well inside the 2 s budget.
 *
 * All refreshes re-fetch the authoritative server-rendered HTML rather than
 * re-implementing rendering here, so there is a single source of truth.
 */
(function () {
  "use strict";

  // ChangeEvent entities that should refresh the schedule view. A booked,
  // rescheduled, or cancelled appointment alters the day's appointments and its
  // open slots (a freed/consumed slot), so both entities trigger a refresh
  // (Req 15.4).
  var SCHEDULE_ENTITIES = ["appointment", "slot"];

  var COMPONENT = "schedule-view";

  function todayISO() {
    // Local calendar day in YYYY-MM-DD, matching the <input type="date"> format.
    var now = new Date();
    var y = now.getFullYear();
    var m = String(now.getMonth() + 1).padStart(2, "0");
    var d = String(now.getDate()).padStart(2, "0");
    return y + "-" + m + "-" + d;
  }

  function shiftDay(iso, deltaDays) {
    // Shift a YYYY-MM-DD string by whole days using UTC math so DST never nudges
    // the calendar date.
    var parts = String(iso).split("-");
    var dt = new Date(
      Date.UTC(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]))
    );
    if (isNaN(dt.getTime())) {
      return iso;
    }
    dt.setUTCDate(dt.getUTCDate() + deltaDays);
    return dt.toISOString().slice(0, 10);
  }

  function scheduleEndpoint(el, providerId, day) {
    var base = el.getAttribute("data-schedule-endpoint");
    if (!base) {
      return null;
    }
    var sep = base.indexOf("?") === -1 ? "?" : "&";
    var query = [];
    if (providerId) {
      query.push("provider_id=" + encodeURIComponent(providerId));
    }
    if (day) {
      query.push("day=" + encodeURIComponent(day));
    }
    return query.length ? base + sep + query.join("&") : base;
  }

  function currentDay(el) {
    return el.getAttribute("data-day");
  }

  function providerId(el) {
    return el.getAttribute("data-provider-id");
  }

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

  function refetch(el, day) {
    var url = scheduleEndpoint(el, providerId(el), day);
    if (!url) {
      return;
    }
    fetch(url, { headers: { Accept: "text/html" }, credentials: "same-origin" })
      .then(function (resp) {
        return resp.ok ? resp.text() : null;
      })
      .then(function (html) {
        if (html != null) {
          var current = document.querySelector(
            '[data-component="' + COMPONENT + '"]'
          );
          if (current) {
            wire(swapPartial(current, html));
          }
        }
      })
      .catch(function () {
        /* transient fetch failure: the next ChangeEvent / action will retry */
      });
  }

  function goToDay(el, day) {
    if (!day) {
      return;
    }
    // Optimistically reflect the target day so subsequent shifts are relative to
    // it even before the refetched partial swaps in.
    el.setAttribute("data-day", day);
    refetch(el, day);
  }

  function wire(root) {
    var el =
      root && root.getAttribute && root.getAttribute("data-component") === COMPONENT
        ? root
        : (root || document).querySelector('[data-component="' + COMPONENT + '"]');
    if (!el || el.dataset.wired === "1") {
      return;
    }
    el.dataset.wired = "1";

    // Default the day picker to today when the server did not fix a day
    // (Req 15.1 "current day by default").
    var picker = el.querySelector('[data-role="schedule-day-picker"]');
    if (picker && !picker.value) {
      picker.value = currentDay(el) || todayISO();
    }

    if (picker) {
      picker.addEventListener("change", function () {
        goToDay(el, picker.value);
      });
    }

    var prev = el.querySelector('[data-role="schedule-prev-day"]');
    if (prev) {
      prev.addEventListener("click", function () {
        goToDay(el, shiftDay(currentDay(el) || todayISO(), -1));
      });
    }

    var next = el.querySelector('[data-role="schedule-next-day"]');
    if (next) {
      next.addEventListener("click", function () {
        goToDay(el, shiftDay(currentDay(el) || todayISO(), 1));
      });
    }

    var today = el.querySelector('[data-role="schedule-today"]');
    if (today) {
      today.addEventListener("click", function () {
        goToDay(el, todayISO());
      });
    }
  }

  function relevant(entity) {
    return SCHEDULE_ENTITIES.indexOf(entity) !== -1;
  }

  function onChangeEvent(evt) {
    var entity = evt && evt.entity;
    if (!entity || !relevant(entity)) {
      return;
    }
    var el = document.querySelector('[data-component="' + COMPONENT + '"]');
    if (el) {
      // Refresh the day currently in view (Req 15.4).
      refetch(el, currentDay(el));
    }
  }

  // The Dashboard exposes its change channel to the page as a subscribe hook.
  // We stay transport-agnostic: whatever delivers ChangeEvents (WebSocket/SSE)
  // just needs to call this handler with a { entity, id, kind } object.
  function connect() {
    if (
      window.DashboardChannel &&
      typeof window.DashboardChannel.subscribe === "function"
    ) {
      window.DashboardChannel.subscribe(onChangeEvent);
    }
    document.addEventListener("dashboard:change", function (e) {
      onChangeEvent(e.detail);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      wire(document);
      connect();
    });
  } else {
    wire(document);
    connect();
  }

  // Exposed for testing / manual invocation.
  window.ClinicScheduleView = {
    onChangeEvent: onChangeEvent,
    wire: wire,
    shiftDay: shiftDay,
    todayISO: todayISO,
  };
})();
