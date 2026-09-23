/**
 * Run the live console's real client script and report what it renders.
 *
 * The console's JavaScript had no coverage at all, and it shipped a bug that made
 * the page useless: the button row was never populated, so a call could not be
 * picked up by any means. A Python test asserting the page *contains* the string
 * "data-talk" passed the whole time — the function was in the source and simply
 * never ran.
 *
 * So this executes it. Not a full DOM: just enough of one that `createCard` and
 * `updateCard` behave as they do in a browser, which is where the bug lived (a
 * state-transition guard comparing against a sentinel that collided with a real
 * value). Given a page of HTML on argv[2], prints JSON describing the controls
 * rendered for a call in each state.
 */

import { readFileSync } from "node:fs";

function makeEl(tag) {
  const el = {
    tagName: tag,
    dataset: {},
    style: {},
    className: "",
    textContent: "",
    _children: {},
    _html: "",
    get innerHTML() {
      return this._html;
    },
    set innerHTML(value) {
      this._html = value;
      // Stand in for parsing: every data-* hook in the markup becomes a child that
      // querySelector can return, which is all the console's code asks of the DOM.
      const pattern = /data-([a-z]+)/g;
      let match;
      while ((match = pattern.exec(value)) !== null) {
        const key = "data-" + match[1];
        if (!this._children[key]) this._children[key] = makeEl("stub");
      }
    },
    querySelector(selector) {
      const match = selector.match(/^\[(data-[a-z]+)/);
      return match ? this._children[match[1]] || null : null;
    },
    querySelectorAll() {
      return [];
    },
    appendChild() {},
    remove() {},
    focus() {},
    addEventListener() {},
    getAttribute() {
      return null;
    },
    setAttribute() {},
    removeAttribute() {},
    scrollTop: 0,
    scrollHeight: 0,
    clientHeight: 0,
  };
  return el;
}

const page = readFileSync(process.argv[2], "utf8");
const opened = page.indexOf("<script>");
const closed = page.lastIndexOf("</script>");
if (opened < 0 || closed < 0) {
  console.log(JSON.stringify({ error: "no script block on the page" }));
  process.exit(0);
}
const script = page.slice(opened + "<script>".length, closed);

// The console's script is an IIFE, so nothing inside it is reachable from out here.
// Unwrap it and hand back the two functions under test.
const bodyStart = script.indexOf("{");
const bodyEnd = script.lastIndexOf("})();");
const body = script.slice(bodyStart + 1, bodyEnd);

globalThis.location = { search: "?role=doctor", protocol: "http:", host: "127.0.0.1:8080" };
// Resolving as not-ok makes refresh() return immediately, so loading the script does
// not try to drive a whole page.
globalThis.fetch = () => Promise.resolve({ ok: false, json: () => Promise.resolve({}) });
globalThis.document = {
  addEventListener() {},
  getElementById() {
    return makeEl("div");
  },
  querySelector() {
    return null;
  },
  createElement: makeEl,
  title: "",
};
globalThis.setInterval = () => 0;
globalThis.setTimeout = () => 0;
globalThis.AudioContext = function () {
  throw new Error("no audio in this harness");
};
// Node 22 defines a real, getter-only `navigator`, so plain assignment throws.
Object.defineProperty(globalThis, "navigator", {
  value: { mediaDevices: { getUserMedia: () => Promise.reject(new Error("no mic")) } },
  configurable: true,
  writable: true,
});
globalThis.WebSocket = function () {};

let api;
try {
  api = new Function(
    body + "\n; return { createCard: createCard, updateCard: updateCard };"
  )();
} catch (error) {
  console.log(JSON.stringify({ error: "script failed to load: " + error.message }));
  process.exit(0);
}

function controlsFor(call) {
  const card = api.createCard(call);
  api.updateCard(card, call);
  // A second pass, because the poll runs every two seconds and must not wipe them.
  api.updateCard(card, call);
  const row = card.querySelector("[data-row]");
  return row ? row.innerHTML : "";
}

const waiting = {
  session_id: "abc",
  needs_human: true,
  taken_over: false,
  reason: "patient_request",
  reason_label: "Asked to speak to a person",
  patient_name: "",
  callback_phone: "",
};
const live = Object.assign({}, waiting, { taken_over: true });

const waitingRow = controlsFor(waiting);
const liveRow = controlsFor(live);

console.log(
  JSON.stringify(
    {
      waiting: {
        html: waitingRow,
        hasTalk: waitingRow.includes("data-talk"),
        hasTake: waitingRow.includes("data-take"),
      },
      live: {
        html: liveRow,
        hasTalk: liveRow.includes("data-talk"),
        hasSay: liveRow.includes("data-say"),
        hasRelease: liveRow.includes("data-release"),
      },
    },
    null,
    2
  )
);
