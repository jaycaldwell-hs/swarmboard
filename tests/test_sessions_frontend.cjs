const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../swarmboard/static/sessions.js"), "utf8");
const settle = () => new Promise(resolve => setImmediate(resolve));

function browser({ legacy = false, continuous = true, state = "running", loseFirstResponse = false } = {}) {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, { listeners: {}, hidden: false, disabled: false, innerHTML: "",
      addEventListener(name, callback) { this.listeners[name] = callback; }, showModal() {}, close() {} });
    return elements.get(id);
  }
  const run = { id: "saved", thread_id: "thread-saved", state, continuous,
    config: { experiment: true, interaction_mode: legacy ? "scripted" : "autonomous", scenario_name: "Saved" } };
  const report = { run_id: "saved", title: "Saved", state, archived: legacy, cadence: null,
    participants: [{ id: "peer", handle: "peer", provider: "ollama", model: "local", available: true }],
    metrics: { turns_used: 0, tokens_used: 0, threads: 1, new_thread: 0, pass: 0, failed_turns: 0 }, actions: [] };
  const form = { agent_ids: ["peer"], title: "Discussion", body: "Hello", cadence: "free", continuous: "on",
    max_rounds: "7", max_tokens: "1200", max_duration_seconds: "80" };
  const requests = [];
  let sequence = 0, first = true;
  const context = {
    document: { hidden: false, getElementById: element },
    location: { pathname: "/sessions", search: "?run=saved" },
    history: { replaceState() {} }, window: { addEventListener() {}, confirm: () => true },
    URLSearchParams, setInterval() {}, crypto: { randomUUID: () => `request-${++sequence}` },
    FormData: class { get(name) { return form[name] ?? null; } getAll(name) { return form[name] || []; } },
    fetch: async (url, options = {}) => {
      if (options.method === "POST") {
        requests.push({ url, body: JSON.parse(options.body) });
        if (url === "/api/sessions" && loseFirstResponse && first) { first = false; throw new Error("lost response"); }
        return { ok: true, json: async () => url.endsWith("/rerun") ? { run: { id: "saved" } } : { run_id: "saved", thread_id: "thread-saved" } };
      }
      return { ok: true, json: async () => url === "/api/state" ? { agents: [{ id: "peer", handle: "peer", model: "local", enabled: true }], runs: [run] } : report };
    },
  };
  vm.runInNewContext(source, context);
  return { element, requests, report, form };
}


test("old autonomous sessions remain active, with correct board links", async () => {
  const b = browser(); await settle();
  assert.match(b.element("controls").innerHTML, /data-control="pause"/);
  assert.match(b.element("controls").innerHTML, /data-control="rerun"/);
  assert.doesNotMatch(b.element("sessions").innerHTML, /Archived/);
  assert.equal(b.element("thread-link").href, "/#thread=thread-saved");
});

test("scripted archives show export but no execution controls", async () => {
  const b = browser({ legacy: true }); await settle();
  assert.match(b.element("sessions").innerHTML, /Archived/);
  assert.match(b.element("controls").innerHTML, /\/export/);
  assert.doesNotMatch(b.element("controls").innerHTML, /data-control=/);
});

test("manual setups retain Step once without an automatic start control", async () => {
  const b = browser({ state: "created", continuous: false }); await settle();
  assert.match(b.element("controls").innerHTML, /data-control="step"/);
  assert.doesNotMatch(b.element("controls").innerHTML, /data-control="start"/);
});

test("retry after lost creation response reuses the request identity", async () => {
  const b = browser({ loseFirstResponse: true }); await settle();
  const form = b.element("session-form");
  const submit = () => form.listeners.submit({ preventDefault() {}, target: form });
  submit(); await settle();
  assert.equal(b.element("create").disabled, false);
  submit(); await settle();
  assert.equal(b.requests.length, 2);
  assert.equal(b.requests[0].body.idempotency_key, b.requests[1].body.idempotency_key);
  assert.equal(b.requests[1].body.cadence, "free");
  assert.equal(b.requests[1].body.max_duration_seconds, 80);
  assert.deepEqual(b.requests[1].body.agent_ids, ["peer"]);
  assert.equal("seed" in b.requests[1].body, false);
});

test("restart follows the nested run response and safety blocks hide restart", async () => {
  const b = browser(); await settle();
  const button = { dataset: { control: "rerun" }, disabled: false };
  b.element("controls").listeners.click({ target: { closest: () => button } });
  await settle();
  assert.equal(b.requests[0].url, "/api/runs/saved/rerun");
  assert.equal(b.element("error").hidden, true);
  b.report.stop_reason = "safety_block: provider refused";
  const selector = { dataset: { run: "saved" } };
  b.element("sessions").listeners.click({ target: { closest: () => selector } });
  await settle();
  assert.doesNotMatch(b.element("controls").innerHTML, /data-control="rerun"/);
});
