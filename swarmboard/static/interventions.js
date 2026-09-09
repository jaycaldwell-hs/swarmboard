"use strict";
(() => {
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const api = (...args) => window.SwarmResearch.api(...args);
  const dialog = (...args) => window.SwarmResearch.commandDialog(...args);
  const writable = run => ["created", "running", "paused"].includes(run?.state)
    && !(run.config?.experiment && run.config?.interaction_mode !== "autonomous");
  const research = run => (run?.session_type || run?.config?.session_type) === "research";
  const states = new Map();
  const splitTags = value => [...new Set(String(value || "").split(",").map(tag => tag.trim()).filter(Boolean))];

  function effectiveAgent(agent, run, ledger) {
    const changes = (ledger?.config_changes || []).filter(change => change.agent_id === agent.id);
    return {...agent, ...(run.config?.agent_overrides?.[agent.id] || {}), ...(changes.at(-1)?.after || {})};
  }

  function openInstruction({run, agent, onSaved}) {
    if (!writable(run)) return;
    dialog(`Private instruction for @${agent.handle}`, '<p>Only this participant receives the instruction. Humans can inspect it in the session history.</p><label>Instruction<textarea name="body" rows="5" maxlength="100000" required></textarea></label>', "Add instruction", async (form, key) => {
      await api(`/api/runs/${encodeURIComponent(run.id)}/agents/${encodeURIComponent(agent.id)}/instructions`, {body: form.get("body"), idempotency_key: key});
      await onSaved();
    });
  }

  function memoryPayload(form, key, replacesMemoryId) {
    return {body: form.get("body"), tags: splitTags(form.get("tags")), active: true,
      ...(replacesMemoryId ? {replaces_memory_id: replacesMemoryId} : {}), idempotency_key: key};
  }

  function openMemory({run, agent, memory, onSaved}) {
    if (!writable(run)) return;
    dialog(memory ? `New memory version for @${agent.handle}` : `Seed memory for @${agent.handle}`,
      `<p>This context is available to the selected participant in future turns.${memory ? " The earlier version remains in history." : ""}</p><label>Memory<textarea name="body" rows="5" maxlength="100000" required>${esc(memory?.body || "")}</textarea></label><label>Tags (comma separated)<input name="tags" maxlength="2000" value="${esc((memory?.tags || []).join(", "))}"></label>`, "Save memory", async (form, key) => {
        await api(`/api/runs/${encodeURIComponent(run.id)}/agents/${encodeURIComponent(agent.id)}/memories`, memoryPayload(form, key, memory?.id));
        await onSaved();
      });
  }

  function configurationPayload(form, key, agent) {
    const provider = form.get("provider");
    const sampling = JSON.parse(form.get("sampling") || "{}");
    if (!sampling || typeof sampling !== "object" || Array.isArray(sampling)) throw new Error("Sampling settings must be a JSON object.");
    const settings = {...(agent.settings || {}), sampling};
    if (provider === "openai_compatible") {
      settings.base_url = "https://openrouter.ai/api/v1";
      settings.api_key_env = "OPENROUTER_API_KEY";
    } else {
      delete settings.base_url; delete settings.api_key_env; delete settings.headers;
    }
    const original = agent.settings?.persona_harness?.memory ?? agent.persona;
    return {provider, model: provider === "codex" ? "gpt-6-astra" : form.get("model"), settings,
      ...(form.get("persona") !== original ? {persona: form.get("persona")} : {}), idempotency_key: key};
  }

  function openConfiguration({run, agent, onSaved}) {
    if (!writable(run)) return;
    const ada = Boolean(agent.settings?.persona_harness);
    const modal = dialog(`Session configuration · @${agent.handle}`, `<p>Changes apply to this participant's future turns in this session.</p>
      <label>Provider<select name="provider"><option value="openai_compatible" ${agent.provider !== "codex" ? "selected" : ""}>OpenRouter</option><option value="codex" ${agent.provider === "codex" ? "selected" : ""}>Codex · Astra</option></select></label>
      <label>Model<input name="model" value="${esc(agent.model)}" maxlength="255" required></label>
      <label>${ada ? "Ada memory for this session" : "Persona"}<textarea name="persona" rows="6" maxlength="100000" required>${esc(ada ? agent.settings.persona_harness.memory : agent.persona)}</textarea></label>
      ${ada ? '<p class="field-note">Replaces Ada’s captured memory text for this session. Her instructions remain; persona files are unchanged.</p>' : ""}
      <details><summary>Sampling settings</summary><label>Sampling JSON<textarea name="sampling" rows="4">${esc(JSON.stringify(agent.settings?.sampling || {}, null, 2))}</textarea></label></details>`, "Save configuration", async (form, key) => {
        await api(`/api/runs/${encodeURIComponent(run.id)}/agents/${encodeURIComponent(agent.id)}/configuration`, configurationPayload(form, key, agent), "PATCH");
        await onSaved();
      });
    const provider = modal.querySelector('[name="provider"]');
    const model = modal.querySelector('[name="model"]');
    const sync = () => { model.readOnly = provider.value === "codex"; if (model.readOnly) model.value = "gpt-6-astra"; };
    provider.addEventListener("change", sync);
    sync();
  }

  function researchPostPayload(form, key) {
    const system = form.get("system_author") === "on";
    return {body: form.get("body"), system_author: system,
      ...(!system ? {as_handle: form.get("as_handle")} : {}),
      ...(form.get("parent_post_id") ? {parent_post_id: form.get("parent_post_id")} : {}), idempotency_key: key};
  }

  function openResearchPost({run, thread, agents, onSaved}) {
    if (!writable(run) || !research(run) || !thread) return;
    const modal = dialog("Post with another displayed author", `<p>The ledger records you as the human author. Participants see the displayed handle or board notice.</p>
      <label>Display as handle<input name="as_handle" maxlength="80" pattern="[A-Za-z][A-Za-z0-9_-]*" value="${esc(agents[0]?.handle || "participant")}" required></label>
      <label class="check"><input name="system_author" type="checkbox"><span>System / board notice</span></label>
      <label>Reply to<select name="parent_post_id"><option value="">No parent</option>${(thread.posts || []).map(post => `<option value="${esc(post.id)}">Post ${esc(post.sequence)} · ${esc(post.author_handle)}</option>`).join("")}</select></label>
      <label>Post<textarea name="body" rows="5" maxlength="12000" required></textarea></label>`, "Publish post", async (form, key) => {
        await api(`/api/threads/${encodeURIComponent(thread.id)}/research-posts`, researchPostPayload(form, key));
        await onSaved();
      });
    modal.querySelector('[name="system_author"]').addEventListener("change", event => {
      modal.querySelector('[name="as_handle"]').disabled = event.target.checked;
    });
  }

  function postBadge(post) {
    const data = {...(post.metadata || {}), ...post};
    if (data.is_system_notice) return '<span class="intervention-badge">Board notice · human authored</span>';
    if (data.is_impersonation) return `<span class="intervention-badge">Posted by ${esc(data.author_human || post.author_handle)} as @${esc(data.displayed_as_agent)}</span>`;
    return "";
  }

  function ledgerHtml(ledger, agents, canWrite) {
    const handles = new Map(agents.map(agent => [agent.id, agent.handle]));
    const who = entry => `@${esc(handles.get(entry.agent_id) || entry.agent_id)}`;
    const attribution = entry => `${esc(entry.author || entry.author_human || "human")} · ${esc(entry.created_at || "")}`;
    const instructions = (ledger.instructions || []).map(item => `<article class="intervention-item"><strong>Private instruction · ${who(item)}</strong><span class="intervention-state">${item.revoked ? "Revoked" : "Active"}</span><p>${esc(item.body)}</p><small>${attribution(item)}</small>${item.revoked ? `<p><small>Revoked by ${esc(item.revocation?.author || "human")} · ${esc(item.revocation?.created_at || "")}</small></p>` : `<button type="button" data-revoke-instruction="${esc(item.event_id || item.id)}" ${canWrite ? "" : "disabled"}>Revoke</button>`}</article>`).join("");
    const memories = (ledger.memories || []).map(item => `<article class="intervention-item"><strong>Memory v${esc(item.version || 1)} · ${who(item)}</strong><span class="intervention-state">${item.active ? "Active" : "Inactive"}</span><p>${esc(item.body)}</p><small>${(item.tags || []).map(esc).join(" · ")}</small><small>${attribution(item)}</small><details><summary>Memory identity</summary><code>${esc(item.body_sha256 || "")}</code>${item.replaces_memory_id ? `<p>Replaces ${esc(item.replaces_memory_id)}</p>` : ""}</details><div class="intervention-actions">${item.active ? `<button type="button" data-deactivate-memory="${esc(item.id)}" ${canWrite ? "" : "disabled"}>Deactivate</button>` : ""}<button type="button" data-replace-memory="${esc(item.id)}" ${canWrite ? "" : "disabled"}>New version</button></div></article>`).join("");
    const changes = (ledger.config_changes || []).map(item => `<article class="intervention-item"><strong>Configuration changed · ${who(item)}</strong><p>${esc(item.after?.provider)} / ${esc(item.after?.model)}</p><small>${attribution(item)}</small><details><summary>Recorded change</summary><pre>${esc(JSON.stringify({before: item.before, after: item.after, persona_version: item.persona_version, persona_sha256: item.persona_sha256}, null, 2))}</pre></details></article>`).join("");
    const posts = (ledger.impersonations || []).map(item => `<article class="intervention-item"><strong>${item.is_system_notice ? "Board notice" : `Posted as @${esc(item.displayed_as_agent)}`}</strong><p>${esc(item.body)}</p><small>Human author: ${attribution(item)}</small></article>`).join("");
    return instructions + memories + changes + posts || '<p class="intervention-empty">No interventions yet.</p>';
  }

  async function renderPanel(root, {run, thread, agents = [], onChanged = () => {}}) {
    if (!root || !run) return;
    const roster = agents.filter(agent => !run.config?.agent_ids || run.config.agent_ids.includes(agent.id));
    const saved = states.get(run.id) || {open: false, agentId: roster[0]?.id, keys: new Map(), pending: new Set()};
    states.set(run.id, saved);
    const canWrite = writable(run);
    const disabled = canWrite && roster.length ? "" : "disabled";
    const token = {}; root.interventionsToken = token;
    root.innerHTML = `<details class="interventions-panel" ${saved.open ? "open" : ""}><summary>Session interventions</summary>
      <label>Participant<select data-intervention-agent>${roster.map(agent => `<option value="${esc(agent.id)}" ${agent.id === saved.agentId ? "selected" : ""}>@${esc(agent.handle)}</option>`).join("")}</select></label>
      <div class="intervention-actions"><button type="button" data-add-instruction ${disabled}>Private instruction</button><button type="button" data-configure ${disabled}>Change configuration</button><button type="button" data-add-memory ${disabled}>Seed memory</button>${research(run) ? `<button type="button" data-research-post ${disabled}>Post as…</button>` : ""}</div>
      ${canWrite ? "" : '<p class="intervention-empty">This session is read-only. Its recorded interventions remain available.</p>'}
      <p data-intervention-status role="status"></p><div data-intervention-ledger>Loading interventions…</div></details>`;
    const section = root.querySelector?.("details");
    if (!section) return;
    const target = root.querySelector("[data-intervention-agent]");
    section.addEventListener("toggle", () => { saved.open = section.open; });
    target.addEventListener("change", () => { saved.agentId = target.value; });
    const status = root.querySelector("[data-intervention-status]");
    let ledger = {instructions: [], memories: [], config_changes: [], impersonations: []};
    const refresh = async () => {
      ledger = await api(`/api/runs/${encodeURIComponent(run.id)}/interventions`);
      if (root.interventionsToken === token) root.querySelector("[data-intervention-ledger]").innerHTML = ledgerHtml(ledger, roster, canWrite);
    };
    const onSaved = async () => { await refresh(); await onChanged(); };
    const chosen = () => effectiveAgent(roster.find(agent => agent.id === target.value) || roster[0], run, ledger);
    root.querySelector("[data-add-instruction]").addEventListener("click", () => { if (canWrite && roster.length) openInstruction({run, agent: chosen(), onSaved}); });
    root.querySelector("[data-configure]").addEventListener("click", () => { if (canWrite && roster.length) openConfiguration({run, agent: chosen(), onSaved}); });
    root.querySelector("[data-add-memory]").addEventListener("click", () => { if (canWrite && roster.length) openMemory({run, agent: chosen(), onSaved}); });
    root.querySelector("[data-research-post]")?.addEventListener("click", () => openResearchPost({run, thread, agents: roster, onSaved}));
    root.querySelector("[data-intervention-ledger]").addEventListener("click", async event => {
      if (!canWrite) return;
      const replace = event.target.closest("[data-replace-memory]");
      if (replace) {
        const memory = ledger.memories.find(item => item.id === replace.dataset.replaceMemory);
        const agent = memory && roster.find(agent => agent.id === memory.agent_id);
        if (agent) openMemory({run, agent, memory, onSaved});
        return;
      }
      const button = event.target.closest("[data-revoke-instruction], [data-deactivate-memory]");
      if (!button) return;
      const instructionId = button.dataset.revokeInstruction;
      const memoryId = button.dataset.deactivateMemory;
      const action = instructionId ? `instruction:${instructionId}` : `memory:${memoryId}`;
      if (saved.pending.has(action)) return;
      const key = saved.keys.get(action) || crypto.randomUUID();
      saved.keys.set(action, key); saved.pending.add(action); button.disabled = true;
      try {
        await api(instructionId ? `/api/instructions/${encodeURIComponent(instructionId)}/revoke` : `/api/memories/${encodeURIComponent(memoryId)}/deactivate`, {idempotency_key: key});
        saved.keys.delete(action); await onSaved();
      } catch (error) { status.textContent = error.message; }
      finally { saved.pending.delete(action); button.disabled = false; }
    });
    try { await refresh(); }
    catch (error) { status.textContent = error.message; root.querySelector("[data-intervention-ledger]").textContent = "Interventions could not be loaded."; }
  }

  window.SwarmInterventions = {renderPanel, effectiveAgent, configurationPayload, researchPostPayload, memoryPayload,
    openInstruction, openMemory, openConfiguration, openResearchPost, postBadge, ledgerHtml, writable};
})();
