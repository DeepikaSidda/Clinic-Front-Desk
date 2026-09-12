/*
 * Browser voice client for the Voice_Front_Desk (/ws).
 *
 * Lets you actually talk to the agent: captures the microphone, streams it to
 * Nova Sonic over the same `/ws` transport AgentCore uses, and plays the spoken
 * reply back. No install and no extra dependency — `http://127.0.0.1` counts as a
 * secure context, so getUserMedia works without HTTPS.
 *
 * The audio contract on each side of the socket:
 *
 *   out  16 kHz mono 16-bit PCM, base64, ~32 ms per frame
 *        {message_type:"user_audio", audio, format:"pcm", sample_rate:16000, channels:1}
 *   in   24 kHz mono 16-bit PCM, base64
 *        {message_type:"agent_audio", audio, sample_rate:24000, ...}
 *
 * Three details that matter, and are easy to get wrong:
 *
 * 1. **Capture rate.** Nova Sonic wants 16 kHz but hardware usually runs at 44.1 or
 *    48 kHz. We ask for a 16 kHz AudioContext (Chrome and Edge honour it, so the
 *    worklet receives 16 kHz directly) and keep a linear resampler for browsers
 *    that quietly ignore the request. Sending 48 kHz samples labelled as 16 kHz
 *    makes the caller sound like a chipmunk and wrecks recognition.
 *
 * 2. **Playback scheduling.** Chunks arrive faster than real time, so each one is
 *    scheduled against a running playhead rather than played on arrival —
 *    otherwise they overlap into noise.
 *
 * 3. **Barge-in.** The server stops *producing* audio within 500 ms, but a client
 *    holding several buffered seconds would keep talking over you. On a `barge_in`
 *    message we drop every queued source, so the agent goes quiet immediately.
 */
(function () {
  "use strict";

  var CAPTURE_RATE = 16000; // what Nova Sonic expects in
  var PLAYBACK_RATE = 24000; // what Nova Sonic streams out
  var FRAME_SAMPLES = 512; // ~32 ms at 16 kHz

  // The mic worklet: accumulates 128-sample render blocks into frames, resampling
  // if the browser refused our requested rate, and posts Int16 frames to the page.
  var WORKLET_SOURCE = [
    "class CaptureProcessor extends AudioWorkletProcessor {",
    "  constructor(options) {",
    "    super();",
    "    var opts = (options && options.processorOptions) || {};",
    "    this.targetRate = opts.targetRate || 16000;",
    "    this.frameSamples = opts.frameSamples || 512;",
    "    this.ratio = sampleRate / this.targetRate;",
    "    this.buffer = [];",
    "    this.pos = 0;",
    "    this.muted = false;",
    "    this.port.onmessage = (e) => {",
    "      if (e.data && e.data.type === 'mute') this.muted = !!e.data.value;",
    "    };",
    "  }",
    "  process(inputs) {",
    "    var input = inputs[0];",
    "    if (!input || !input[0]) return true;",
    "    var channel = input[0];",
    "    var peak = 0;",
    "    for (var i = 0; i < channel.length; i++) {",
    "      var s = channel[i];",
    "      var a = s < 0 ? -s : s;",
    "      if (a > peak) peak = a;",
    "      this.buffer.push(this.muted ? 0 : s);",
    "    }",
    // Resample by walking a fractional read position. ratio === 1 when the
    // browser honoured our 16 kHz request, which is the common path.
    "    var out = [];",
    "    while (this.pos + this.ratio < this.buffer.length) {",
    "      var idx = Math.floor(this.pos);",
    "      var frac = this.pos - idx;",
    "      var a0 = this.buffer[idx];",
    "      var a1 = this.buffer[idx + 1] !== undefined ? this.buffer[idx + 1] : a0;",
    "      out.push(a0 + (a1 - a0) * frac);",
    "      this.pos += this.ratio;",
    "    }",
    "    var consumed = Math.floor(this.pos);",
    "    if (consumed > 0) {",
    "      this.buffer = this.buffer.slice(consumed);",
    "      this.pos -= consumed;",
    "    }",
    "    if (out.length) {",
    "      if (!this.pending) this.pending = [];",
    "      for (var j = 0; j < out.length; j++) this.pending.push(out[j]);",
    "      while (this.pending.length >= this.frameSamples) {",
    "        var frame = this.pending.splice(0, this.frameSamples);",
    "        var pcm = new Int16Array(frame.length);",
    "        for (var k = 0; k < frame.length; k++) {",
    "          var v = Math.max(-1, Math.min(1, frame[k]));",
    "          pcm[k] = v < 0 ? v * 0x8000 : v * 0x7fff;",
    "        }",
    "        this.port.postMessage({ pcm: pcm.buffer, peak: peak }, [pcm.buffer]);",
    "      }",
    "    }",
    "    return true;",
    "  }",
    "}",
    "registerProcessor('capture-processor', CaptureProcessor);",
  ].join("\n");

  // --- DOM ---------------------------------------------------------------

  var el = {};
  function bind() {
    [
      "start",
      "stop",
      "mute",
      "call-status",
      "session-id",
      "outcome",
      "sent",
      "received",
      "transcript",
      "transcript-empty",
      "meter",
      "hint",
    ].forEach(function (role) {
      el[role] = document.querySelector('[data-role="' + role + '"]');
    });
  }

  function setStatus(state, label) {
    if (!el["call-status"]) return;
    el["call-status"].setAttribute("data-state", state);
    el["call-status"].textContent = label;
  }

  function addLine(who, text) {
    if (!el.transcript) return;
    if (el["transcript-empty"]) el["transcript-empty"].remove();
    var li = document.createElement("li");
    li.className = "voice-client__line voice-client__line--" + who;
    var label = document.createElement("span");
    label.className = "voice-client__who";
    label.textContent = who === "user" ? "You" : "Agent";
    var body = document.createElement("span");
    body.className = "voice-client__said";
    body.textContent = text;
    li.appendChild(label);
    li.appendChild(body);
    el.transcript.appendChild(li);
    el.transcript.scrollTop = el.transcript.scrollHeight;
  }

  /**
   * Show the clinic's address and a tappable map link in the transcript.
   *
   * Built with createElement and textContent rather than an HTML string: the
   * address comes from a doctor-uploaded document, so interpolating it into
   * markup would make an upload a script-injection route. Only the href is a
   * URL, and it is one this page constructed.
   */
  function addClinicCard(message) {
    if (!el.transcript) return;
    if (el["transcript-empty"]) el["transcript-empty"].remove();

    var li = document.createElement("li");
    li.className = "voice-client__card";

    var title = document.createElement("span");
    title.className = "voice-client__card-title";
    title.textContent = "Clinic location";
    li.appendChild(title);

    if (message.address) {
      var address = document.createElement("span");
      address.className = "voice-client__card-address";
      address.textContent = message.address;
      li.appendChild(address);
    }

    if (message.maps_url) {
      var link = document.createElement("a");
      link.className = "voice-client__card-link";
      link.href = message.maps_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "Open in Google Maps";
      li.appendChild(link);
    }

    if (message.coordinates) {
      var coords = document.createElement("span");
      coords.className = "voice-client__card-coords";
      coords.textContent = message.coordinates;
      li.appendChild(coords);
    }

    el.transcript.appendChild(li);
    el.transcript.scrollTop = el.transcript.scrollHeight;
  }

  function addNote(text, kind) {
    if (!el.transcript) return;
    if (el["transcript-empty"]) el["transcript-empty"].remove();
    var li = document.createElement("li");
    li.className = "voice-client__note-line voice-client__note-line--" + (kind || "info");
    li.textContent = text;
    el.transcript.appendChild(li);
    el.transcript.scrollTop = el.transcript.scrollHeight;
  }

  // --- base64 <-> bytes ---------------------------------------------------

  function bytesToBase64(bytes) {
    var chunk = 0x8000;
    var parts = [];
    for (var i = 0; i < bytes.length; i += chunk) {
      parts.push(String.fromCharCode.apply(null, bytes.subarray(i, i + chunk)));
    }
    return btoa(parts.join(""));
  }

  function base64ToBytes(b64) {
    var raw = atob(b64);
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  // --- the call ----------------------------------------------------------

  var call = null;

  function createCall() {
    var state = {
      socket: null,
      micContext: null,
      playContext: null,
      stream: null,
      worklet: null,
      sources: [],
      playhead: 0,
      sentSamples: 0,
      receivedSamples: 0,
      ended: false,
    };

    function updateStats() {
      if (el.sent) {
        el.sent.textContent = (state.sentSamples / CAPTURE_RATE).toFixed(1) + " s";
      }
      if (el.received) {
        el.received.textContent =
          (state.receivedSamples / PLAYBACK_RATE).toFixed(1) + " s";
      }
    }

    // Drop everything queued so the agent stops talking now (barge-in).
    function flushPlayback() {
      state.sources.forEach(function (source) {
        try {
          source.stop();
        } catch (err) {
          /* already finished */
        }
      });
      state.sources = [];
      state.playhead = state.playContext ? state.playContext.currentTime : 0;
    }

    function enqueueAudio(bytes, rate) {
      var ctx = state.playContext;
      if (!ctx) return;
      var samples = bytes.length / 2;
      if (!samples) return;
      var view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
      var buffer = ctx.createBuffer(1, samples, rate || PLAYBACK_RATE);
      var channel = buffer.getChannelData(0);
      for (var i = 0; i < samples; i++) {
        channel[i] = view.getInt16(i * 2, true) / 0x8000;
      }
      var source = ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(ctx.destination);
      // Schedule against a running playhead: chunks arrive faster than real time,
      // so playing each on arrival would overlap them into noise.
      var startAt = Math.max(ctx.currentTime + 0.05, state.playhead);
      source.start(startAt);
      state.playhead = startAt + buffer.duration;
      state.sources.push(source);
      source.onended = function () {
        var i = state.sources.indexOf(source);
        if (i !== -1) state.sources.splice(i, 1);
      };
      state.receivedSamples += samples;
      updateStats();
    }

    function onMessage(event) {
      var message;
      try {
        message = JSON.parse(event.data);
      } catch (err) {
        return;
      }
      switch (message.message_type) {
        case "session_started":
          if (el["session-id"]) el["session-id"].textContent = message.session_id;
          setStatus("live", "Connected — speak now");
          addNote("Call connected. Say something.", "info");
          break;
        case "transcript":
          addLine(message.role === "assistant" ? "agent" : "user", message.text);
          break;
        case "clinic_card":
          addClinicCard(message);
          break;
        case "agent_audio":
          enqueueAudio(base64ToBytes(message.audio), message.sample_rate);
          break;
        case "barge_in":
          flushPlayback();
          addNote("You interrupted — playback stopped.", "info");
          break;
        case "session_ended":
          if (el.outcome) el.outcome.textContent = message.outcome || "—";
          addNote("Call ended (outcome: " + (message.outcome || "unknown") + ").", "end");
          break;
        default:
          break;
      }
    }

    async function start() {
      setStatus("connecting", "Requesting microphone");
      state.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });

      // Ask for 16 kHz directly; the worklet resamples if the browser declines.
      state.micContext = new AudioContext({ sampleRate: CAPTURE_RATE });
      state.playContext = new AudioContext({ sampleRate: PLAYBACK_RATE });
      await state.micContext.resume();
      await state.playContext.resume();
      state.playhead = state.playContext.currentTime;

      if (state.micContext.sampleRate !== CAPTURE_RATE) {
        addNote(
          "Browser captured at " +
            state.micContext.sampleRate +
            " Hz; resampling to 16 kHz.",
          "info"
        );
      }

      var blob = new Blob([WORKLET_SOURCE], { type: "application/javascript" });
      var url = URL.createObjectURL(blob);
      await state.micContext.audioWorklet.addModule(url);
      URL.revokeObjectURL(url);

      setStatus("connecting", "Connecting");
      var scheme = location.protocol === "https:" ? "wss" : "ws";
      state.socket = new WebSocket(scheme + "://" + location.host + "/ws");
      state.socket.binaryType = "arraybuffer";

      await new Promise(function (resolve, reject) {
        state.socket.addEventListener("open", resolve, { once: true });
        state.socket.addEventListener(
          "error",
          function () {
            reject(new Error("WebSocket failed to open"));
          },
          { once: true }
        );
      });

      state.socket.addEventListener("message", onMessage);
      state.socket.addEventListener("close", function () {
        if (!state.ended) {
          setStatus("offline", "Disconnected");
          addNote("Connection closed by the server.", "end");
        }
      });

      var source = state.micContext.createMediaStreamSource(state.stream);
      state.worklet = new AudioWorkletNode(state.micContext, "capture-processor", {
        numberOfInputs: 1,
        numberOfOutputs: 0,
        processorOptions: { targetRate: CAPTURE_RATE, frameSamples: FRAME_SAMPLES },
      });
      state.worklet.port.onmessage = function (event) {
        var data = event.data || {};
        if (typeof data.peak === "number" && el.meter) {
          el.meter.style.width = Math.min(100, data.peak * 140).toFixed(0) + "%";
        }
        if (!data.pcm || !state.socket || state.socket.readyState !== 1) return;
        var bytes = new Uint8Array(data.pcm);
        state.socket.send(
          JSON.stringify({
            message_type: "user_audio",
            audio: bytesToBase64(bytes),
            format: "pcm",
            sample_rate: CAPTURE_RATE,
            channels: 1,
          })
        );
        state.sentSamples += bytes.length / 2;
        updateStats();
      };
      source.connect(state.worklet);
    }

    function setMuted(muted) {
      if (state.worklet) state.worklet.port.postMessage({ type: "mute", value: muted });
    }

    async function stop() {
      state.ended = true;
      if (state.socket && state.socket.readyState === 1) {
        try {
          state.socket.send(JSON.stringify({ message_type: "end_session" }));
        } catch (err) {
          /* closing anyway */
        }
        // Give the server a moment to finalize and send session_ended.
        await new Promise(function (resolve) {
          setTimeout(resolve, 600);
        });
      }
      flushPlayback();
      if (state.worklet) state.worklet.disconnect();
      if (state.stream) {
        state.stream.getTracks().forEach(function (track) {
          track.stop();
        });
      }
      [state.micContext, state.playContext].forEach(function (ctx) {
        if (ctx && ctx.state !== "closed") ctx.close();
      });
      if (state.socket && state.socket.readyState <= 1) state.socket.close();
      if (el.meter) el.meter.style.width = "0%";
    }

    return { start: start, stop: stop, setMuted: setMuted };
  }

  // --- wiring ------------------------------------------------------------

  function unsupported() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return "This browser has no microphone API. Use Chrome, Edge, or Firefox.";
    }
    if (typeof AudioWorkletNode === "undefined") {
      return "This browser lacks AudioWorklet, which the capture pipeline needs.";
    }
    if (!window.isSecureContext) {
      return (
        "Microphone access needs a secure context. Open this page on " +
        "http://127.0.0.1:8080/voice (localhost counts as secure) or serve it over HTTPS."
      );
    }
    return null;
  }

  function boot() {
    bind();

    var blocker = unsupported();
    if (blocker) {
      setStatus("offline", "Unavailable");
      if (el.hint) el.hint.textContent = blocker;
      if (el.start) el.start.disabled = true;
      return;
    }

    el.start.addEventListener("click", async function () {
      el.start.disabled = true;
      call = createCall();
      try {
        await call.start();
        el.stop.disabled = false;
      } catch (err) {
        setStatus("offline", "Failed");
        // The overwhelmingly common cause is a denied mic permission, so say so
        // rather than showing a bare DOMException name.
        var message =
          err && err.name === "NotAllowedError"
            ? "Microphone permission was denied. Allow it in the browser's site settings and try again."
            : "Could not start the call: " + (err && err.message ? err.message : err);
        addNote(message, "error");
        el.start.disabled = false;
        call = null;
      }
    });

    el.stop.addEventListener("click", async function () {
      el.stop.disabled = true;
      setStatus("idle", "Ending call");
      if (call) await call.stop();
      call = null;
      setStatus("idle", "Not connected");
      el.start.disabled = false;
    });

    el.mute.addEventListener("change", function () {
      if (call) call.setMuted(el.mute.checked);
    });

    window.addEventListener("beforeunload", function () {
      if (call) call.stop();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
