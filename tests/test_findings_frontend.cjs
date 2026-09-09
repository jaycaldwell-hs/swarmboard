const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const read = name => fs.readFileSync(path.join(__dirname, "../swarmboard/static/", name), "utf8");
const settle = () => new Promise(resolve => setImmediate(resolve));

function browser(api = async () => ({})) {
  const modals = [];
  const context = {window: {SwarmResearch: {api, commandDialog(title, html, label, submit) {
    modals.push({title, html, label, submit});
    return {querySelectorAll: () => []};
  }}}};
  vm.runInNewContext(read("findings.js"), context);
  return {tools: context.window.SwarmFindings, modals};
}
const form = data => ({get: name => data[name] ?? null});

test("free tags and suggested tags use attributed append-only flag, note, and resolution endpoints", async () => {
  const requests = [];
  const b = browser(async (url, body) => { requests.push({url, body}); return {}; });
  let saved = 0;
  b.tools.openFlag({runId: "run", targetType: "post", targetId: "post/one",
    suggestedTags: ["novel-tag", '<unsafe>'], onSaved: () => { saved++; }});
  assert.match(b.modals[0].html, /novel-tag/);
  assert.doesNotMatch(b.modals[0].html, /<unsafe>/);
  await b.modals[0].submit(form({tags: " unusual , unusual, custom tag ", body: "Observation"}), "flag-key");
  assert.equal(requests[0].url, "/api/posts/post%2Fone/flags");
  assert.deepEqual(Array.from(requests[0].body.tags), ["unusual", "custom tag"]);
  assert.equal(requests[0].body.idempotency_key, "flag-key");
  b.tools.openNote({runId: "run", onSaved: () => { saved++; }});
  await b.modals[1].submit(form({body: "Session context", tags: "free"}), "note-key");
  assert.equal(requests[1].url, "/api/runs/run/notes");
  const original = {id: 41, target_type: "post", target_id: "post/one", body: "Observation", tags: ["unusual"], resolved: false};
  const before = JSON.stringify(original);
  b.tools.openResolve({flag: original, onSaved: () => { saved++; }});
  await b.modals[2].submit(form({body: "Checked the evidence", tags: "resolved"}), "resolve-key");
  assert.equal(requests[2].url, "/api/flags/41/resolve");
  assert.equal(JSON.stringify(original), before);
  assert.equal(saved, 3);
});

test("finding history keeps original flag, resolution author, and note text visible and escaped", () => {
  const b = browser();
  const html = b.tools.findingsHtml({flags: [{id: 1, target_type: "turn", target_id: "turn", body: "Original flag",
    tags: ["free"], author: "researcher", resolved: true, resolution: {body: "Follow-up <script>", author: "reviewer"}},
    {id: 2, target_type: "post", target_id: "post", body: "Open flag", tags: [], author: "researcher", resolved: false}],
    notes: [{body: "Run note", author: "operator", tags: []}]});
  assert.match(html, /Original flag/);
  assert.match(html, /Resolution history/);
  assert.match(html, /reviewer/);
  assert.match(html, /Follow-up &lt;script&gt;/);
  assert.match(html, /Run note/);
  assert.match(html, /data-resolve-flag="2"/);
  assert.doesNotMatch(html, /data-resolve-flag="1"/);
});

test("Activity filtering includes flagged posts, turns, and their flag history", () => {
  const b = browser();
  const flags = [{id: 7, target_type: "post", target_id: "post", turn_id: "related-turn"}, {id: 8, target_type: "turn", target_id: "turn", resolved: true}];
  assert.equal(b.tools.eventIsFlagged({id: 2, post_id: "post", payload: {}}, flags), true);
  assert.equal(b.tools.eventIsFlagged({id: 3, payload: {turn_id: "turn"}}, flags), true);
  assert.equal(b.tools.eventIsFlagged({id: 7, payload: {}}, flags), true);
  assert.equal(b.tools.eventIsFlagged({id: 9, payload: {flag_event_id: 8}}, flags), true);
  assert.equal(b.tools.isFlagged(flags, "turn", "related-turn"), true);
  assert.equal(b.tools.eventIsFlagged({id: 10, payload: {turn_id: "other"}}, flags), false);
});

test("findings panel offers all exports and switches prompt inclusion without changing raw event URL", async () => {
  const elements = new Map();
  const element = name => {
    if (!elements.has(name)) elements.set(name, {listeners: {}, innerHTML: "", addEventListener(name, callback) { this.listeners[name] = callback; }});
    return elements.get(name);
  };
  const root = {innerHTML: "", querySelector: element};
  const b = browser(async url => {
    assert.equal(url, "/api/runs/run%2Fone/findings");
    return {flags: [], notes: [], suggested_tags: ["configured"]};
  });
  let loaded;
  await b.tools.renderPanel(root, {run: {id: "run/one", session_type: "collaboration"}, onLoaded: data => { loaded = data; }});
  assert.match(root.innerHTML, /Add flag/);
  assert.match(root.innerHTML, /Add note/);
  assert.match(root.innerHTML, /Turns JSONL/);
  assert.match(root.innerHTML, /Events JSONL/);
  assert.match(root.innerHTML, /ZIP bundle/);
  assert.equal(loaded.suggested_tags[0], "configured");
  element("[data-include-prompts]").listeners.change({target: {checked: false}});
  assert.equal(element("[data-export-jsonl]").href, "/api/runs/run%2Fone/export.jsonl?include_prompts=false");
  assert.equal(element("[data-export-zip]").href, "/api/runs/run%2Fone/export.zip?include_prompts=false");
  assert.equal(b.tools.exportUrls("run/one", false).events, "/api/runs/run%2Fone/events.jsonl");
});

test("export identifiers stay in an encoded same-origin path", () => {
  const b = browser();
  const runId = '//observer.invalid/collect?token=private#fragment';
  for (const href of Object.values(b.tools.exportUrls(runId))) {
    const url = new URL(href, "https://board.test");
    assert.equal(url.origin, "https://board.test");
    assert.equal(url.hash, "");
    assert.equal(url.searchParams.has("token"), false);
    assert.equal(decodeURIComponent(url.pathname.split("/")[3]), runId);
  }
});

test("Sessions flagged-only filter works in collaboration and keeps research resampling hidden", async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {innerHTML: "", listeners: {}, addEventListener(name, callback) { this.listeners[name] = callback; }});
    return elements.get(id);
  };
  const b = browser();
  const report = {run_id: "run", session_type: "collaboration", title: "Conversation", state: "paused", metrics: {}, participants: [],
    actions: [{turn_id: "flagged-turn", handle: "peer", kind: "reply", state: "completed"},
      {turn_id: "ordinary-turn", handle: "peer", kind: "pass", state: "passed"}]};
  const context = {window: {addEventListener() {}, SwarmFindings: {...b.tools,
    renderPanel(root, options) { options.onLoaded({flags: [{target_type: "turn", target_id: "flagged-turn"}]}); }}},
    document: {getElementById: element}, location: {pathname: "/sessions", search: "?run=run"}, history: {replaceState() {}}, URLSearchParams,
    setInterval() {}, FormData: class {getAll() { return []; }},
    fetch: async url => ({ok: true, json: async () => url === "/api/state" ? {agents: [], runs: [{id: "run", state: "paused", config: {collaboration: true}}]} : report})};
  vm.runInNewContext(read("sessions.js"), context);
  await settle();
  assert.match(element("actions").innerHTML, /ordinary-turn/);
  element("sessions-flagged-only").listeners.change({target: {checked: true}});
  assert.match(element("actions").innerHTML, /flagged-turn/);
  assert.match(element("actions").innerHTML, /Flag turn/);
  assert.doesNotMatch(element("actions").innerHTML, /ordinary-turn|Resample/);
});
