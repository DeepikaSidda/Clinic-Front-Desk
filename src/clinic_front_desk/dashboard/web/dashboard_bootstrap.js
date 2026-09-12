/*
 * Dashboard bootstrap: connects the server-rendered page to the live backend.
 *
 * The three component controllers in this directory are deliberately transport-
 * agnostic. schedule_view.js and activity_metrics.js self-wire on DOMContentLoaded
 * and re-fetch their own partials, but both look for a change-event source
 * (window.DashboardChannel or a `dashboard:change` DOM event) that nothing
 * provided. decisions_feed.js is a factory needing an injected `deps` object and
 * was never instantiated. This file supplies both halves:
 *
 *   1. A `window.DashboardChannel` backed by EventSource over the BFF's
 *      server-sent ChangeEvent stream, re-broadcast as `dashboard:change` DOM
 *      events so every controller sees the same feed regardless of which hook it
 *      chose. Reconnects with capped exponential backoff and reflects the
 *      connection state in the app bar.
 *
 *   2. `createDecisionsFeed(root, deps)` instantiated against the real endpoints:
 *      fetchOpenDecisions -> GET /dashboard/decisions,
 *      resolveDecision    -> POST /dashboard/decisions/{id}/{approve|dismiss},
 *      subscribe          -> the channel above.
 *
 * The viewer's role travels on every request (X-Clinic-Role, from the page's
 * meta tag) so the server's RoleGate decides authorization identically for the
 * page and for each subsequent partial fetch.
 *
 * No build step, no framework, no dependencies.
 */
(function (global) {
  "use strict";

  var doc = global.document;

  // --- page context ------------------------------------------------------

  function meta(name, fallback) {
    var el = doc.querySelector('meta[name="' + name + '"]');
    var value = el && el.getAttribute("content");
    return value ? value : fallback;
  }

  var ROLE = meta("clinic-role", "");
  var EVENTS_URL = meta("clinic-events-endpoint", "/dashboard/events");

  function headers(extra) {
    var h = extra || {};
    if (ROLE) h["X-Clinic-Role"] = ROLE;
    return h;
  }

  // The controllers build their own partial URLs from data-*-endpoint
  // attributes and fetch them directly, so they cannot add the role header
  // themselves. Wrapping fetch is the least invasive way to attach it to every
  // same-origin dashboard request without editing those tested files.
  var nativeFetch = global.fetch && global.fetch.bind(global);
  if (nativeFetch && ROLE) {
    global.fetch = function (input, init) {
      var options = init || {};
      var url = typeof input === "string" ? input : input && input.url;
      if (url && url.indexOf("/dashboard/") !== -1) {
        var merged = {};
        for (var key in options) {
          if (Object.prototype.hasOwnProperty.call(options, key)) {
            merged[key] = options[key];
          }
        }
        var h = new Headers(options.headers || {});
        if (!h.has("X-Clinic-Role")) h.set("X-Clinic-Role", ROLE);
        merged.headers = h;
        return nativeFetch(input, merged);
      }
      return nativeFetch(input, options);
    };
  }

  // --- connection status indicator ---------------------------------------

  function setStatus(state, label) {
    var el = doc.querySelector('[data-role="connection-status"]');
    if (!el) return;
    el.setAttribute("data-state", state);
    el.textContent = label;
  }

  // --- change-event channel over SSE -------------------------------------

  function createChannel(url) {
    var handlers = [];
    var source = null;
    var attempt = 0;
    var closed = false;

    function dispatch(event) {
      // Re-broadcast as a DOM event so controllers wired to `dashboard:change`
      // receive it too. Both hooks are fed, so ordering between them is stable.
      handlers.forEach(function (handler) {
        try {
          handler(event);
        } catch (err) {
          // One faulty subscriber must not stop delivery to the others —
          // mirroring the server channel's delivery-isolation contract.
          if (global.console) global.console.error("change handler failed", err);
        }
      });
      doc.dispatchEvent(new CustomEvent("dashboard:change", { detail: event }));
    }

    function connect() {
      if (closed || !global.EventSource) {
        if (!global.EventSource) setStatus("offline", "Live updates unsupported");
        return;
      }
      setStatus(attempt === 0 ? "connecting" : "reconnecting", "Connecting");
      source = new global.EventSource(url, { withCredentials: true });

      source.addEventListener("open", function () {
        attempt = 0;
        setStatus("live", "Live");
      });

      source.addEventListener("change", function (message) {
        var payload = null;
        try {
          payload = JSON.parse(message.data);
        } catch (err) {
          return;
        }
        if (payload && payload.entity) dispatch(payload);
      });

      source.addEventListener("error", function () {
        // EventSource retries on its own, but only for transient network drops;
        // an explicitly closed stream needs a manual retry with backoff so a
        // restarted server is picked up without a page reload.
        if (source) source.close();
        source = null;
        if (closed) return;
        attempt += 1;
        var delay = Math.min(1000 * Math.pow(2, attempt - 1), 30000);
        setStatus("reconnecting", "Reconnecting");
        global.setTimeout(connect, delay);
      });
    }

    connect();

    return {
      subscribe: function (handler) {
        handlers.push(handler);
        return function unsubscribe() {
          var i = handlers.indexOf(handler);
          if (i !== -1) handlers.splice(i, 1);
        };
      },
      close: function () {
        closed = true;
        if (source) source.close();
      },
      // Exposed so a test (or a manual console call) can inject an event.
      dispatch: dispatch,
    };
  }

  // --- decisions feed deps ------------------------------------------------

  function decisionsEndpoint(root) {
    return root.getAttribute("data-decisions-endpoint") || "/dashboard/decisions";
  }

  function createDecisionDeps(root, channel) {
    var base = decisionsEndpoint(root);
    return {
      fetchOpenDecisions: function () {
        return fetch(base, {
          headers: headers({ Accept: "application/json" }),
          credentials: "same-origin",
        }).then(function (resp) {
          if (!resp.ok) throw new Error("Could not load decisions");
          return resp.json();
        });
      },
      resolveDecision: function (id, action) {
        return fetch(
          base + "/" + encodeURIComponent(id) + "/" + encodeURIComponent(action),
          {
            method: "POST",
            headers: headers({ Accept: "application/json" }),
            credentials: "same-origin",
          }
        ).then(function (resp) {
          // A non-2xx still carries {outcome, error}; surface the message so the
          // restored card explains itself rather than showing a generic failure.
          return resp
            .json()
            .catch(function () {
              return { outcome: "store_error", error: "Request failed" };
            })
            .then(function (body) {
              if (!resp.ok && !body.error) {
                body.error = "Request failed (" + resp.status + ")";
              }
              return body;
            });
        });
      },
      subscribe: channel.subscribe,
    };
  }

  // --- theme toggle persistence ------------------------------------------

  function initTheme() {
    var stored = null;
    try {
      stored = global.localStorage.getItem("clinic-theme");
    } catch (err) {
      /* storage unavailable (private mode): fall back to the OS preference */
    }
    if (stored) doc.documentElement.setAttribute("data-theme", stored);
  }

  // --- boot ---------------------------------------------------------------

  function boot() {
    initTheme();

    var channel = createChannel(EVENTS_URL);
    // Published before the controllers' own connect() runs is not guaranteed
    // (all scripts are deferred, order is document order and this is last), so
    // the DOM-event path above is what actually reaches them. Publishing here
    // keeps the documented window.DashboardChannel hook available too.
    global.DashboardChannel = channel;

    var feedRoot = doc.querySelector('[data-component="decisions-feed"]');
    if (feedRoot && typeof global.createDecisionsFeed === "function") {
      global.ClinicDecisionsFeed = global.createDecisionsFeed(
        feedRoot,
        createDecisionDeps(feedRoot, channel)
      );
    }

    global.addEventListener("beforeunload", function () {
      channel.close();
    });
  }

  if (doc.readyState === "loading") {
    doc.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  global.ClinicDashboard = {
    boot: boot,
    createChannel: createChannel,
    createDecisionDeps: createDecisionDeps,
  };
})(typeof window !== "undefined" ? window : this);
