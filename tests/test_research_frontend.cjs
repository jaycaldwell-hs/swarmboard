const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const read = name => fs.readFileSync(path.join(__dirname, "../swarmboard/static/", name), "utf8");
const settle = () => new Promise(resolve => setImmediate(resolve));

function researchForm() {
  const fields = new Map();
  const field = name => {
    if (!fields.has(name)) fields.set(name, {value: "", checked: false, listeners: {},
      addEventListener(event, callback) { this.listeners[event] = callback; }});
    return fields.get(name);
  };
  const policy = field("policy");
  const root = {querySelector(selector) {
    return selector === "[data-research-policy]" ? policy : field(selector.match(/name="(.*?)"/)[1]);
  }};
  field("policy_profile").value = "production";
  for (const name of ["dedup", "loop_prevention", "cooldowns", "dormancy"]) field("policy_" + name).checked = true;
  field("policy_consecutive_turn_cap").value = "1";
  field("policy_schema_mode").value = "strict";
  const data = {get(name) {
    if (name === "research" || ["dedup", "loop_prevention", "cooldowns", "dormancy"].some(toggle => name === "policy_" + toggle)) {
      return field(name).checked ? "on" : null;
    }
    return field(name).value;
  }};
  const context = {window: {}, document: {addEventListener() {}}};
  vm.runInNewContext(read("research.js"), context);
  const helper = context.window.SwarmResearch;
  helper.bind(root);
  return {field, policy, data, helper};
}

test("collaboration hides and disables policy settings and keeps the original creation payload", () => {
  const b = researchForm();
  assert.equal(b.policy.hidden, true);
  assert.equal(b.policy.disabled, true);
  assert.equal(JSON.stringify(b.helper.creationOptions(b.data)), "{}");
});

test("research preset and custom knobs round-trip and opting out drops all research fields", () => {
  const b = researchForm();
  b.field("research").checked = true;
  b.field("research").listeners.change();
  assert.equal(b.policy.hidden, false);
  assert.equal(b.policy.disabled, false);
  b.field("policy_profile").value = "permissive";
  b.field("policy_profile").listeners.change();
  const payload = b.helper.creationOptions(b.data);
  assert.equal(payload.session_type, "research");
  assert.equal(payload.policy.profile, "permissive");
  assert.equal(payload.policy.dedup, false);
  assert.equal(payload.policy.consecutive_turn_cap, null);
  assert.equal(payload.policy.schema_mode, "capture");
  b.field("policy_cooldown_seconds").value = "30";
  b.policy.listeners.input({target: b.field("policy_cooldown_seconds")});
  assert.equal(b.helper.creationOptions(b.data).policy.profile, "custom");
  assert.equal(b.helper.creationOptions(b.data).policy.cooldown_seconds, 30);
  b.field("research").checked = false;
  b.field("research").listeners.change();
  assert.equal(JSON.stringify(b.helper.creationOptions(b.data)), "{}");
});

test("sessions submit selected research policy and label only research activity", async () => {
  const form = researchForm();
  form.field("research").checked = true;
  form.field("policy_profile").value = "permissive";
  form.field("policy_profile").listeners.change();
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {listeners: {}, innerHTML: "", hidden: false,
      addEventListener(name, callback) { this.listeners[name] = callback; }});
    return elements.get(id);
  };
  const report = {run_id: "research", title: "Research", state: "created", session_type: "research",
    policy: {profile: "permissive"}, participants: [], metrics: {}, actions: []};
  const requests = [];
  const context = {
    window: {SwarmResearch: form.helper, addEventListener() {}},
    document: {getElementById: element, hidden: false},
    location: {pathname: "/sessions", search: "?run=research"}, history: {replaceState() {}},
    URLSearchParams, crypto: {randomUUID: () => "request"}, setInterval() {},
    FormData: class {
      get(name) { return ({title: "Research", body: "Opening", cadence: "free", max_rounds: "5",
        max_tokens: "1000", max_duration_seconds: "60"})[name] ?? form.data.get(name); }
      getAll() { return ["peer"]; }
    },
    fetch: async (url, options = {}) => {
      if (options.method === "POST") {
        requests.push(JSON.parse(options.body));
        return {ok: true, json: async () => ({run_id: "research"})};
      }
      return {ok: true, json: async () => url === "/api/state" ? {agents: [], runs: [{id: "research",
        state: "created", config: {collaboration: true, session_type: report.session_type}}]} : report};
    },
  };
  vm.runInNewContext(read("sessions.js"), context);
  await settle();
  assert.match(element("sessions").innerHTML, /research-badge/);
  assert.equal(element("report-policy").hidden, false);
  assert.equal(element("report-policy").textContent, "Research · permissive policy");
  const sessionForm = element("session-form");
  sessionForm.listeners.submit({preventDefault() {}, target: sessionForm});
  await settle();
  assert.equal(requests[0].session_type, "research");
  assert.equal(requests[0].policy.schema_mode, "capture");
  report.session_type = "collaboration";
  element("sessions").listeners.click({target: {closest: () => ({dataset: {run: "research"}})}});
  await settle();
  assert.equal(element("report-policy").hidden, true);
  assert.doesNotMatch(element("sessions").innerHTML, /research-badge/);
});
