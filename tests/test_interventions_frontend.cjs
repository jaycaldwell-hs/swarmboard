const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../swarmboard/static/interventions.js"), "utf8");
const form = data => ({get: name => data[name] ?? null});

function browser(api = async () => ({})) {
  const modals = [];
  const context = {crypto: {randomUUID: () => `key-${Math.random()}`}, window: {SwarmResearch: {api,
    commandDialog(title, html, label, submit) {
      const fields = new Map();
      const field = name => {
        if (!fields.has(name)) fields.set(name, {value: name === '[name="provider"]' ? "openai_compatible" : "", listeners: {},
          addEventListener(name, callback) { this.listeners[name] = callback; }});
        return fields.get(name);
      };
      const modal = {title, html, label, submit, querySelector: field};
      modals.push(modal);
      return modal;
    }}}};
  vm.runInNewContext(source, context);
  return {tools: context.window.SwarmInterventions, modals};
}

test("configuration uses effective session overrides and pins OpenRouter/Codex destinations", () => {
  const b = browser();
  const original = {id: "peer", handle: "peer", provider: "openai_compatible", model: "global", persona: "Global",
    settings: {base_url: "old-host", api_key_env: "OLD_KEY", headers: {"X-Title": "Board"}, sampling: {temperature: 0.7}}};
  const run = {config: {agent_overrides: {peer: {model: "session", persona: "Session persona", settings: {...original.settings, sampling: {temperature: 0.3}}}}}};
  const agent = b.tools.effectiveAgent(original, run, {config_changes: []});
  assert.equal(agent.model, "session");
  const payload = b.tools.configurationPayload(form({provider: "openai_compatible", model: "new-open-model", persona: "Session persona", sampling: '{"temperature":0.2}'}), "edit", agent);
  assert.equal(payload.settings.base_url, "https://openrouter.ai/api/v1");
  assert.equal(payload.settings.api_key_env, "OPENROUTER_API_KEY");
  assert.equal(payload.settings.sampling.temperature, 0.2);
  assert.equal(payload.idempotency_key, "edit");
  assert.equal("persona" in payload, false);
  const codex = b.tools.configurationPayload(form({provider: "codex", model: "other", persona: "Changed", sampling: "{}"}), "astra", agent);
  assert.equal(codex.model, "gpt-6-astra");
  assert.equal(codex.persona, "Changed");
  assert.equal("base_url" in codex.settings, false);
  assert.equal("api_key_env" in codex.settings, false);
  assert.equal("headers" in codex.settings, false);
  assert.equal(original.model, "global");
  assert.equal(original.settings.base_url, "old-host");
});

test("Ada configuration labels session memory and PATCH retains captured instructions", async () => {
  const requests = [];
  const b = browser(async (...args) => { requests.push(args); });
  const agent = {id: "ada", handle: "ada", provider: "codex", model: "gpt-6-astra", persona: "Files",
    settings: {persona_harness: {instructions: "Original instructions", memory: "Captured memory", source: "private", version: 1}}};
  b.tools.openConfiguration({run: {id: "run", state: "paused", config: {}}, agent, onSaved: async () => {}});
  assert.match(b.modals[0].html, /Ada memory for this session/);
  assert.match(b.modals[0].html, /persona files are unchanged/);
  assert.doesNotMatch(b.modals[0].html, /name="base_url"|name="api_key_env"/);
  await b.modals[0].submit(form({provider: "codex", model: "gpt-6-astra", persona: "New memory", sampling: "{}"}), "change");
  assert.equal(requests[0][0], "/api/runs/run/agents/ada/configuration");
  assert.equal(requests[0][2], "PATCH");
  assert.equal(requests[0][1].persona, "New memory");
  assert.equal(requests[0][1].settings.persona_harness.instructions, "Original instructions");
});

test("private instructions and memory versions target one session participant with retained retry keys", async () => {
  const requests = [];
  const b = browser(async (url, body) => { requests.push({url, body}); });
  const run = {id: "run/one", state: "running", config: {}};
  const agent = {id: "peer", handle: "peer"};
  b.tools.openInstruction({run, agent, onSaved: async () => {}});
  await b.modals[0].submit(form({body: "Only for this participant"}), "instruction-key");
  assert.equal(requests[0].url, "/api/runs/run%2Fone/agents/peer/instructions");
  assert.equal(requests[0].body.idempotency_key, "instruction-key");
  assert.equal(requests[0].body.body, "Only for this participant");
  b.tools.openMemory({run, agent, memory: {id: "prior", body: "Old", tags: ["one"]}, onSaved: async () => {}});
  await b.modals[1].submit(form({body: "Next version", tags: "new, custom, new"}), "memory-key");
  assert.equal(requests[1].url, "/api/runs/run%2Fone/agents/peer/memories");
  assert.equal(requests[1].body.replaces_memory_id, "prior");
  assert.equal(requests[1].body.active, true);
  assert.deepEqual(Array.from(requests[1].body.tags), ["new", "custom"]);
  assert.equal(requests[1].body.idempotency_key, "memory-key");
});

test("research posting preserves ledger author separation and is unavailable to collaboration or terminal sessions", async () => {
  const requests = [];
  const b = browser(async (url, body) => { requests.push({url, body}); });
  const config = {thread: {id: "thread", posts: []}, agents: [{handle: "peer"}], onSaved: async () => {}};
  b.tools.openResearchPost({...config, run: {id: "run", state: "running", session_type: "collaboration"}});
  b.tools.openResearchPost({...config, run: {id: "run", state: "completed", session_type: "research"}});
  assert.equal(b.modals.length, 0);
  b.tools.openResearchPost({...config, run: {id: "run", state: "paused", session_type: "research"}});
  await b.modals[0].submit(form({body: "Displayed reply", as_handle: "wintermute", parent_post_id: "parent"}), "post-key");
  assert.equal(requests[0].url, "/api/threads/thread/research-posts");
  assert.equal(requests[0].body.as_handle, "wintermute");
  assert.equal(requests[0].body.parent_post_id, "parent");
  assert.equal("author_human" in requests[0].body, false);
  const system = b.tools.researchPostPayload(form({body: "Notice", as_handle: "ignored", system_author: "on"}), "system");
  assert.equal(system.system_author, true);
  assert.equal("as_handle" in system, false);
  const badge = b.tools.postBadge({author_handle: "operator", metadata: {is_impersonation: true, author_human: "operator", displayed_as_agent: "<peer>"}});
  assert.match(badge, /Posted by operator as @&lt;peer&gt;/);
  assert.equal(b.tools.postBadge({author_handle: "operator", metadata: {}}), "");
});

test("terminal intervention history remains readable with every mutation disabled", async () => {
  const elements = new Map();
  const element = name => {
    if (!elements.has(name)) elements.set(name, {listeners: {}, value: "peer", innerHTML: "", addEventListener(name, callback) { this.listeners[name] = callback; }});
    return elements.get(name);
  };
  const root = {innerHTML: "", querySelector: element};
  const ledger = {instructions: [{id: 1, agent_id: "peer", body: "Instruction", author: "human", revoked: false}],
    memories: [{id: "memory", agent_id: "peer", body: "Remember", version: 2, active: true, tags: [], body_sha256: "hash"}],
    config_changes: [], impersonations: []};
  const b = browser(async () => ledger);
  await b.tools.renderPanel(root, {run: {id: "run", state: "completed", session_type: "collaboration", config: {}},
    thread: {id: "thread"}, agents: [{id: "peer", handle: "peer"}]});
  assert.match(root.innerHTML, /data-add-instruction disabled/);
  assert.match(root.innerHTML, /data-configure disabled/);
  assert.match(root.innerHTML, /data-add-memory disabled/);
  assert.doesNotMatch(root.innerHTML, /data-research-post/);
  assert.match(element("[data-intervention-ledger]").innerHTML, /Instruction/);
  assert.match(element("[data-intervention-ledger]").innerHTML, /data-revoke-instruction="1" disabled/);
  assert.match(element("[data-intervention-ledger]").innerHTML, /data-deactivate-memory="memory" disabled/);
  element("[data-add-instruction]").listeners.click();
  assert.equal(b.modals.length, 0);
});

test("revoke and deactivate retain the same key after lost responses", async () => {
  const elements = new Map();
  const element = name => {
    if (!elements.has(name)) elements.set(name, {listeners: {}, value: "peer", addEventListener(name, callback) { this.listeners[name] = callback; }});
    return elements.get(name);
  };
  const root = {innerHTML: "", querySelector: element};
  const requests = [];
  const ledger = {instructions: [], memories: [], config_changes: [], impersonations: []};
  const b = browser(async (url, body) => {
    if (!body) return ledger;
    requests.push({url, body});
    if (requests.length % 2 === 1) throw new Error("lost response");
    return {};
  });
  await b.tools.renderPanel(root, {run: {id: "run", state: "paused", config: {}}, agents: [{id: "peer", handle: "peer"}]});
  for (const dataset of [{revokeInstruction: "12"}, {deactivateMemory: "memory"}]) {
    const button = {dataset, disabled: false};
    const event = {target: {closest: selector => selector === "[data-replace-memory]" ? null : button}};
    await element("[data-intervention-ledger]").listeners.click(event);
    await element("[data-intervention-ledger]").listeners.click(event);
  }
  assert.equal(requests[0].url, "/api/instructions/12/revoke");
  assert.equal(requests[0].body.idempotency_key, requests[1].body.idempotency_key);
  assert.equal(requests[2].url, "/api/memories/memory/deactivate");
  assert.equal(requests[2].body.idempotency_key, requests[3].body.idempotency_key);
});
