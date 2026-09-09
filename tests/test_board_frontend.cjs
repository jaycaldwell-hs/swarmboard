const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../swarmboard/static/app.js"), "utf8");
const settle = () => new Promise(resolve => setImmediate(resolve));

function browser({ state = "running", runId = "session", threadId = "secondary", representative = "opening" } = {}) {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      id, listeners: {}, hidden: false, disabled: false, innerHTML: "", textContent: "", value: "", style: {},
      lastChild: { textContent: "" },
      classList: { contains: () => false, toggle() {}, add() {}, remove() {} },
      addEventListener(name, callback) { this.listeners[name] = callback; },
      setAttribute() {}, querySelector: () => null, querySelectorAll: () => [], appendChild() {},
    });
    return elements.get(id);
  }
  const documentListeners = {};
  const selectedThread = { id: threadId, run_id: runId, title: "Follow-up discussion", status: "active", posts: [] };
  const run = { id: "session", thread_id: representative, state, continuous: true,
    config: { collaboration: true, interaction_mode: "autonomous" } };
  // A stale representative must not take precedence over the thread's actual session.
  const stale = { id: "different-session", thread_id: threadId, state: "running", config: {} };
  const context = {
    document: { addEventListener(name, callback) { documentListeners[name] = callback; },
      getElementById: element, querySelectorAll: () => [], createElement: element },
    location: { hash: `#thread=${threadId}` },
    history: { replaceState() {} }, navigator: { onLine: false }, URLSearchParams,
    window: { addEventListener() {}, setTimeout() {}, clearTimeout() {} },
    fetch: async () => ({ ok: true, headers: { get: () => "application/json" },
      json: async () => ({ agents: [], threads: [selectedThread], selected_thread: selectedThread,
        runs: [stale, run], events: [], server: {} }) }),
  };
  vm.runInNewContext(source, context);
  documentListeners.DOMContentLoaded();
  return { element };
}

test("secondary threads retain controls and activity links for their own session", async () => {
  const b = browser(); await settle();
  assert.match(b.element("run-content").innerHTML, /\/sessions\?run=session/);
  assert.match(b.element("run-content").innerHTML, /data-run-id="session" data-run-action="pause"/);
  assert.doesNotMatch(b.element("run-content").innerHTML, /different-session/);
  assert.equal(b.element("replay-run-button").hidden, false);
  assert.equal(b.element("post-body").disabled, false);
  assert.equal(b.element("thread-run-button").lastChild.textContent, " View session");
});

test("completed sessions make secondary thread composers read-only", async () => {
  const b = browser({ state: "completed" }); await settle();
  assert.equal(b.element("post-body").disabled, true);
  assert.equal(b.element("post-submit").disabled, true);
  assert.match(b.element("run-content").innerHTML, /data-action="replay-run" data-run-id="session"/);
  assert.doesNotMatch(b.element("run-content").innerHTML, /data-run-action="pause"/);
});

test("threads explicitly without a session ignore stale run representatives", async () => {
  const b = browser({ runId: null }); await settle();
  assert.match(b.element("run-content").innerHTML, /No session yet/);
  assert.equal(b.element("replay-run-button").hidden, true);
  assert.equal(b.element("thread-run-button").lastChild.textContent, " Invite board");
});
