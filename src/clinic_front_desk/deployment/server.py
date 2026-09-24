"""The AgentCore Runtime HTTP/WebSocket server (task 14.1, Req 1.8, 16.1).

This is the deployable container surface. It turns the framework-agnostic
:class:`~clinic_front_desk.deployment.app.ClinicFrontDeskApplication` into an
ASGI app that satisfies the documented Amazon Bedrock AgentCore Runtime HTTP
protocol contract, so one container serves both agents:

===============  ========  =======================================================
Path             Method    Purpose
===============  ========  =======================================================
``/ping``        GET       Health probe. ``Healthy`` / ``HealthyBusy`` while a
                           call or an analysis run is in flight.
``/invocations`` POST      JSON request/response surface: the scheduled
                           Practice_Intelligence run plus the role-gated
                           Dashboard BFF reads and decision approve/dismiss.
``/ws``          WS        The Voice_Front_Desk bidirectional transport — patient
                           audio in, Nova Sonic audio out, one Call_Session per
                           connection.
===============  ========  =======================================================

Both the HTTP and WebSocket endpoints live on **port 8080** of a single ARM64
container, which is what the runtime contract requires and what lets the reactive
voice agent and the autonomous intelligence agent share **one** Data_Layer and
**one** :class:`~clinic_front_desk.dashboard.pubsub.DashboardChannel` (Req 16.1).

Security boundary
-----------------
This server performs **no authentication of its own** — that is deliberate and
matches the runtime contract: AgentCore Runtime terminates SigV4 or OAuth *in
front of* the container and only forwards authorized requests, so the container
must not be exposed directly to the internet. What this server *does* enforce is
**authorization**: every dashboard read and decision action is passed through the
:class:`~clinic_front_desk.dashboard.role_gate.RoleGate`, so a caller without an
assigned role receives ``403`` and **no** schedule, activity, or metrics data
(Req 15.5, 15.7). The caller's role arrives in the request body (``role``) or the
``X-Clinic-Role`` header, which the authenticating layer in front is responsible
for setting from the verified identity.

Session identity
----------------
AgentCore sends ``X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`` on both the
``/invocations`` POST and the ``/ws`` upgrade. It is used as the Call_Session id
so a voice call's persisted :class:`~clinic_front_desk.models.CallSession`
correlates with the runtime session.

Dependencies
------------
Starlette (ASGI + WebSocket) and uvicorn are in the ``deploy`` extra, so the core
agent, Data_Layer, and dashboard logic stay installable without a web framework::

    pip install -e ".[voice,deploy]"
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import enum
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from clinic_front_desk.dashboard.decisions import DecisionActionService
from clinic_front_desk.dashboard.role_gate import DashboardView, RoleGate
from clinic_front_desk.dashboard.shell import render_dashboard_shell
from clinic_front_desk.data_layer.events import ChangeEvent
from clinic_front_desk.models import CallOutcome, is_err
from clinic_front_desk.handover.live import LiveCallRegistry, LiveHandoverService
from clinic_front_desk.voice.clinic_briefing import build_clinic_card
from clinic_front_desk.voice.recording import CallRecorder

from .app import ClinicFrontDeskApplication
from .dashboard_app import (
    EVENTS_ENDPOINT,
    STATIC_PREFIX,
    ChangeEventStream,
    DashboardHttpError,
    DashboardWebApp,
)
from .runtime import (
    RuntimeConfig,
    build_runtime_application,
    build_scheduled_intelligence_entrypoint,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.websockets import WebSocket

logger = logging.getLogger(__name__)

__all__ = [
    "PING_HEALTHY",
    "PING_HEALTHY_BUSY",
    "SESSION_ID_HEADER",
    "ROLE_HEADER",
    "InvocationError",
    "AgentCoreServer",
    "create_asgi_app",
    "runtime_config_from_env",
    "build_asgi_app_from_env",
    "main",
]

#: How often the SSE stream emits a comment heartbeat. Keeps intermediaries from
#: closing an idle connection and lets the server notice a vanished client.
SSE_HEARTBEAT_SECONDS = 15.0


# ---------------------------------------------------------------------------
# Protocol constants (from the AgentCore Runtime HTTP protocol contract).
# ---------------------------------------------------------------------------

#: ``/ping`` status meaning the container can accept new work.
PING_HEALTHY = "Healthy"

#: ``/ping`` status meaning the container is up but busy with async work. While
#: this is reported the runtime keeps the session alive.
PING_HEALTHY_BUSY = "HealthyBusy"

#: The doctor's live-call console, served at ``GET /live``.
#:
#: Deliberately one self-contained page with no build step and no framework. It
#: exists to be opened on a phone while the doctor is between patients, and the
#: whole point is that it loads instantly and does one thing: show who is on the
#: line right now and let you talk to them.
_LIVE_CONSOLE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Live calls — Clinic Front Desk</title>
<link rel="stylesheet" href="/static/dashboard.css">
<style>
  body { font-family: system-ui, sans-serif; margin: 0; padding: 1.25rem; }
  h1 { font-size: 1.35rem; margin: 0 0 .25rem; }
  .muted { color: #6b7280; font-size: .85rem; margin: 0 0 1.25rem; }
  .call { border: 1px solid #d1d5db; border-radius: .6rem; padding: .9rem;
          margin-bottom: .9rem; }
  .call.waiting { border-color: #dc2626; border-width: 2px; }
  .badge { display: inline-block; font-size: .7rem; font-weight: 700;
           letter-spacing: .04em; padding: .18rem .5rem; border-radius: .3rem;
           background: #dc2626; color: #fff; }
  .badge.live { background: #059669; }
  .who { font-weight: 600; margin: .45rem 0 .2rem; }
  .reason { font-size: .85rem; color: #b91c1c; margin: 0 0 .5rem; }
  /* Colours set explicitly, not inherited. dashboard.css is a dark theme, so a
     light panel here inherited light text and the transcript came out unreadable —
     which matters more on this page than anywhere else, because the doctor is
     reading it to decide whether to take a live call. */
  .transcript { background: #0b1220; color: #e5e7eb; border: 1px solid #1f2937;
                border-radius: .4rem; padding: .7rem;
                max-height: 13rem; overflow-y: auto; font-size: .88rem;
                line-height: 1.45; margin: .5rem 0; }
  .turn { margin: 0 0 .4rem; color: #e5e7eb; }
  .turn b { text-transform: capitalize; color: #7dd3fc; font-weight: 700; }
  .turn.patient b { color: #fca5a5; }
  .row { display: flex; gap: .5rem; margin-top: .5rem; }
  .build { color: #6b7280; font-size: .7rem; margin-top: 1.5rem;
           font-family: ui-monospace, monospace; }
  input[type=text] { flex: 1; padding: .55rem; border: 1px solid #d1d5db;
                     border-radius: .4rem; font-size: 1rem; }
  button { padding: .55rem .9rem; border-radius: .4rem; border: 0;
           background: #111827; color: #fff; font-weight: 600; cursor: pointer; }
  button.ghost { background: #e5e7eb; color: #111827; }
  button.talk { background: #047857; }
  .empty { color: #6b7280; }
  /* The microphone being live is the one thing on this page that must be
     impossible to miss: everything the doctor says is going down a phone line. */
  .talking { background: #047857; color: #fff; padding: .55rem .8rem;
             border-radius: .4rem; font-weight: 600; margin: 0 0 1rem; }
</style>
</head>
<body>
<h1>Live calls</h1>
<p class="muted">Calls happening right now. Take one over and the agent goes quiet.
Press <b>Talk</b> to speak to the caller with your own voice, or type a line and
press Say.</p>
<p id="talk-status" class="talking" style="display:none"></p>
<div id="calls"><p class="empty">Waiting for a call…</p></div>

<script>
(function () {
  var params = new URLSearchParams(location.search);
  var role = params.get("role") || "doctor";
  // The shared secret, when the console is published on a public host. Carried
  // through from this page's own URL so every request it makes stays authorised —
  // miss one and that call silently 403s while the rest of the page looks fine.
  var key = params.get("k") || "";
  var q = function (p) {
    var url = p + (p.indexOf("?") < 0 ? "?" : "&") + "role=" + encodeURIComponent(role);
    if (key) url += "&k=" + encodeURIComponent(key);
    return url;
  };
  var open = {};

  function post(path, body) {
    return fetch(q(path), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    });
  }

  function renderTurns(el, turns) {
    var html = turns.length
      ? turns.map(function (t) {
          // The caller's own words are what the doctor is scanning for, so they are
          // marked differently from the agent's and from her own typed lines.
          var who = t.role === "patient" || t.role === "user" ? "patient" : "";
          var name = t.role === "human" ? "you" : t.role;
          return '<p class="turn ' + who + '"><b>' + name + ':</b> ' +
                 t.text.replace(/[<>&]/g, "") + "</p>";
        }).join("")
      : '<p class="empty">Nothing said yet.</p>';

    // Rewriting identical markup every two seconds resets the scroll position and
    // makes the panel impossible to read back through.
    if (el.innerHTML === html) return;

    // Follow the conversation only if already at the bottom. Otherwise the doctor is
    // reading something earlier and must not be yanked away from it.
    var atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    el.innerHTML = html;
    if (atBottom) el.scrollTop = el.scrollHeight;
  }

  // Cards are created once and then updated in place, keyed by session id.
  //
  // The first version rebuilt the whole list on every poll. That destroyed the text
  // box two seconds after it appeared — along with whatever had been typed and the
  // focus — so it was impossible to actually say anything to the caller. Nothing may
  // replace an element the doctor is typing into.
  var cards = {};

  function controlsHtml(call) {
    var id = call.session_id;
    return call.taken_over
      ? '<input type="text" placeholder="Type what to say to the caller…" data-say="' + id + '">' +
        '<button data-send="' + id + '">Say</button>' +
        '<button class="talk" data-talk="' + id + '">🎤 Talk</button>' +
        '<button class="ghost" data-release="' + id + '">Hand back</button>'
      : '<button data-take="' + id + '">Take over</button>' +
        '<button class="talk" data-talk="' + id + '">🎤 Take over &amp; talk</button>';
  }

  // --- the doctor's own voice ------------------------------------------------
  //
  // Typing was only half a handover: it put the doctor's words on the call but not
  // her voice. This opens a second socket carrying her microphone to the caller and
  // the caller's audio back to her, so it is a conversation rather than a relay.
  var talk = { ws: null, id: null, ctx: null, play: null, node: null, stream: null,
               playhead: 0 };

  var MIC_RATE = 16000;   // what we send; the caller's client reads the rate per frame
  var MIC_FRAME = 512;    // ~32 ms, the cadence the caller's player expects

  var WORKLET = [
    "class DocMic extends AudioWorkletProcessor {",
    "  constructor(o){super();this.frame=o.processorOptions.frame;",
    "   this.ratio=sampleRate/o.processorOptions.rate;this.buf=[];this.pos=0;}",
    "  process(inputs){",
    "    var ch=inputs[0]&&inputs[0][0];if(!ch)return true;",
    "    for(var i=0;i<ch.length;i++){",
    "      this.pos+=1;",
    "      if(this.pos>=this.ratio){this.pos-=this.ratio;this.buf.push(ch[i]);}",
    "    }",
    "    while(this.buf.length>=this.frame){",
    "      var f=this.buf.splice(0,this.frame);",
    "      var pcm=new Int16Array(f.length);",
    "      for(var j=0;j<f.length;j++){",
    "        var s=Math.max(-1,Math.min(1,f[j]));",
    "        pcm[j]=s<0?s*0x8000:s*0x7fff;",
    "      }",
    "      this.port.postMessage(pcm.buffer,[pcm.buffer]);",
    "    }",
    "    return true;",
    "  }",
    "}",
    "registerProcessor('doc-mic', DocMic);"
  ].join("\\n");

  function b64(bytes) {
    var s = "";
    for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s);
  }

  function fromB64(text) {
    var raw = atob(text);
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  // The caller's voice, played on the doctor's side.
  function playCaller(bytes, rate) {
    if (!talk.play) return;
    var samples = Math.floor(bytes.length / 2);
    if (!samples) return;
    var view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    var buffer = talk.play.createBuffer(1, samples, rate || MIC_RATE);
    var channel = buffer.getChannelData(0);
    for (var i = 0; i < samples; i++) channel[i] = view.getInt16(i * 2, true) / 0x8000;
    var src = talk.play.createBufferSource();
    src.buffer = buffer;
    src.connect(talk.play.destination);
    var now = talk.play.currentTime;
    if (talk.playhead < now) talk.playhead = now + 0.05;
    src.start(talk.playhead);
    talk.playhead += buffer.duration;
  }

  async function startTalking(id) {
    if (talk.ws) await stopTalking();

    var proto = location.protocol === "https:" ? "wss://" : "ws://";
    var ws = new WebSocket(proto + location.host +
                           q("/dashboard/live/" + id + "/talk"));
    talk.ws = ws;
    talk.id = id;

    ws.addEventListener("message", function (e) {
      var m;
      try { m = JSON.parse(e.data); } catch (err) { return; }
      if (m.message_type === "caller_audio") {
        playCaller(fromB64(m.audio), m.sample_rate);
      }
    });
    ws.addEventListener("close", function () { stopTalking(); });

    await new Promise(function (resolve, reject) {
      ws.addEventListener("open", resolve, { once: true });
      ws.addEventListener("error", reject, { once: true });
    });

    talk.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true }
    });
    talk.ctx = new AudioContext({ sampleRate: MIC_RATE });
    talk.play = new AudioContext();
    await talk.ctx.resume();
    await talk.play.resume();
    talk.playhead = talk.play.currentTime;

    var blob = new Blob([WORKLET], { type: "application/javascript" });
    await talk.ctx.audioWorklet.addModule(URL.createObjectURL(blob));
    talk.node = new AudioWorkletNode(talk.ctx, "doc-mic", {
      processorOptions: { rate: MIC_RATE, frame: MIC_FRAME }
    });
    talk.node.port.onmessage = function (e) {
      if (!talk.ws || talk.ws.readyState !== 1) return;
      talk.ws.send(JSON.stringify({
        message_type: "doctor_audio",
        audio: b64(new Uint8Array(e.data)),
        format: "pcm",
        sample_rate: MIC_RATE,
        channels: 1
      }));
    };
    talk.ctx.createMediaStreamSource(talk.stream).connect(talk.node);
    setStatus("You are speaking to the caller. Microphone is live.");
    refresh();
  }

  async function stopTalking() {
    if (talk.ws) {
      try { talk.ws.send(JSON.stringify({ message_type: "leave" })); } catch (e) {}
      try { talk.ws.close(); } catch (e) {}
    }
    if (talk.stream) talk.stream.getTracks().forEach(function (t) { t.stop(); });
    if (talk.ctx) { try { await talk.ctx.close(); } catch (e) {} }
    if (talk.play) { try { await talk.play.close(); } catch (e) {} }
    talk = { ws: null, id: null, ctx: null, play: null, node: null, stream: null,
             playhead: 0 };
    setStatus("");
    refresh();
  }

  // --- noticing a call at all -----------------------------------------------
  //
  // The console was silent: a call could sit flagged for a person while the doctor
  // was reading something else on the page, and the first she knew of it was the
  // agent apologising to the caller. Ring, and put it in the tab title, so the page
  // does not have to be the thing being looked at.
  var alerted = {};

  function ring() {
    try {
      var ctx = new AudioContext();
      // Two short rising blips. Synthesised rather than a file so there is no asset
      // to serve and nothing to 404 on a fresh deploy.
      [0, 0.28].forEach(function (offset) {
        var osc = ctx.createOscillator();
        var gain = ctx.createGain();
        osc.frequency.value = 880;
        osc.type = "sine";
        gain.gain.setValueAtTime(0.0001, ctx.currentTime + offset);
        gain.gain.exponentialRampToValueAtTime(0.25, ctx.currentTime + offset + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + offset + 0.22);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(ctx.currentTime + offset);
        osc.stop(ctx.currentTime + offset + 0.24);
      });
      setTimeout(function () { try { ctx.close(); } catch (e) {} }, 1200);
    } catch (e) { /* no audio available; the title still changes */ }
  }

  function announce(calls) {
    var waiting = calls.filter(function (c) { return c.needs_human && !c.taken_over; });

    // Ring once per call, not once per poll — this runs every two seconds.
    waiting.forEach(function (c) {
      if (!alerted[c.session_id]) {
        alerted[c.session_id] = true;
        ring();
      }
    });

    var live = {};
    calls.forEach(function (c) { live[c.session_id] = true; });
    Object.keys(alerted).forEach(function (id) {
      if (!live[id]) delete alerted[id];
    });

    document.title = waiting.length
      ? "(" + waiting.length + ") CALL WAITING — Live calls"
      : (calls.length ? "(" + calls.length + ") Live calls" : "Live calls");
  }

  function setStatus(text) {
    var el = document.getElementById("talk-status");
    if (el) { el.textContent = text; el.style.display = text ? "" : "none"; }
  }

  function createCard(call) {
    var id = call.session_id;
    var div = document.createElement("div");
    div.innerHTML =
      '<span class="badge" data-badge></span>' +
      '<p class="who" data-who></p>' +
      '<p class="reason" data-reason></p>' +
      '<div class="transcript" data-t="' + id + '"></div>' +
      '<div class="row" data-row></div>';
    // A sentinel that cannot equal either real state, so the first updateCard always
    // renders the controls. This was "" — which *is* the not-taken-over value, so the
    // equality check below saw no change and the button row stayed empty for the
    // whole life of the card. Since those buttons were the only way to pick a call
    // up, nothing could be taken over at all.
    div.dataset.taken = "unset";
    return div;
  }

  function updateCard(div, call) {
    div.className = "call" + (call.needs_human && !call.taken_over ? " waiting" : "");

    var badge = div.querySelector("[data-badge]");
    badge.className = "badge" + (call.taken_over ? " live" : "");
    badge.textContent = call.taken_over
      ? "YOU ARE ON THIS CALL"
      : call.needs_human ? "NEEDS A PERSON" : "IN PROGRESS";

    div.querySelector("[data-who]").textContent =
      (call.patient_name || "Caller not yet identified") +
      (call.callback_phone ? " · " + call.callback_phone : "");

    var reason = div.querySelector("[data-reason]");
    reason.textContent = call.reason ? (call.reason_label || call.reason) : "";
    reason.style.display = call.reason ? "" : "none";

    // Only rebuild the controls when the call actually changes hands. Otherwise the
    // input survives every poll, keeping its text and the cursor.
    var taken = call.taken_over ? "1" : "";
    if (div.dataset.taken !== taken) {
      div.dataset.taken = taken;
      div.querySelector("[data-row]").innerHTML = controlsHtml(call);
      if (call.taken_over) {
        var box = div.querySelector("[data-say]");
        if (box) box.focus();
      }
    }
  }

  async function refresh() {
    var res = await fetch(q("/dashboard/live"));
    if (!res.ok) return;
    var calls = (await res.json()).calls || [];

    // Before the early return below, so the tab title still clears when the last
    // call ends.
    announce(calls);

    var host = document.getElementById("calls");
    var seen = {};

    var placeholder = host.querySelector(".empty");
    if (calls.length && placeholder) placeholder.remove();

    for (var i = 0; i < calls.length; i++) {
      var call = calls[i];
      seen[call.session_id] = true;
      var div = cards[call.session_id];
      if (!div) {
        div = createCard(call);
        cards[call.session_id] = div;
        host.appendChild(div);
      }
      updateCard(div, call);
    }

    // Drop cards for calls that have ended.
    Object.keys(cards).forEach(function (id) {
      if (!seen[id]) {
        cards[id].remove();
        delete cards[id];
      }
    });

    if (!calls.length && !host.querySelector(".empty")) {
      host.innerHTML = '<p class="empty">No calls in progress.</p>';
      return;
    }

    for (var j = 0; j < calls.length; j++) {
      (function (c) {
        var t = document.querySelector('[data-t="' + c.session_id + '"]');
        fetch(q("/dashboard/live/" + c.session_id + "/transcript"))
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (d) { if (d && t) renderTurns(t, d.turns || []); });
      })(calls[j]);
    }
  }

  document.addEventListener("click", async function (e) {
    var take = e.target.getAttribute("data-take");
    var send = e.target.getAttribute("data-send");
    var rel = e.target.getAttribute("data-release");
    var mic = e.target.getAttribute("data-talk");

    if (mic) {
      if (talk.id === mic) { await stopTalking(); }
      else {
        try { await startTalking(mic); }
        catch (err) {
          setStatus("Could not start the microphone: " + (err && err.message ? err.message : err));
        }
      }
      return;
    }

    if (take) { await post("/dashboard/live/" + take + "/takeover"); refresh(); }
    if (rel) {
      if (talk.id === rel) await stopTalking();
      await post("/dashboard/live/" + rel + "/release");
      refresh();
    }
    if (send) {
      var box = document.querySelector('[data-say="' + send + '"]');
      if (box && box.value.trim()) {
        var text = box.value.trim();
        box.value = "";
        await post("/dashboard/live/" + send + "/say", { text: text });
        refresh();
      }
    }
  });

  document.addEventListener("keydown", function (e) {
    if (e.key !== "Enter") return;
    var id = e.target.getAttribute && e.target.getAttribute("data-say");
    if (!id) return;
    var btn = document.querySelector('[data-send="' + id + '"]');
    if (btn) btn.click();
  });

  refresh();
  setInterval(refresh, 2000);
})();
</script>
<p class="build">console build __BUILD__</p>
</body>
</html>
"""

#: A short fingerprint of the console, shown in the corner of the page.
#:
#: Earned its place. Three separate times a fix was reported as working because the
#: server was serving it, while the browser was still running a cached copy with the
#: old bug — and there was no way to tell the two apart by looking. Now there is: if
#: this does not match the running server, the page is stale.
_LIVE_CONSOLE_BUILD = hashlib.sha256(_LIVE_CONSOLE_HTML.encode("utf-8")).hexdigest()[:8]

_LIVE_CONSOLE_HTML = _LIVE_CONSOLE_HTML.replace("__BUILD__", _LIVE_CONSOLE_BUILD)

#: How long a caller's socket may go quiet before it is treated as gone.
#:
#: Not a conversational timeout — the caller may think for as long as they like. This
#: is about frames on the wire. The client's AudioWorklet posts one roughly every
#: 32 ms for the whole call, so a full minute of nothing is a dead connection, not a
#: pause. Without this bound a dropped socket leaves a call listed as in progress
#: forever, with a Nova Sonic stream open behind it.
CALLER_IDLE_TIMEOUT_SECONDS = float(
    os.environ.get("CLINIC_CALLER_IDLE_TIMEOUT_SECONDS") or 60.0
)

#: Never cache this, anywhere, by anyone.
#:
#: For the live console and its polling: everything here describes a call happening
#: right now, and a cached copy of that is actively misleading rather than merely old.
NO_STORE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

#: Transcript roles that belong to the person who rang the clinic.
#:
#: Nova Sonic labels the caller ``"user"``. Spelled out here because a guard that
#: silences "everything that is not the caller" gets this exactly backwards if it
#: guesses the name — and the failure is quiet: the caller's own words vanish from
#: the record while the agent's keep coming.
CALLER_ROLES = frozenset({"user", "patient"})

#: Runtime session id header AgentCore sets on invocations and WS upgrades.
SESSION_ID_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"

#: Header carrying the viewer's dashboard role, set by the authenticating layer.
ROLE_HEADER = "X-Clinic-Role"

#: Host and port the runtime contract requires.
BIND_HOST = "0.0.0.0"  # noqa: S104 - required by the runtime contract
BIND_PORT = 8080


# ---------------------------------------------------------------------------
# JSON encoding for the domain models.
# ---------------------------------------------------------------------------


def _to_jsonable(value: Any) -> Any:
    """Convert domain models to JSON-serializable primitives.

    Handles the shapes the Data_Layer and dashboard actually return: frozen
    dataclasses (models, view-models, results), ``StrEnum``/``Enum`` statuses,
    dates, and nested containers of those.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, enum.Enum):
        return _to_jsonable(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_to_jsonable(item) for item in value]
    return value


def _utc_today() -> str:
    """Today's date in UTC as an ISO ``YYYY-MM-DD`` string."""
    return datetime.now(UTC).date().isoformat()


class InvocationError(Exception):
    """A ``/invocations`` failure carrying the HTTP status to return.

    Mapped onto the runtime contract's native HTTP error codes: ``400`` for a
    malformed request (``ValidationException``), ``403`` for a role-gate denial
    (``AccessDeniedException``), ``404`` for a missing record
    (``ResourceNotFoundException``), and ``500`` for a Data_Layer failure
    (``InternalServerException``).
    """

    def __init__(self, message: str, *, status_code: int = 400, error_type: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type or _DEFAULT_ERROR_TYPES.get(status_code, "ValidationException")


_DEFAULT_ERROR_TYPES: dict[int, str] = {
    400: "ValidationException",
    403: "AccessDeniedException",
    404: "ResourceNotFoundException",
    500: "InternalServerException",
}


# ---------------------------------------------------------------------------
# The server.
# ---------------------------------------------------------------------------


class AgentCoreServer:
    """Serves one :class:`ClinicFrontDeskApplication` over the runtime contract.

    Framework-light on purpose: this class holds the request *handling* logic as
    plain, directly-callable methods (``ping``, ``invoke``, ``run_voice_call``)
    that are unit-testable without an HTTP client, while
    :func:`create_asgi_app` does nothing but bind them to Starlette routes.

    Args:
        app: The composed application (in-memory for local runs, DynamoDB in
            deployment) whose one Data_Layer both agents share (Req 16.1).
        role_gate: Access-control policy for dashboard reads (Req 15.5, 15.7).
        activity_limit: Default number of call-activity entries to return.
        today: Injectable source of the current ISO date, used as the schedule
            view's default day (Req 15.1). Overridden in tests for determinism.
    """

    def __init__(
        self,
        app: ClinicFrontDeskApplication,
        *,
        role_gate: RoleGate | None = None,
        activity_limit: int = 50,
        today: Callable[[], str] = _utc_today,
    ) -> None:
        self.app = app
        self.role_gate = role_gate or RoleGate()
        self.activity_limit = activity_limit

        # Calls in progress, and the means to speak into one. In-process because a
        # call cannot outlive the process holding its socket, so a shared store
        # would buy coordination nobody needs. This is what lets a doctor step
        # into a live call instead of reading about it afterwards.
        self.live_calls = LiveCallRegistry()
        self.live_handover = LiveHandoverService(self.live_calls)
        self._today = today
        self._run_analysis = build_scheduled_intelligence_entrypoint(app)
        self._decision_actions = DecisionActionService(
            decision_store=app.stores.decisions,
            appointment_store=app.stores.appointments,
            waitlist_store=app.stores.waitlist,
        )
        # The dashboard web surface (page, live partials, change stream). Shares
        # this server's role gate so the API and the UI authorize identically.
        self.dashboard = DashboardWebApp(app, role_gate=self.role_gate)
        # Number of in-flight calls/analysis runs; drives HealthyBusy so the
        # runtime keeps a session alive while a call is still up.
        self._in_flight = 0

    # -- /ping --------------------------------------------------------------

    @property
    def in_flight(self) -> int:
        """Count of voice calls / analysis runs currently in progress."""
        return self._in_flight

    def ping(self) -> dict[str, str]:
        """Return the health payload for ``GET /ping``.

        Reports ``HealthyBusy`` while any call or analysis run is in flight so the
        runtime treats the session as active, and ``Healthy`` otherwise.
        ``time_of_last_update`` is deliberately omitted — the contract warns that
        advancing it on every ping prevents the idle-session timeout from firing.
        """
        status = PING_HEALTHY_BUSY if self._in_flight > 0 else PING_HEALTHY
        return {"status": status}

    @contextlib.contextmanager
    def _busy(self) -> Any:
        """Mark the container busy for the duration of the block."""
        self._in_flight += 1
        try:
            yield
        finally:
            self._in_flight -= 1

    # -- /invocations -------------------------------------------------------

    def invoke(self, payload: Mapping[str, Any], *, role: str | None = None) -> dict[str, Any]:
        """Dispatch one ``POST /invocations`` request.

        The body selects an ``action``; ``role`` (body field or ``X-Clinic-Role``
        header) is the caller's dashboard role. Every action except
        ``run_intelligence`` is role-gated: ``run_intelligence`` is invoked by the
        schedule, not by a human viewer, so it carries no viewer role.

        Args:
            payload: The parsed JSON request body.
            role: The caller's role, when not supplied in the body.

        Returns:
            A JSON-serializable response body.

        Raises:
            InvocationError: On an unknown/malformed action, a role-gate denial,
                a missing record, or a Data_Layer failure.
        """
        action = payload.get("action")
        if not isinstance(action, str) or not action:
            raise InvocationError(
                "request body must include a string 'action' field; "
                f"supported actions: {', '.join(sorted(self._actions()))}"
            )
        handler = self._actions().get(action)
        if handler is None:
            raise InvocationError(
                f"unknown action {action!r}; "
                f"supported actions: {', '.join(sorted(self._actions()))}"
            )
        effective_role = payload.get("role") or role
        return handler(payload, effective_role)

    def _actions(self) -> dict[str, Callable[[Mapping[str, Any], str | None], dict[str, Any]]]:
        """The ``/invocations`` action table."""
        return {
            "run_intelligence": self._action_run_intelligence,
            "open_decisions": self._action_open_decisions,
            "approve_decision": self._action_approve_decision,
            "dismiss_decision": self._action_dismiss_decision,
            "schedule": self._action_schedule,
            "activity": self._action_activity,
            "dashboard_shell": self._action_dashboard_shell,
            "config_version": self._action_config_version,
        }

    # -- authorization ------------------------------------------------------

    def _require_view(self, role: str | None, view: DashboardView) -> None:
        """Deny the request unless ``role`` may see ``view`` (Req 15.5, 15.7).

        A viewer with no assigned role is denied outright, and because the denial
        raises before any store read, no data for the view is ever gathered — let
        alone returned (Req 15.7).
        """
        decision = self.role_gate.resolve(role)
        if not decision.granted:
            raise InvocationError(
                "access denied: no role assigned for this dashboard",
                status_code=403,
            )
        if view not in decision.permitted_views:
            raise InvocationError(
                f"access denied: role {role!r} may not view {view.value!r}",
                status_code=403,
            )

    # -- action implementations --------------------------------------------

    def _action_run_intelligence(
        self, payload: Mapping[str, Any], role: str | None
    ) -> dict[str, Any]:
        """Run one Practice_Intelligence analysis pass (Req 13.1-13.8)."""
        with self._busy():
            summary = self._run_analysis(payload)
        return {"action": "run_intelligence", **summary}

    def _action_open_decisions(
        self, payload: Mapping[str, Any], role: str | None
    ) -> dict[str, Any]:
        """Return the open Decisions feed, newest-first (Req 14.1)."""
        self._require_view(role, DashboardView.DECISIONS)
        result = self.app.bff.open_decisions()
        if is_err(result):
            raise InvocationError(
                f"failed to read open decisions: {result.error.detail}",
                status_code=500,
            )
        return {
            "action": "open_decisions",
            "decisions": _to_jsonable(result.value),
        }

    def _action_approve_decision(
        self, payload: Mapping[str, Any], role: str | None
    ) -> dict[str, Any]:
        """Approve a Decision and execute its action (Req 14.3, 14.5, 14.6)."""
        self._require_view(role, DashboardView.DECISIONS)
        decision_id = self._required_str(payload, "decision_id")
        result = self._decision_actions.approve(decision_id)
        return {"action": "approve_decision", "result": _to_jsonable(result)}

    def _action_dismiss_decision(
        self, payload: Mapping[str, Any], role: str | None
    ) -> dict[str, Any]:
        """Dismiss a Decision, executing no action (Req 14.4)."""
        self._require_view(role, DashboardView.DECISIONS)
        decision_id = self._required_str(payload, "decision_id")
        result = self._decision_actions.dismiss(decision_id)
        return {"action": "dismiss_decision", "result": _to_jsonable(result)}

    def _action_schedule(
        self, payload: Mapping[str, Any], role: str | None
    ) -> dict[str, Any]:
        """Return a provider's appointments and open slots for a day (Req 15.1, 15.6)."""
        self._require_view(role, DashboardView.SCHEDULE)
        provider_id = self._required_str(payload, "provider_id")
        # Default to the current day (Req 15.1); an explicit day serves the
        # select-another-day path (Req 15.6).
        day = payload.get("day")
        if day is None:
            day = self._today()
        if not isinstance(day, str):
            raise InvocationError("'day' must be an ISO date string (YYYY-MM-DD)")
        services = payload.get("services")
        if services is not None and not (
            isinstance(services, list) and all(isinstance(item, str) for item in services)
        ):
            raise InvocationError("'services' must be a list of service-name strings")
        result = self.app.bff.schedule_for_day(provider_id, day, services)
        if is_err(result):
            raise InvocationError(
                f"failed to read schedule: {result.error.detail}", status_code=500
            )
        return {"action": "schedule", "schedule": _to_jsonable(result.value)}

    def _action_activity(
        self, payload: Mapping[str, Any], role: str | None
    ) -> dict[str, Any]:
        """Return the most-recent-first call-activity log (Req 15.2, 9.6)."""
        self._require_view(role, DashboardView.CALL_ACTIVITY)
        limit = payload.get("limit", self.activity_limit)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise InvocationError("'limit' must be a non-negative integer")
        result = self.app.bff.recent_activity(limit)
        if is_err(result):
            raise InvocationError(
                f"failed to read activity log: {result.error.detail}", status_code=500
            )
        return {"action": "activity", "entries": _to_jsonable(result.value)}

    def _action_dashboard_shell(
        self, payload: Mapping[str, Any], role: str | None
    ) -> dict[str, Any]:
        """Render the role-scoped dashboard shell HTML (Req 15.5, 15.7).

        Not itself gated: the shell *is* the access decision. A viewer with no
        role gets the access-denied shell with no data regions at all.
        """
        return {
            "action": "dashboard_shell",
            "role": role,
            "html": render_dashboard_shell(role, gate=self.role_gate),
        }

    def _action_config_version(
        self, payload: Mapping[str, Any], role: str | None
    ) -> dict[str, Any]:
        """Return the live clinic-config version counter (Req 1.8).

        Increments synchronously on every successful config save, so a client can
        confirm a new configuration is live without restarting anything.
        """
        return {
            "action": "config_version",
            "config_version": self.app.config_version,
            "last_event": _to_jsonable(self.app.last_config_event),
        }

    @staticmethod
    def _required_str(payload: Mapping[str, Any], field: str) -> str:
        """Read a required non-empty string field or raise a 400."""
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise InvocationError(f"'{field}' is required and must be a non-empty string")
        return value

    # -- /ws ----------------------------------------------------------------

    async def run_voice_call(
        self,
        *,
        session_id: str | None,
        receive: Callable[[], Awaitable[Mapping[str, Any] | None]],
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> CallOutcome | None:
        """Drive one Call_Session over an abstract bidirectional message channel.

        Kept transport-agnostic (``receive``/``send`` callables rather than a
        ``WebSocket``) so the whole voice path is testable without a real socket
        while the Starlette route is a thin adapter over it.

        The flow mirrors the design's voice topology: a **fresh**
        :class:`~clinic_front_desk.voice.agent.VoiceSession` per connection so the
        guardrail and tools read clinic config live (Req 1.8); assistant audio
        forwarded outbound as it is produced; patient audio pumped inbound; and on
        end the Call_Session outcome persisted, defaulting to ``interrupted`` when
        the call ended without completing a task (Req 11.5, 12.7).

        Args:
            session_id: The AgentCore runtime session id, used as the
                Call_Session id. A new id is generated when absent.
            receive: Awaits the next client message; returns ``None`` on
                disconnect.
            send: Sends one message to the client.

        Returns:
            The persisted :class:`~clinic_front_desk.models.CallOutcome`, or
            ``None`` if the outcome could not be persisted.
        """
        with self._busy():
            # Off the event loop. Building the session reads the clinic
            # configuration, the uploaded documents and the calendar span to brief
            # the agent — all synchronous DynamoDB and S3 calls, measured at 4-6
            # seconds against a real account. Run inline, that blocks the loop, so
            # the server cannot read inbound WebSocket frames for the whole of it:
            # the caller's opening words queue up in the socket and arrive at Nova
            # Sonic in a burst after the fact, which is enough to wreck the first
            # exchange. Every other store call in this module is already deferred
            # this way; this one was the exception.
            session = await asyncio.to_thread(self.app.start_voice_session, session_id)
            # Recording is opt-in: without a configured CallRecordingStore no
            # patient audio is captured at all. The transcript is always collected
            # (it is text on the CallSession the doctor already sees in the
            # activity log); only the audio depends on a bucket being configured.
            recorder = CallRecorder()
            record_audio = self.app.stores.recordings is not None

            live = self.live_calls.register(session.session_id, send)

            # Comes back to the caller if nobody picks up. Observed on a real call:
            # the agent said it was connecting someone, the call was flagged on the
            # console, nobody was watching, and the caller sat in silence — because
            # the agent had stopped talking believing it had handed over. Started for
            # every call and cancelled on hang-up; it does nothing at all unless a
            # human is actually asked for.
            unattended_watch = asyncio.create_task(
                self.live_handover.watch_unattended(session.session_id)
            )

            # So the doctor's console can show *which* call is waiting rather than
            # listing every call equally. Set on the live session's toolset, which
            # is the same instance the model-invoked tool and the guardrail backstop
            # share, so either route to an escalation flags the call.
            toolset = getattr(session, "toolset", None)
            if toolset is not None:
                toolset.on_escalation = (
                    lambda _sid, reason: self.live_calls.mark_needs_human(
                        session.session_id, reason
                    )
                )

            async def forward_audio(chunk: Any) -> None:
                if record_audio:
                    recorder.add_agent_audio(
                        base64.b64decode(chunk.audio or ""),
                        sample_rate=chunk.sample_rate,
                    )
                # A human holds this call: the agent stays silent. Its transcription
                # keeps running, so the doctor still reads what the caller says —
                # the caller simply never hears two voices at once. Deliberately not
                # the barge-in suppression flag, which clears on the next model
                # response; a handover has to hold until the human is finished.
                if live.taken_over:
                    return
                await send(
                    {
                        "message_type": "agent_audio",
                        "session_id": session.session_id,
                        "audio": chunk.audio,
                        "format": chunk.format,
                        "sample_rate": chunk.sample_rate,
                        "channels": chunk.channels,
                    }
                )

            async def forward_turn(turn: Any) -> None:
                recorder.add_turn(turn.role, turn.text)
                # Mirrored into the registry so a doctor joining mid-call can read
                # what they missed rather than asking the caller to start again.
                self.live_calls.record_turn(session.session_id, turn.role, turn.text)
                # Silencing the agent's audio was not enough: its words still arrived
                # as transcript and the caller read them on screen while talking to a
                # person. Anything the model generates once a human is on the call
                # belongs to a conversation it is no longer part of.
                #
                # The caller's own turns still go through, so the record stays
                # complete. Nova Sonic labels those "user" — not "patient", which is
                # what this guard checked at first, and which would have dropped
                # exactly the lines it was meant to keep.
                #
                # Still needed even though the model is fed silence while held:
                # generation already in flight at the moment of takeover has to land
                # somewhere.
                if live.taken_over and turn.role not in CALLER_ROLES:
                    return
                await send(
                    {
                        "message_type": "transcript",
                        "session_id": session.session_id,
                        "role": turn.role,
                        "text": turn.text,
                    }
                )

            async def forward_barge_in(timing: Any) -> None:
                """Tell the client the patient interrupted, so it stops playback.

                The stream manager already stopped *producing* assistant audio
                within the ≤ 500 ms budget (Req 12.2), but a client that has
                buffered several seconds of it would keep talking over the
                patient. This lets it flush its queue, so the budget holds from
                the patient's side of the call too.
                """
                # Nothing to interrupt when the agent is not the one talking. Left
                # unguarded this put "interrupted — playback stopped" on the caller's
                # screen every time they spoke to the doctor, which reads as the call
                # malfunctioning at the exact moment it is working as intended.
                if live.taken_over:
                    return
                await send(
                    {
                        "message_type": "barge_in",
                        "session_id": session.session_id,
                        "latency_ms": getattr(timing, "latency_ms", None),
                        "within_budget": getattr(timing, "within_budget", None),
                    }
                )

            session.manager.add_audio_output_handler(forward_audio)
            session.manager.add_interpreted_turn_handler(forward_turn)
            session.manager.add_barge_in_handler(forward_barge_in)

            await session.start()
            await send({"message_type": "session_started", "session_id": session.session_id})

            # The clinic's address and a map link, on screen from the moment the
            # call connects. A map URL cannot be read aloud — a caller cannot
            # transcribe percent-encoded text from audio — so it is delivered as
            # something tappable instead, and sending it up front keeps it clear
            # of the mid-answer tool-result timing problem.
            card = await asyncio.to_thread(
                build_clinic_card,
                self.app.stores.knowledge_base,
                self.app.stores.documents,
            )
            if card is not None:
                await send(
                    {
                        "message_type": "clinic_card",
                        "session_id": session.session_id,
                        **card,
                    }
                )

            model_events = asyncio.create_task(session.run())
            client_pump = asyncio.create_task(
                self._pump_client(
                    session, receive, recorder=recorder if record_audio else None
                )
            )
            outcome: CallOutcome | None = None
            try:
                # Whichever side ends first ends the call: the model stream
                # closing or the patient hanging up.
                await asyncio.wait(
                    {model_events, client_pump},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                # Stop watching for an unattended handover before anything else: the
                # socket is going away, and speaking into a closed one is noise in
                # the log at best.
                unattended_watch.cancel()

                # Drop it from the live registry first, so the doctor's console can
                # never offer to take over a call whose socket has already gone.
                self.live_calls.unregister(session.session_id)

                # Everything below must run however we got here — a clean end, a
                # stream error, or this coroutine being cancelled because the
                # client vanished — because Req 11.5/12.7 require the Call_Session
                # outcome to be persisted on *every* session end.
                #
                # Note the suppressed types include CancelledError: it derives
                # from BaseException, so a bare `suppress(Exception)` would let a
                # cancellation during teardown skip the finalize below. Tearing
                # down the Nova Sonic event stream really does raise it.
                for task in (model_events, client_pump):
                    if not task.done():
                        task.cancel()
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await task
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await session.stop()

                # Store the audio before finalizing so the CallSession can carry
                # the pointer. A failed upload is logged and ignored: losing a
                # recording must never lose the call record.
                recording_uri = await self._store_recording(session, recorder)

                # finalize() is synchronous and store-only, so it cannot be
                # interrupted — the outcome is persisted before any further await.
                recorded = session.context.outcome
                result = session.finalize(
                    recorded or CallOutcome.INTERRUPTED,
                    transcript=recorder.render_transcript(),
                    recording_uri=recording_uri,
                )
                outcome = None if is_err(result) else result.value.outcome

                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await send(
                        {
                            "message_type": "session_ended",
                            "session_id": session.session_id,
                            "outcome": _to_jsonable(outcome),
                        }
                    )
            return outcome

    async def _store_recording(
        self, session: Any, recorder: CallRecorder
    ) -> str | None:
        """Render and store the call audio, returning its URI or ``None``.

        Returns ``None`` — leaving the CallSession's ``recording_uri`` untouched —
        when recording is off, when no audio was captured, or when the upload
        failed. Rendering and uploading are pushed off the event loop because both
        are CPU/network bound and this runs while the socket is closing.
        """
        store = self.app.stores.recordings
        if store is None or not recorder.has_audio:
            return None
        try:
            audio = await asyncio.to_thread(recorder.render_wav)
        except Exception:  # pragma: no cover - defensive
            logger.exception("failed to render the recording for %s", session.session_id)
            return None
        if not audio:
            return None
        result = await asyncio.to_thread(
            store.put, session.session_id, audio, started_at=recorder.started_at
        )
        if is_err(result):
            logger.warning(
                "could not store the recording for %s: %s",
                session.session_id,
                result.error.detail,
            )
            return None
        if recorder.truncated:
            logger.warning(
                "recording for %s was truncated at the per-call cap",
                session.session_id,
            )
        return result.value.uri

    async def _pump_client(
        self,
        session: Any,
        receive: Callable[[], Awaitable[Mapping[str, Any] | None]],
        *,
        recorder: CallRecorder | None = None,
    ) -> None:
        """Forward client messages into the voice stream until disconnect.

        Recognized ``message_type`` values:

        - ``user_audio`` — base64 ``audio`` (plus optional ``format``,
          ``sample_rate``, ``channels``) forwarded to Nova Sonic.
        - ``user_text`` — a text turn, for text-mode clients and testing.
        - ``end_session`` — the patient hung up; ends the pump.

        A message with raw ``bytes`` under ``audio`` is base64-encoded first, so a
        binary WebSocket frame works without the client doing the encoding.
        """
        while True:
            # Bounded, because a socket can die without saying so.
            #
            # A caller who shuts a laptop, loses signal, or is probed by a tool that
            # drops the TCP connection without a close frame never produces a
            # disconnect message. This await then blocks forever: the call stays on
            # the doctor's console as "in progress" with nothing said, offering her a
            # dead line to take over, and the Nova Sonic stream behind it stays open
            # and billing. Two such calls sat there for twenty minutes before this
            # was noticed.
            #
            # The bound is safe because the client streams continuously: its
            # AudioWorklet posts a frame roughly every 32 ms whether or not anyone is
            # speaking, so silence on the wire means the socket is gone, not that the
            # caller is thinking.
            try:
                message = await asyncio.wait_for(
                    receive(), timeout=CALLER_IDLE_TIMEOUT_SECONDS
                )
            except (asyncio.TimeoutError, TimeoutError):
                logger.info(
                    "voice call %s: no frames for %.0fs, treating the socket as gone",
                    session.session_id,
                    CALLER_IDLE_TIMEOUT_SECONDS,
                )
                return
            if message is None:
                return
            message_type = message.get("message_type") or message.get("type")
            if message_type == "end_session":
                return
            if message_type == "user_text":
                text = message.get("text")
                if isinstance(text, str) and text:
                    # Same rule as audio: with a human on the call the model is not a
                    # participant, so it is not given the turn either. Still recorded,
                    # just not answered by the agent.
                    held = self.live_calls.get(session.session_id)
                    if held is not None and held.taken_over:
                        self.live_calls.record_turn(session.session_id, "user", text)
                    else:
                        await session.manager.send_text(text)
                continue
            audio = message.get("audio")
            if audio is None:
                logger.debug("ignoring unrecognized voice message: %r", message_type)
                continue
            if isinstance(audio, (bytes, bytearray)):
                audio = base64.b64encode(bytes(audio)).decode("ascii")
            if not isinstance(audio, str):
                logger.debug("ignoring non-string audio payload")
                continue
            sample_rate = int(message.get("sample_rate", 16000))
            raw_audio = base64.b64decode(audio)
            if recorder is not None:
                # The real audio, always. A call a human took over is still a call
                # the clinic made, and the recording should reflect what was said.
                recorder.add_patient_audio(raw_audio, sample_rate=sample_rate)

            # Tee the caller's voice to the doctor when she is on the call, so she can
            # actually hear them rather than reading a transcript. A no-op with no
            # doctor attached, which is every ordinary call — this runs on every
            # inbound frame, so it must cost nothing in the common case.
            await self.live_handover.relay_caller_audio(
                session.session_id, audio, sample_rate=sample_rate
            )

            # While a human holds the call, the model must stop *listening*, not just
            # stop speaking. Muting only its output left it hearing the whole
            # doctor-patient conversation and forming replies to it: it answered
            # questions meant for the doctor, and its queued turns surfaced in the
            # middle of theirs.
            #
            # Silence rather than sending nothing at all. Nova Sonic holds a
            # bidirectional stream, and starving it for the length of a real
            # conversation risks it closing — which would break the hand-back, when
            # the whole point of handing back is that the agent is still there.
            # Silence keeps the stream warm and triggers no voice activity, so the
            # model neither responds nor accumulates a conversation it was not part
            # of.
            live_call = self.live_calls.get(session.session_id)
            if live_call is not None and live_call.taken_over:
                audio = base64.b64encode(bytes(len(raw_audio))).decode("ascii")
            await session.manager.send_audio(
                audio,
                format=str(message.get("format", "pcm")),
                sample_rate=sample_rate,
                channels=int(message.get("channels", 1)),
            )


# ---------------------------------------------------------------------------
# Starlette binding (the only framework-aware code in this module).
# ---------------------------------------------------------------------------


def create_asgi_app(
    app: ClinicFrontDeskApplication,
    *,
    role_gate: RoleGate | None = None,
    server: AgentCoreServer | None = None,
    voice_only: bool = False,
    console_token: str | None = None,
) -> Starlette:
    """Bind an :class:`AgentCoreServer` to the runtime contract's ASGI routes.

    Args:
        app: The composed application to serve.
        role_gate: Optional access-control policy override.
        server: Optional pre-built server (takes precedence over ``app``).
        voice_only: Serve only the caller-facing voice routes — the client page,
            the WebSocket, its static assets and the health probe. The doctor's
            dashboard, calendar, patient records, documents and onboarding are not
            routed at all.

            For putting the agent on a public URL. Dashboard access is decided by a
            ``?role=`` query parameter, which
            :meth:`DashboardWebApp.resolve_role` itself documents as *not* a
            security control — it exists for local runs behind no auth layer. On a
            public host that means anyone holding the link is the doctor, reading
            patient names, mobile numbers and blood groups. Not routing those paths
            is a stronger guarantee than guarding them, and it is less code.

    Returns:
        A Starlette app exposing ``GET /ping``, ``POST /invocations``, and
        ``WebSocket /ws`` — all on the one port the container listens on.

    Raises:
        RuntimeError: If Starlette is not installed (``pip install -e .[deploy]``).
    """
    try:
        from starlette.applications import Starlette as StarletteApp
        from starlette.requests import Request as StarletteRequest
        from starlette.responses import (
            HTMLResponse,
            JSONResponse,
            RedirectResponse,
            Response as StarletteResponse,
            StreamingResponse,
        )
        from starlette.routing import Route, WebSocketRoute
        from starlette.websockets import WebSocket as StarletteWebSocket
        from starlette.websockets import WebSocketDisconnect
    except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "the AgentCore server needs Starlette; install the deploy extra: "
            'pip install -e ".[deploy]"'
        ) from exc

    runtime_server = server or AgentCoreServer(app, role_gate=role_gate)

    def error_response(message: str, status_code: int, error_type: str) -> Response:
        """Build a contract-shaped error response.

        The HTTP protocol contract returns errors as native HTTP responses with
        the exception name in the ``x-amzn-ErrorType`` header rather than wrapping
        them in a protocol envelope, so that is what this emits.
        """
        return JSONResponse(
            {"message": message, "error_type": error_type},
            status_code=status_code,
            headers={"x-amzn-ErrorType": error_type},
        )

    async def ping_route(request: StarletteRequest) -> Response:
        """``GET /ping`` — runtime health probe."""
        return JSONResponse(runtime_server.ping())

    async def invocations_route(request: StarletteRequest) -> Response:
        """``POST /invocations`` — JSON request/response surface."""
        try:
            payload = await request.json()
        except Exception:
            return error_response(
                "request body must be valid JSON", 400, "ValidationException"
            )
        if not isinstance(payload, dict):
            return error_response(
                "request body must be a JSON object", 400, "ValidationException"
            )
        role = request.headers.get(ROLE_HEADER)
        try:
            # The store/BFF calls are synchronous; run them off the event loop so
            # a slow DynamoDB read cannot stall concurrent voice WebSockets.
            body = await asyncio.to_thread(runtime_server.invoke, payload, role=role)
        except InvocationError as exc:
            return error_response(exc.message, exc.status_code, exc.error_type)
        except Exception:  # pragma: no cover - defensive
            logger.exception("unhandled error in /invocations")
            return error_response(
                "internal error handling the invocation",
                500,
                "InternalServerException",
            )
        return JSONResponse(body)

    async def ws_route(websocket: StarletteWebSocket) -> None:
        """``WebSocket /ws`` — the Voice_Front_Desk bidirectional transport."""
        await websocket.accept()
        session_id = websocket.headers.get(SESSION_ID_HEADER.lower())

        async def receive() -> Mapping[str, Any] | None:
            try:
                message = await websocket.receive()
            except WebSocketDisconnect:
                return None
            if message["type"] == "websocket.disconnect":
                return None
            if (raw := message.get("bytes")) is not None:
                # A binary frame is raw patient PCM.
                return {"message_type": "user_audio", "audio": raw}
            text = message.get("text")
            if text is None:
                return {}
            try:
                parsed = json.loads(text)
            except ValueError:
                # Plain-text frames are accepted as a text turn.
                return {"message_type": "user_text", "text": text}
            return parsed if isinstance(parsed, dict) else {}

        async def send(message: Mapping[str, Any]) -> None:
            await websocket.send_json(dict(message))

        try:
            await runtime_server.run_voice_call(
                session_id=session_id, receive=receive, send=send
            )
        except WebSocketDisconnect:
            logger.info("voice client disconnected (session_id=%s)", session_id)
        except Exception:  # pragma: no cover - defensive
            logger.exception("unhandled error in /ws voice call")
        finally:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await websocket.close()

    # -- dashboard UI ------------------------------------------------------
    #
    # Every handler below runs the (synchronous) render off the event loop via
    # asyncio.to_thread, so a slow store read cannot stall a concurrent voice
    # WebSocket sharing this process.

    dashboard = runtime_server.dashboard

    def role_of(request: StarletteRequest) -> str | None:
        return dashboard.resolve_role(request.query_params, request.headers.get(ROLE_HEADER))

    async def render(fn: Callable[[], str], media_type: str = "text/html") -> Response:
        """Run a dashboard render off-loop and map its failures to HTTP."""
        try:
            body = await asyncio.to_thread(fn)
        except DashboardHttpError as exc:
            return error_response(exc.message, exc.status_code, _DEFAULT_ERROR_TYPES.get(
                exc.status_code, "ValidationException"
            ))
        except Exception:  # pragma: no cover - defensive
            logger.exception("unhandled error rendering a dashboard view")
            return error_response(
                "internal error rendering the dashboard", 500, "InternalServerException"
            )
        return StarletteResponse(body, media_type=media_type)

    async def dashboard_page_route(request: StarletteRequest) -> Response:
        """``GET /`` — the full role-scoped dashboard page (Req 15.5, 15.7).

        Redirects to the onboarding wizard when no clinic configuration exists,
        which is the Req 1.1 "present onboarding on first access" behaviour.
        """
        role = role_of(request)
        configured = await asyncio.to_thread(dashboard.is_configured)
        if not configured:
            return RedirectResponse("/onboarding", status_code=303)
        try:
            window = dashboard.parse_window(request.query_params)
        except DashboardHttpError as exc:
            return error_response(exc.message, exc.status_code, "ValidationException")
        return await render(lambda: dashboard.page(role, window_days=window))

    async def onboarding_route(request: StarletteRequest) -> Response:
        """``GET|POST /onboarding`` — the clinic onboarding wizard (Req 1.1-1.6).

        ``POST`` is what the wizard's own form has always targeted; only ``GET``
        was routed, so submitting the form 405'd and no configuration could be
        saved through the UI at all.
        """
        if request.method == "POST":
            form = await request.form()
            values = {
                key: value for key, value in form.items() if isinstance(value, str)
            }
            return await render(lambda: dashboard.submit_onboarding(values))
        return await render(dashboard.onboarding_page)

    async def slots_route(request: StarletteRequest) -> Response:
        """``GET|POST /slots`` — the doctor's day calendar and its publish action."""
        role = role_of(request)
        if request.method != "POST":
            return await render(
                lambda: dashboard.day_schedule_page(
                    role,
                    day=request.query_params.get("day") or None,
                    provider_id=request.query_params.get("provider_id") or None,
                )
            )

        form = await request.form()

        def value(name: str) -> str:
            raw = form.get(name)
            return raw if isinstance(raw, str) else ""

        day = value("day")
        provider_id = value("provider_id")
        try:
            message, error = await asyncio.to_thread(
                dashboard.publish_slots,
                role,
                day=day,
                provider_id=provider_id,
                service=value("service"),
                minutes=value("minutes"),
                start=value("start"),
                end=value("end"),
                until=value("until"),
                skip_closed=value("skip_closed") == "1",
            )
        except DashboardHttpError as exc:
            return error_response(
                exc.message,
                exc.status_code,
                _DEFAULT_ERROR_TYPES.get(exc.status_code, "ValidationException"),
            )
        return await render(
            lambda: dashboard.day_schedule_page(
                role,
                day=day or None,
                provider_id=provider_id or None,
                message=message,
                error=error,
            )
        )

    async def patient_detail_route(request: StarletteRequest) -> Response:
        """``GET|POST /slots/patient/{id}`` — the patient behind a booked slot.

        ``POST`` corrects the record. It exists because a phone line mishears
        names, and without it a misheard one was permanent.
        """
        role = role_of(request)
        patient_id = request.path_params["patient_id"]
        day = request.query_params.get("day") or None
        provider_id = request.query_params.get("provider_id") or None

        message: str | None = None
        error: str | None = None
        if request.method == "POST":
            form = await request.form()

            def value(name: str) -> str:
                raw = form.get(name)
                return raw if isinstance(raw, str) else ""

            try:
                message, error = await asyncio.to_thread(
                    dashboard.update_patient,
                    role,
                    patient_id,
                    name=value("name"),
                    callback_phone=value("callback_phone"),
                    age=value("age"),
                    blood_group=value("blood_group"),
                    weight_kg=value("weight_kg"),
                    height_cm=value("height_cm"),
                )
            except DashboardHttpError as exc:
                return error_response(
                    exc.message,
                    exc.status_code,
                    _DEFAULT_ERROR_TYPES.get(exc.status_code, "ValidationException"),
                )

        return await render(
            lambda: dashboard.patient_detail_page(
                role,
                patient_id,
                day=day,
                provider_id=provider_id,
                message=message,
                error=error,
            )
        )

    async def slot_block_route(request: StarletteRequest) -> Response:
        """``POST /slots/block`` — take slots off the calendar, or give them back."""
        role = role_of(request)
        form = await request.form()

        def value(name: str) -> str:
            raw = form.get(name)
            return raw if isinstance(raw, str) else ""

        day = value("day")
        provider_id = value("provider_id")
        try:
            message, error = await asyncio.to_thread(
                dashboard.set_slot_block,
                role,
                day=day,
                provider_id=provider_id,
                blocked=value("blocked") == "1",
                slot_id=value("slot_id"),
                start=value("start"),
                end=value("end"),
            )
        except DashboardHttpError as exc:
            return error_response(
                exc.message,
                exc.status_code,
                _DEFAULT_ERROR_TYPES.get(exc.status_code, "ValidationException"),
            )
        return await render(
            lambda: dashboard.day_schedule_page(
                role,
                day=day or None,
                provider_id=provider_id or None,
                message=message,
                error=error,
            )
        )

    # -- clinic documents --------------------------------------------------

    async def documents_route(request: StarletteRequest) -> Response:
        """``GET|POST /documents`` — the uploaded-document library (doctor only).

        ``POST`` is a multipart upload. It renders the page directly rather than
        redirecting, so the ingest outcome — how many passages became searchable,
        or why the file could not be read — is shown with the result it describes.
        """
        role = role_of(request)
        if request.method != "POST":
            return await render(lambda: dashboard.documents_page(role))

        try:
            form = await request.form()
        except Exception:
            return error_response(
                "the upload could not be read as a multipart form",
                400,
                "ValidationException",
            )
        upload = form.get("document")
        if upload is None or isinstance(upload, str):
            return await render(
                lambda: dashboard.documents_page(
                    role, error="No file was received. Please choose a file."
                )
            )
        data = await upload.read()
        filename = upload.filename or "upload"
        content_type = upload.content_type or ""
        try:
            message, error = await asyncio.to_thread(
                dashboard.upload_document,
                role,
                data,
                filename=filename,
                content_type=content_type,
            )
        except DashboardHttpError as exc:
            return error_response(
                exc.message,
                exc.status_code,
                _DEFAULT_ERROR_TYPES.get(exc.status_code, "ValidationException"),
            )
        return await render(
            lambda: dashboard.documents_page(role, message=message, error=error)
        )

    async def document_delete_route(request: StarletteRequest) -> Response:
        """``POST /documents/{id}/delete`` — withdraw a document."""
        role = role_of(request)
        document_id = request.path_params["document_id"]
        try:
            message = await asyncio.to_thread(
                dashboard.delete_document, role, document_id
            )
        except DashboardHttpError as exc:
            return error_response(
                exc.message,
                exc.status_code,
                _DEFAULT_ERROR_TYPES.get(exc.status_code, "ValidationException"),
            )
        return await render(lambda: dashboard.documents_page(role, message=message))

    async def document_download_route(request: StarletteRequest) -> Response:
        """``GET /documents/{id}/download`` — the original file as uploaded."""
        role = role_of(request)
        document_id = request.path_params["document_id"]
        try:
            media_type, data, filename = await asyncio.to_thread(
                dashboard.document_original, role, document_id
            )
        except DashboardHttpError as exc:
            return error_response(
                exc.message,
                exc.status_code,
                _DEFAULT_ERROR_TYPES.get(exc.status_code, "ValidationException"),
            )
        # Quoted and attachment-dispositioned: the filename is doctor-supplied, so
        # it must not be able to inject header syntax or be rendered inline.
        safe_name = filename.replace('"', "").replace("\r", "").replace("\n", "")
        return StarletteResponse(
            data,
            media_type=media_type,
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": f'attachment; filename="{safe_name}"',
            },
        )

    async def document_extract_route(request: StarletteRequest) -> Response:
        """``POST /documents/{id}/extract`` — pre-fill the setup form from a document.

        Renders the wizard with values proposed and nothing saved; the doctor
        submitting it is what persists them.
        """
        role = role_of(request)
        document_id = request.path_params["document_id"]
        return await render(lambda: dashboard.extract_config_page(role, document_id))

    async def voice_client_route(request: StarletteRequest) -> Response:
        """``GET /voice`` — the browser voice client (speak to the agent).

        No role gate: this is the caller's side of the phone call and carries no
        clinic data. It only opens ``/ws``, which is the same transport a real
        phone integration would use.
        """
        return await render(dashboard.voice_client_page)

    async def schedule_partial_route(request: StarletteRequest) -> Response:
        """``GET /dashboard/schedule`` — ScheduleView partial (Req 15.1, 15.6)."""
        role = role_of(request)
        provider_id = request.query_params.get("provider_id") or None
        day = request.query_params.get("day") or None
        return await render(
            lambda: dashboard.schedule_partial(role, provider_id=provider_id, day=day)
        )

    async def activity_partial_route(request: StarletteRequest) -> Response:
        """``GET /dashboard/activity`` — CallActivityLog partial (Req 15.2, 9.6)."""
        role = role_of(request)
        try:
            limit = dashboard.parse_limit(request.query_params)
        except DashboardHttpError as exc:
            return error_response(exc.message, exc.status_code, "ValidationException")
        return await render(lambda: dashboard.activity_partial(role, limit=limit))

    async def metrics_partial_route(request: StarletteRequest) -> Response:
        """``GET /dashboard/metrics`` — ImpactMetricsStrip partial (Req 15.3)."""
        role = role_of(request)
        try:
            window = dashboard.parse_window(request.query_params)
        except DashboardHttpError as exc:
            return error_response(exc.message, exc.status_code, "ValidationException")
        return await render(lambda: dashboard.metrics_partial(role, window_days=window))

    async def decisions_route(request: StarletteRequest) -> Response:
        """``GET /dashboard/decisions`` — the DecisionsFeedView as JSON (Req 14.1)."""
        role = role_of(request)
        return await render(
            lambda: dashboard.decisions_feed_json(role), media_type="application/json"
        )

    async def call_record_route(request: StarletteRequest) -> Response:
        """``GET /dashboard/calls/{id}`` — one call's transcript + playback URL."""
        role = role_of(request)
        call_id = request.path_params["call_session_id"]
        return await render(
            lambda: dashboard.call_record_json(role, call_id),
            media_type="application/json",
        )

    async def recording_route(request: StarletteRequest) -> Response:
        """``GET /dashboard/calls/{id}/recording`` — stream a call's audio.

        The fallback playback path for backends that cannot presign a URL. With S3
        the dashboard uses a presigned URL instead, so audio does not transit this
        process.
        """
        role = role_of(request)
        call_id = request.path_params["call_session_id"]
        try:
            media_type, audio = await asyncio.to_thread(
                dashboard.recording_bytes, role, call_id
            )
        except DashboardHttpError as exc:
            return error_response(
                exc.message,
                exc.status_code,
                _DEFAULT_ERROR_TYPES.get(exc.status_code, "ValidationException"),
            )
        return StarletteResponse(
            audio,
            media_type=media_type,
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": f'inline; filename="{call_id}.wav"',
            },
        )

    # -- live human takeover ------------------------------------------------
    #
    # A caller who needs a person should not be told someone will ring back. These
    # routes let the doctor step into a call that is still open: the agent goes
    # quiet, the doctor types, and Amazon Polly speaks it down the audio channel the
    # caller's browser is already playing.
    #
    # Gated on the call-activity view, the same permission that already governs
    # transcripts — this shows live speech, which is the most sensitive thing the
    # dashboard serves.

    def _live_token_ok(request: StarletteRequest) -> bool:
        """Check the shared secret, when one is configured.

        ``?role=`` is not a security control — it is a convenience for local runs
        behind no auth layer, and anyone holding the link can name themselves doctor.
        That is why the console is normally not routed at all on a public host.

        A configured token changes that: the console may be published, but only to
        someone holding the secret. Compared with :func:`hmac.compare_digest` so the
        comparison does not leak the prefix through timing.

        With no token configured this is always true, leaving local runs exactly as
        they were — the gate is opt-in, and its absence must not silently deny.
        """
        if not console_token:
            return True
        offered = request.query_params.get("k", "")
        return hmac.compare_digest(offered, console_token)

    def _live_guard(request: StarletteRequest) -> str | None:
        """Return the role when it may see live calls, else ``None``."""
        if not _live_token_ok(request):
            return None
        role = role_of(request)
        try:
            dashboard.require_view(role, DashboardView.CALL_ACTIVITY)
        except DashboardHttpError:
            return None
        return role

    async def live_calls_route(request: StarletteRequest) -> Response:
        """``GET /dashboard/live`` — calls in progress, the ones needing a human first."""
        if _live_guard(request) is None:
            return error_response("Access denied.", 403, "AccessDeniedException")
        return JSONResponse(
            {"calls": runtime_server.live_calls.list_calls()},
            # Whoever is in front of a CDN must not be able to cache this. A stale
            # list means a call shown as waiting that was answered minutes ago, or a
            # live one missing entirely — worse than an empty console, because it
            # looks authoritative. Currently uncached by the distribution's policy;
            # this stops that being the only thing preventing it.
            headers=NO_STORE,
        )

    async def live_transcript_route(request: StarletteRequest) -> Response:
        """``GET /dashboard/live/{id}/transcript`` — so a doctor joining late catches up."""
        if _live_guard(request) is None:
            return error_response("Access denied.", 403, "AccessDeniedException")
        session_id = request.path_params["session_id"]
        call = runtime_server.live_calls.get(session_id)
        if call is None:
            return error_response("That call is no longer live.", 404, "NotFound")
        return JSONResponse(
            {
                "session_id": session_id,
                "taken_over": call.taken_over,
                "turns": runtime_server.live_calls.transcript_of(session_id),
            }
        )

    async def live_takeover_route(request: StarletteRequest) -> Response:
        """``POST /dashboard/live/{id}/takeover`` — silence the agent, join the call."""
        if _live_guard(request) is None:
            return error_response("Access denied.", 403, "AccessDeniedException")
        session_id = request.path_params["session_id"]
        joined = await runtime_server.live_handover.take_over(session_id)
        if not joined:
            return error_response("That call is no longer live.", 404, "NotFound")
        return JSONResponse({"session_id": session_id, "taken_over": True})

    async def live_release_route(request: StarletteRequest) -> Response:
        """``POST /dashboard/live/{id}/release`` — hand the call back to the agent."""
        if _live_guard(request) is None:
            return error_response("Access denied.", 403, "AccessDeniedException")
        session_id = request.path_params["session_id"]
        released = await runtime_server.live_handover.release(session_id)
        if not released:
            return error_response("That call is no longer live.", 404, "NotFound")
        return JSONResponse({"session_id": session_id, "taken_over": False})

    async def live_say_route(request: StarletteRequest) -> Response:
        """``POST /dashboard/live/{id}/say`` — speak the doctor's words to the caller."""
        if _live_guard(request) is None:
            return error_response("Access denied.", 403, "AccessDeniedException")
        session_id = request.path_params["session_id"]
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        text = str((body or {}).get("text", "")).strip()
        if not text:
            return error_response("Nothing to say.", 400, "ValidationException")

        spoken = await runtime_server.live_handover.say(session_id, text)
        if not spoken:
            # The line is recorded on the transcript either way, so the doctor can
            # see what did not make it through rather than wondering.
            return JSONResponse(
                {"session_id": session_id, "spoken": False, "text": text},
                status_code=502,
            )
        return JSONResponse({"session_id": session_id, "spoken": True, "text": text})

    async def live_doctor_ws_route(websocket: StarletteWebSocket) -> None:
        """``WebSocket /dashboard/live/{id}/talk`` — the doctor's own voice on the call.

        Typing was only ever half a handover. This joins her microphone to the
        caller's audio channel and sends the caller's voice back to her, so it is a
        real conversation rather than a relay.

        Her audio goes to the caller as ``agent_audio`` — the frame their browser
        already plays — so nothing has to change on the patient side.
        """
        session_id = websocket.path_params["session_id"]

        # Authorise *before* accepting. This socket streams a live caller's voice out
        # and lets whoever holds it speak to them as the clinic, so an unauthorised
        # client should not get a completed handshake at all.
        #
        # It also has to honour the shared secret when the console is published on a
        # public host. It did not: it ran its own role check and never consulted the
        # token, which left the single most sensitive endpoint here wide open on
        # exactly the deployment the token exists to protect.
        role = websocket.query_params.get("role")
        offered = websocket.query_params.get("k", "")
        authorised = not console_token or hmac.compare_digest(offered, console_token)
        if authorised:
            try:
                dashboard.require_view(role, DashboardView.CALL_ACTIVITY)
            except DashboardHttpError:
                authorised = False
        if not authorised:
            await websocket.close(code=1008)
            return

        await websocket.accept()

        async def to_doctor(message: Mapping[str, Any]) -> None:
            await websocket.send_json(dict(message))

        call = runtime_server.live_calls.attach_doctor(session_id, to_doctor)
        if call is None:
            await websocket.send_json({"message_type": "error", "text": "call ended"})
            await websocket.close()
            return

        # Tell the caller a person is here, exactly as the typed takeover does.
        await runtime_server.live_handover.take_over(session_id)
        await websocket.send_json(
            {"message_type": "joined", "session_id": session_id}
        )

        try:
            while True:
                message = await websocket.receive_json()
                kind = str(message.get("message_type", ""))
                if kind == "leave":
                    break
                if kind != "doctor_audio":
                    continue
                audio = message.get("audio")
                if not isinstance(audio, str) or not audio:
                    continue
                await runtime_server.live_handover.relay_doctor_audio(
                    session_id,
                    audio,
                    sample_rate=int(message.get("sample_rate", 16000)),
                    channels=int(message.get("channels", 1)),
                )
        except Exception:  # noqa: BLE001 - a dropped tab is not an error worth raising
            logger.info("doctor disconnected from %s", session_id)
        finally:
            # Hand the call back rather than leaving the caller in silence if her
            # tab closed unexpectedly.
            runtime_server.live_calls.detach_doctor(session_id)
            with contextlib.suppress(Exception):
                await runtime_server.live_handover.release(session_id)
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await websocket.close()

    async def live_console_route(request: StarletteRequest) -> Response:
        """``GET /live`` — the doctor's console for calls happening right now."""
        role = _live_guard(request)
        if role is None:
            return error_response("Access denied.", 403, "AccessDeniedException")
        # This page *is* its JavaScript — the console's whole behaviour is inlined, so
        # a cached copy is stale code, not just stale text. Served without these
        # headers it silently kept handing back an old console after deploys: buttons
        # missing, fixes apparently not applied, and nothing on the server side to
        # show for it. A doctor picking up a live call must not be looking at a build
        # from before the last restart.
        return StarletteResponse(
            _LIVE_CONSOLE_HTML, media_type="text/html", headers=NO_STORE
        )

    async def resolve_decision_route(request: StarletteRequest) -> Response:
        """``POST /dashboard/decisions/{id}/{action}`` — approve/dismiss (Req 14.3, 14.4)."""
        role = role_of(request)
        decision_id = request.path_params["decision_id"]
        action = request.path_params["action"]
        try:
            body = await asyncio.to_thread(
                dashboard.resolve_decision, role, decision_id, action
            )
        except DashboardHttpError as exc:
            return error_response(
                exc.message,
                exc.status_code,
                _DEFAULT_ERROR_TYPES.get(exc.status_code, "ValidationException"),
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("unhandled error resolving a decision")
            return JSONResponse(
                {"outcome": "store_error", "error": "internal error"}, status_code=500
            )
        # A failed action is a 200 with a non-resolved outcome: the feed
        # controller restores the card from `error`, and the Decision is
        # legitimately still open (Req 14.6), so this is not an HTTP error.
        return JSONResponse(body)

    async def events_route(request: StarletteRequest) -> Response:
        """``GET /dashboard/events`` — server-sent ``ChangeEvent`` stream.

        Bridges the synchronous in-process :class:`DashboardChannel` to this
        client's connection. The channel fans out on whichever thread performed
        the mutation (often a ``to_thread`` worker), so events are handed to the
        event loop with ``call_soon_threadsafe`` rather than touching the queue
        directly.

        Requires a role: an unauthenticated viewer must not receive a live feed of
        clinic mutations (Req 15.7).
        """
        role = role_of(request)
        decision = runtime_server.role_gate.resolve(role)
        if not decision.granted:
            return error_response(
                "Access denied: no role assigned for this dashboard.",
                403,
                "AccessDeniedException",
            )

        stream = ChangeEventStream(
            runtime_server.app.channel,
            asyncio.get_running_loop(),
            heartbeat_seconds=SSE_HEARTBEAT_SECONDS,
        )
        return StreamingResponse(
            stream.events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                # Defeats proxy buffering, which would otherwise defer events
                # past the propagation budgets (Req 14.8, 15.4).
                "X-Accel-Buffering": "no",
            },
        )

    async def static_route(request: StarletteRequest) -> Response:
        """``GET /static/{asset}`` — the stylesheet and controller scripts."""
        name = request.path_params["asset"]
        try:
            media_type, body = await asyncio.to_thread(dashboard.static_asset, name)
        except DashboardHttpError as exc:
            return error_response(exc.message, exc.status_code, "ResourceNotFoundException")
        return StarletteResponse(
            body,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=300"},
        )

    # Everything a caller needs and nothing a doctor does. Kept as its own list so
    # the public surface is a thing you can read, rather than something inferred by
    # scanning a route table for what was left out.
    voice_routes = [
        Route("/ping", ping_route, methods=["GET"]),
        Route("/voice", voice_client_route, methods=["GET"]),
        WebSocketRoute("/ws", ws_route),
        Route(STATIC_PREFIX + "{asset}", static_route, methods=["GET"]),
    ]
    if voice_only:
        routes: list[Any] = list(voice_routes)
        if console_token:
            # Publishing the live console, and *only* the live console.
            #
            # Needed because live calls are tracked in memory, per process: a caller on
            # the public URL is registered inside this container, so a console running
            # anywhere else is looking at an empty list no matter what it is allowed to
            # see. To take a real call, the console has to be served by the process
            # holding it.
            #
            # Deliberately not the whole dashboard. Nothing here reads stored records —
            # no patient list, no calendar, no documents, no onboarding — so the blast
            # radius is calls in progress rather than the clinic's history. Those
            # remain unrouted even with a valid token.
            routes += [
                Route("/live", live_console_route, methods=["GET"]),
                Route("/dashboard/live", live_calls_route, methods=["GET"]),
                Route(
                    "/dashboard/live/{session_id}/transcript",
                    live_transcript_route,
                    methods=["GET"],
                ),
                Route(
                    "/dashboard/live/{session_id}/takeover",
                    live_takeover_route,
                    methods=["POST"],
                ),
                Route(
                    "/dashboard/live/{session_id}/release",
                    live_release_route,
                    methods=["POST"],
                ),
                Route(
                    "/dashboard/live/{session_id}/say",
                    live_say_route,
                    methods=["POST"],
                ),
                WebSocketRoute(
                    "/dashboard/live/{session_id}/talk", live_doctor_ws_route
                ),
            ]
            logger.info(
                "serving the voice agent plus the token-gated live console; "
                "patient records, calendar and documents are not mounted"
            )
        else:
            logger.info("serving the voice agent only; dashboard routes are not mounted")
        asgi_app = StarletteApp(routes=routes)
        asgi_app.state.server = runtime_server
        return asgi_app

    asgi_app = StarletteApp(
        routes=[
            Route("/ping", ping_route, methods=["GET"]),
            Route("/invocations", invocations_route, methods=["POST"]),
            WebSocketRoute("/ws", ws_route),
            # Dashboard UI.
            Route("/", dashboard_page_route, methods=["GET"]),
            Route("/onboarding", onboarding_route, methods=["GET", "POST"]),
            Route("/voice", voice_client_route, methods=["GET"]),
            Route("/slots", slots_route, methods=["GET", "POST"]),
            Route("/slots/block", slot_block_route, methods=["POST"]),
            Route(
                "/slots/patient/{patient_id}",
                patient_detail_route,
                methods=["GET", "POST"],
            ),
            Route("/documents", documents_route, methods=["GET", "POST"]),
            Route(
                "/documents/{document_id}/download",
                document_download_route,
                methods=["GET"],
            ),
            Route(
                "/documents/{document_id}/delete",
                document_delete_route,
                methods=["POST"],
            ),
            Route(
                "/documents/{document_id}/extract",
                document_extract_route,
                methods=["POST"],
            ),
            # Live human takeover. Dashboard routes, so absent from the voice-only
            # build: these carry live patient speech.
            Route("/live", live_console_route, methods=["GET"]),
            Route("/dashboard/live", live_calls_route, methods=["GET"]),
            Route(
                "/dashboard/live/{session_id}/transcript",
                live_transcript_route,
                methods=["GET"],
            ),
            Route(
                "/dashboard/live/{session_id}/takeover",
                live_takeover_route,
                methods=["POST"],
            ),
            Route(
                "/dashboard/live/{session_id}/release",
                live_release_route,
                methods=["POST"],
            ),
            Route("/dashboard/live/{session_id}/say", live_say_route, methods=["POST"]),
            WebSocketRoute("/dashboard/live/{session_id}/talk", live_doctor_ws_route),
            Route("/dashboard/schedule", schedule_partial_route, methods=["GET"]),
            Route("/dashboard/activity", activity_partial_route, methods=["GET"]),
            Route("/dashboard/metrics", metrics_partial_route, methods=["GET"]),
            Route("/dashboard/decisions", decisions_route, methods=["GET"]),
            Route(
                "/dashboard/calls/{call_session_id}",
                call_record_route,
                methods=["GET"],
            ),
            Route(
                "/dashboard/calls/{call_session_id}/recording",
                recording_route,
                methods=["GET"],
            ),
            Route(
                "/dashboard/decisions/{decision_id}/{action}",
                resolve_decision_route,
                methods=["POST"],
            ),
            Route(EVENTS_ENDPOINT, events_route, methods=["GET"]),
            Route(STATIC_PREFIX + "{asset}", static_route, methods=["GET"]),
        ]
    )
    # Expose the server for tests and for callers that want to introspect it.
    asgi_app.state.server = runtime_server
    return asgi_app


# ---------------------------------------------------------------------------
# Container entry point.
# ---------------------------------------------------------------------------


def runtime_config_from_env(env: Mapping[str, str] | None = None) -> RuntimeConfig:
    """Build a :class:`RuntimeConfig` from environment variables.

    Read from the container environment so the same image runs against any
    table/region without a rebuild:

    ``CLINIC_TABLE_NAME``, ``AWS_REGION`` (or ``CLINIC_REGION``),
    ``CLINIC_NOVA_SONIC_MODEL_ID``, ``CLINIC_ANALYSIS_INTERVAL_HOURS``,
    ``CLINIC_CREATE_TABLE_IF_MISSING``, ``CLINIC_DYNAMODB_ENDPOINT_URL``.
    """
    source = env if env is not None else os.environ
    defaults = RuntimeConfig()
    interval_raw = source.get("CLINIC_ANALYSIS_INTERVAL_HOURS")
    try:
        interval = float(interval_raw) if interval_raw else defaults.analysis_interval_hours
    except ValueError:
        logger.warning(
            "invalid CLINIC_ANALYSIS_INTERVAL_HOURS=%r; using %s",
            interval_raw,
            defaults.analysis_interval_hours,
        )
        interval = defaults.analysis_interval_hours
    return RuntimeConfig(
        table_name=source.get("CLINIC_TABLE_NAME", defaults.table_name),
        region=source.get("AWS_REGION") or source.get("CLINIC_REGION") or defaults.region,
        nova_sonic_model_id=source.get(
            "CLINIC_NOVA_SONIC_MODEL_ID", defaults.nova_sonic_model_id
        ),
        analysis_interval_hours=interval,
        create_table_if_missing=source.get("CLINIC_CREATE_TABLE_IF_MISSING", "").lower()
        in {"1", "true", "yes"},
        endpoint_url=source.get("CLINIC_DYNAMODB_ENDPOINT_URL") or None,
        # No bucket => calls are not recorded. Recording patient audio is opt-in
        # by configuration, never by default.
        recordings_bucket=source.get("CLINIC_RECORDINGS_BUCKET") or None,
        recordings_prefix=source.get(
            "CLINIC_RECORDINGS_PREFIX", defaults.recordings_prefix
        ),
        recordings_sse=source.get("CLINIC_RECORDINGS_SSE", defaults.recordings_sse),
        recordings_kms_key_id=source.get("CLINIC_RECORDINGS_KMS_KEY_ID") or None,
        # No bucket => no uploaded documents and no document-backed answers.
        # Falls back to the recordings bucket when one is set and no separate
        # documents bucket is: the prefixes keep the two apart, and needing a
        # second bucket just to let a doctor upload a PDF is friction with no
        # security benefit. Set CLINIC_DOCUMENTS_BUCKET to separate them.
        documents_bucket=(
            source.get("CLINIC_DOCUMENTS_BUCKET")
            or source.get("CLINIC_RECORDINGS_BUCKET")
            or None
        ),
        documents_prefix=source.get(
            "CLINIC_DOCUMENTS_PREFIX", defaults.documents_prefix
        ),
        documents_sse=source.get("CLINIC_DOCUMENTS_SSE", defaults.documents_sse),
        documents_kms_key_id=source.get("CLINIC_DOCUMENTS_KMS_KEY_ID") or None,
        embedding_model_id=source.get(
            "CLINIC_EMBEDDING_MODEL_ID", defaults.embedding_model_id
        ),
        extraction_model_id=source.get(
            "CLINIC_EXTRACTION_MODEL_ID", defaults.extraction_model_id
        ),
    )


def build_asgi_app_from_env(env: Mapping[str, str] | None = None) -> Starlette:
    """Build the production ASGI app from the environment.

    Composes the DynamoDB-backed application (both agents plus the Dashboard BFF
    over one Data_Layer, Req 16.1) and binds it to the runtime contract routes.

    Set ``CLINIC_BACKEND=memory`` to compose over in-memory fakes instead, which
    removes the DynamoDB dependency for a local run of ``/ping`` and
    ``/invocations``. Note it does **not** remove the Bedrock dependency: ``/ws``
    still opens a real Nova Sonic stream, so a voice call needs AWS credentials
    and Nova Sonic access in the configured region either way.

    Set ``CLINIC_VOICE_ONLY=1`` to serve only the caller-facing voice routes and
    leave the doctor's dashboard, calendar, patient records and documents
    unrouted. That is the setting for a public host: dashboard access is decided
    by a ``?role=`` query parameter which is explicitly not a security control, so
    on a public URL anyone with the link would be the doctor.
    """
    source = env if env is not None else os.environ
    # Publishes the live-call console on a voice-only host, to whoever holds this
    # secret. Short tokens are rejected rather than quietly accepted: this guards live
    # patient speech and the ability to speak as the clinic, and a four-character
    # secret on a public URL is barely a gate at all.
    console_token = (source.get("CLINIC_CONSOLE_TOKEN") or "").strip()
    if console_token and len(console_token) < 24:
        raise ValueError(
            "CLINIC_CONSOLE_TOKEN must be at least 24 characters: it is the only "
            "thing standing between a public URL and a live patient call. Generate "
            "one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    voice_only = source.get("CLINIC_VOICE_ONLY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if source.get("CLINIC_BACKEND", "dynamodb").lower() == "memory":
        from .app import build_memory_application

        logger.warning(
            "CLINIC_BACKEND=memory: serving with in-memory stores; "
            "all data is lost when the container stops"
        )
        return create_asgi_app(
            build_memory_application(),
            voice_only=voice_only,
            console_token=console_token,
        )
    return create_asgi_app(
        build_runtime_application(runtime_config_from_env(source)),
        voice_only=voice_only,
        console_token=console_token,
    )


#: Module-level ASGI callable for ``uvicorn clinic_front_desk.deployment.server:app``.
#: Built lazily by :func:`main` so importing this module never touches AWS.
def main() -> None:  # pragma: no cover - process entry point
    """Serve the container on ``0.0.0.0:8080`` (the runtime contract's bind).

    Both ``/invocations`` and ``/ws`` are served from this one process and port,
    which is what lets the reactive voice agent and the scheduled intelligence
    agent share one Data_Layer inside one container.
    """
    import uvicorn

    logging.basicConfig(
        level=os.environ.get("CLINIC_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(
        build_asgi_app_from_env(),
        host=BIND_HOST,
        port=int(os.environ.get("PORT", BIND_PORT)),
        log_level=os.environ.get("CLINIC_LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":  # pragma: no cover - process entry point
    main()
