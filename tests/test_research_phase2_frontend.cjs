const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const read = name => fs.readFileSync(path.join(__dirname, "../swarmboard/static/", name), "utf8");
const settle = () => new Promise(resolve => setImmediate(resolve));

function helper(extra = {}) {
  const context = {window: {}, document: {addEventListener() {}}, URLSearchParams, ...extra};
  vm.runInNewContext(read("research.js"), context);
  return context.window.SwarmResearch;
}
function values(data) { return {get: name => data[name] ?? null, getAll: name => data[name] || []}; }

test("fork and resample preserve manual defaults, budgets, prompt reuse, and retry identity", () => {
  const tools = helper();
  const fork = tools.forkPayload(values({agent_ids: ["ada", "peer"], policy: "inherit"}), "retry", "post/1");
  assert.equal(fork.at_post_id, "post/1");
  assert.equal(fork.continuous, false);
  assert.equal(fork.inherit_remaining, false);
  assert.equal(fork.idempotency_key, "retry");
  assert.equal("policy" in fork, false);
  const custom = tools.forkPayload(values({agent_ids: ["ada"], policy_json: '{"dedup":false}',
    inherit_remaining: "on", max_tokens: "900", continuous: "on"}), "custom", "source");
  assert.equal(custom.policy.dedup, false);
  assert.equal(custom.limits.max_tokens, 900);
  assert.equal(custom.inherit_remaining, true);
  assert.equal(custom.continuous, true);
  const sample = tools.resamplePayload(values({n: "3", reuse_prompt: "on"}), "samples");
  assert.equal(sample.n, 3);
  assert.equal(sample.reuse_prompt, true);
  assert.equal(sample.idempotency_key, "samples");
});

test("participant views select exact agent and historical post without unsafe URL interpolation", () => {
  const tools = helper();
  const url = tools.participantViewUrl("thread/1", "agent&2", "post?3");
  assert.match(url, /^\/api\/threads\/thread%2F1\/participant-view\?/);
  const query = new URLSearchParams(url.split("?")[1]);
  assert.equal(query.get("agent_id"), "agent&2");
  assert.equal(query.get("at_post_id"), "post?3");
  assert.equal(new URLSearchParams(tools.participantViewUrl("t", "a").split("?")[1]).has("at_post_id"), false);
});

test("external-looking lineage identifiers cannot turn source navigation into offsite links", () => {
  const tools = helper();
  const external = '//observer.invalid/collect?token=private" onmouseover="alert(1)';
  const run = {id: "fork", session_type: "research", lineage: {parent_thread_id: external, parent_post_id: external}};
  const html = tools.navigationHtml(run, [run]);
  const links = Array.from(html.matchAll(/href="([^"]+)"/g), match => match[1]);
  assert.equal(links.length, 2);
  for (const href of links) {
    const url = new URL(href, "https://board.test");
    assert.equal(url.origin, "https://board.test");
    assert.equal(url.pathname, "/");
    assert.equal(new URLSearchParams(url.hash.slice(1)).get("thread"), external);
  }
  assert.doesNotMatch(html, / onmouseover="/);
});

test("fork navigation nests descendants, collapses siblings around selection, and hides research", () => {
  const tools = helper();
  const runs = [
    {id: "base", session_type: "collaboration"},
    {id: "fork", session_type: "research", lineage: {parent_thread_id: "original"}},
    {id: "sample-a", session_type: "research", sibling_group_id: "group", lineage: {parent_thread_id: "forked"}},
    {id: "sample-b", session_type: "research", sibling_group_id: "group", lineage: {parent_thread_id: "forked"}},
  ];
  const threads = [{id: "b", run_id: "sample-b"}, {id: "a", run_id: "sample-a"}, {id: "forked", run_id: "fork"}, {id: "original", run_id: "base"}];
  const entries = tools.threadEntries(threads, runs, "a");
  assert.deepEqual(Array.from(entries, item => item.thread.id), ["original", "forked", "a"]);
  assert.deepEqual(Array.from(entries, item => item.depth), [0, 1, 2]);
  assert.equal(entries[2].siblingCount, 2);
  assert.deepEqual(Array.from(tools.threadEntries(threads, runs, "a", true), item => item.thread.id), ["original"]);
  assert.equal(tools.navigationHtml(runs[0], runs), "");
  assert.match(tools.navigationHtml({...runs[2], thread_id: "a"}, runs), /source discussion/);
  assert.match(tools.navigationHtml({...runs[2], thread_id: "a"}, runs), /Sample 2/);
});

test("resample grouping counts runs and preserves their additional discussions", () => {
  const tools = helper();
  const runs = [{id: "base", session_type: "collaboration"},
    {id: "a", thread_id: "a-extra", session_type: "research", sibling_group_id: "samples", lineage: {parent_thread_id: "original"}},
    {id: "b", thread_id: "b-extra", session_type: "research", sibling_group_id: "samples", lineage: {parent_thread_id: "original"}}];
  const threads = [{id: "a-extra", run_id: "a", created_at: "2026-09-09T00:02:00"},
    {id: "b-extra", run_id: "b", created_at: "2026-09-09T00:03:00"},
    {id: "b-root", run_id: "b", created_at: "2026-09-09T00:01:00"},
    {id: "a-root", run_id: "a", created_at: "2026-09-09T00:01:00"},
    {id: "original", run_id: "base", created_at: "2026-09-09T00:00:00"}];
  const entries = tools.threadEntries(threads, runs, "a-extra");
  assert.deepEqual(Array.from(entries, item => item.thread.id), ["original", "a-root", "a-extra", "b-extra"]);
  assert.equal(entries[1].siblingCount, 2);
  assert.equal(entries[2].siblingCount, 0);
  assert.equal(entries[3].siblingCount, 0);
  const navigation = tools.navigationHtml(runs[1], runs, threads);
  assert.match(navigation, /#thread=a-root/);
  assert.match(navigation, /#thread=b-root/);
  assert.doesNotMatch(navigation, /#thread=a-extra|#thread=b-extra/);
});

test("participant view displays complete system messages including private instructions and memories", async () => {
  const elements = new Map();
  const element = name => {
    if (!elements.has(name)) elements.set(name, {listeners: {}, disabled: false, addEventListener(name, callback) { this.listeners[name] = callback; }});
    return elements.get(name);
  };
  const form = element("form");
  form.querySelector = element;
  const root = {innerHTML: "", querySelector: element};
  let modal;
  const tools = helper({
    document: {addEventListener() {}, body: {appendChild() {}}, createElement() {
      modal = {innerHTML: "", querySelector: element, querySelectorAll: () => [], addEventListener() {}, showModal() {}};
      return modal;
    }},
    FormData: class {get(name) { return {agent_id: "ada", thread_id: "thread"}[name] || ""; }},
    fetch: async () => ({ok: true, json: async () => ({prompt_sha256: "captured-hash", messages: [
      {role: "system", content: "Private instruction: inspect the assumption. Seeded memory: prior observation."},
      {role: "user", content: "Public discussion"},
    ]})}),
  });
  tools.renderPanel(root, {run: {id: "run", state: "paused", config: {}},
    thread: {id: "thread", run_id: "run"}, agents: [{id: "ada", handle: "ada"}]});
  await element('[data-command="view"]').listeners.click({target: element('[data-command="view"]')});
  assert.match(modal.innerHTML, /Private instruction: inspect the assumption/);
  assert.match(modal.innerHTML, /Seeded memory: prior observation/);
  assert.match(modal.innerHTML, /Public discussion/);
  assert.match(modal.innerHTML, /captured-hash/);
});

test("force next uses explicit cooldown override and reuses request key after a lost response", async () => {
  const fields = new Map();
  const element = selector => {
    if (!fields.has(selector)) fields.set(selector, {listeners: {}, disabled: false,
      addEventListener(name, callback) { this.listeners[name] = callback; }});
    return fields.get(selector);
  };
  const form = element("form");
  form.querySelector = element;
  const root = {querySelector: element, innerHTML: ""};
  const requests = [];
  const data = {agent_id: "peer", thread_id: "thread", at_post_id: "post", override_cooldown: "on"};
  let sequence = 0;
  const tools = helper({crypto: {randomUUID: () => `request-${++sequence}`},
    FormData: class {get(name) { return data[name] ?? null; }},
    fetch: async (url, options) => {
      requests.push({url, body: JSON.parse(options.body)});
      if (requests.length === 1) throw new Error("lost response");
      return {ok: true, json: async () => ({stimulus_id: "queued"})};
    }});
  const config = {run: {id: "run", state: "paused", session_type: "collaboration", config: {agent_ids: ["peer"]}},
    thread: {id: "thread", run_id: "run", posts: [{id: "post", sequence: 1, author_handle: "human"}]}, agents: [{id: "peer", handle: "peer"}]};
  tools.renderPanel(root, config);
  assert.match(root.innerHTML, /Force next/);
  assert.match(root.innerHTML, /Participant view/);
  assert.doesNotMatch(root.innerHTML, /Fork here|Resample/);
  const submit = () => form.listeners.submit({preventDefault() {}});
  await submit();
  await submit();
  assert.equal(requests.length, 2);
  assert.equal(requests[0].url, "/api/runs/run/force-turn");
  assert.equal(requests[0].body.idempotency_key, requests[1].body.idempotency_key);
  assert.equal(requests[1].body.override_cooldown, true);
  assert.equal(requests[1].body.stimulus_post_id, "post");
  element("details").open = true;
  element("details").listeners.toggle();
  form.listeners.input();
  tools.renderPanel(root, config);
  assert.match(root.innerHTML, /class="session-tools" open/);
  assert.match(root.innerHTML, /name="override_cooldown" checked/);
  assert.match(root.innerHTML, /Turn queued/);
});

function board(sessionType) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {id, listeners: {}, hidden: false, disabled: false, innerHTML: "", value: "", style: {},
      lastChild: {textContent: ""}, classList: {contains: () => false, toggle() {}, add() {}, remove() {}},
      addEventListener(name, callback) { this.listeners[name] = callback; }, setAttribute() {},
      querySelector: () => null, querySelectorAll: () => [], appendChild() {}});
    return elements.get(id);
  };
  const listeners = {};
  const thread = {id: "thread", run_id: "run", title: "Discussion", status: "active",
    posts: [{id: "post", sequence: 1, author_type: "human", author_handle: "human", body: "Opening", is_inherited: sessionType === "research"}]};
  const run = {id: "run", thread_id: "thread", state: "paused", session_type: sessionType, config: {collaboration: true}};
  const event = {id: 1, run_id: "run", thread_id: "thread", event_type: "turn.completed", payload: {turn_id: "turn"}};
  const tools = helper();
  const panels = [], forks = [], resamples = [];
  const context = {document: {getElementById: element, querySelectorAll: () => [], createElement: element,
      addEventListener(name, callback) { listeners[name] = callback; }},
    window: {SwarmResearch: {...tools, renderPanel(root, config) { panels.push(config); }, openFork: config => forks.push(config), openResample: config => resamples.push(config)},
      addEventListener() {}, setTimeout() {}, clearTimeout() {}},
    location: {hash: "#thread=thread"}, history: {replaceState() {}}, navigator: {onLine: false}, URLSearchParams,
    fetch: async url => ({ok: true, headers: {get: () => "application/json"}, json: async () => url.startsWith("/api/turns/")
      ? {id: "turn", agent_id: "peer", session_type: sessionType, state: "completed", policy_snapshot: {profile: "production"}}
      : {agents: [{id: "peer", handle: "peer"}], threads: [thread], selected_thread: thread, runs: [run], events: [event], server: {}}}),
  };
  vm.runInNewContext(read("app.js"), context);
  listeners.DOMContentLoaded();
  return {element, panels, forks, resamples};
}

test("board exposes fork and resample only for research and participant tools for both types", async () => {
  for (const type of ["collaboration", "research"]) {
    const b = board(type); await settle();
    assert.equal(b.panels.length, 1);
    assert.equal(b.panels[0].run.session_type, type);
    b.element("event-timeline").listeners.click({target: {closest: () => ({dataset: {eventId: "1"}})}});
    await settle();
    if (type === "research") {
      assert.match(b.element("post-feed").innerHTML, /data-fork-post-id="post"/);
      assert.match(b.element("post-feed").innerHTML, /Inherited/);
      assert.match(b.element("turn-inspector").innerHTML, /data-resample-turn="turn"/);
      b.element("post-feed").listeners.click({target: {closest: selector => selector === "[data-fork-post-id]" ? {dataset: {forkPostId: "post"}} : null}});
      assert.equal(b.forks[0].post.id, "post");
      b.element("turn-inspector").listeners.click({target: {closest: selector => selector === "[data-resample-turn]" ? {dataset: {resampleTurn: "turn"}} : null}});
      assert.equal(b.resamples[0].turnId, "turn");
    } else {
      assert.doesNotMatch(b.element("post-feed").innerHTML, /Fork here|Inherited/);
      assert.doesNotMatch(b.element("turn-inspector").innerHTML, /Resample/);
    }
  }
});
