"use strict";
(() => {
  const toggles = ["dedup", "loop_prevention", "cooldowns", "dormancy"];
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const isResearch = run => (run?.session_type || run?.config?.session_type) === "research";
  const lineage = run => run?.lineage || run?.config?.lineage || {};
  const groupId = run => run?.sibling_group_id || run?.config?.sibling_group_id;
  const panelStates = new Map();

  function creationOptions(formData) {
    if (formData.get("research") !== "on") return {};
    const nullableNumber = name => {
      const value = formData.get("policy_" + name);
      return value == null || value === "" ? null : Number(value);
    };
    return {session_type: "research", policy: {
      profile: formData.get("policy_profile") || "production",
      ...Object.fromEntries(toggles.map(name => [name, formData.get("policy_" + name) === "on"])),
      cooldown_seconds: nullableNumber("cooldown_seconds"),
      consecutive_turn_cap: nullableNumber("consecutive_turn_cap"),
      schema_mode: formData.get("policy_schema_mode") || "strict",
    }};
  }

  function bind(root) {
    const field = name => root.querySelector(`[name="${name}"]`);
    const policy = root.querySelector("[data-research-policy]");
    const updateVisibility = () => {
      policy.hidden = !field("research").checked;
      policy.disabled = policy.hidden;
    };
    field("research").addEventListener("change", updateVisibility);
    field("policy_profile").addEventListener("change", () => {
      const profile = field("policy_profile").value;
      if (profile === "custom") return;
      const production = profile === "production";
      toggles.forEach(name => { field("policy_" + name).checked = production; });
      field("policy_cooldown_seconds").value = "";
      field("policy_consecutive_turn_cap").value = production ? "1" : "";
      field("policy_schema_mode").value = production ? "strict" : "capture";
    });
    policy.addEventListener("input", event => {
      if (event.target !== field("policy_profile")) field("policy_profile").value = "custom";
    });
    updateVisibility();
  }

  async function api(url, body) {
    const response = await fetch(url, body === undefined ? {} : {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
    return data;
  }

  function commandDialog(title, content, submitLabel, submit) {
    const dialog = document.createElement("dialog");
    dialog.className = "research-dialog";
    dialog.innerHTML = `<form><header><h2>${esc(title)}</h2><button type="button" data-dismiss aria-label="Close">×</button></header>${content}<p class="form-error" data-error role="alert" hidden></p><footer><button type="button" data-dismiss>Cancel</button><button type="submit">${esc(submitLabel)}</button></footer></form>`;
    let key = null;
    dialog.querySelectorAll("[data-dismiss]").forEach(button => button.addEventListener("click", () => dialog.close()));
    dialog.querySelector("form").addEventListener("input", () => { key = null; });
    dialog.querySelector("form").addEventListener("submit", async event => {
      event.preventDefault();
      const button = dialog.querySelector('[type="submit"]');
      const error = dialog.querySelector("[data-error]");
      if (button.disabled) return;
      button.disabled = true; error.hidden = true;
      try {
        await submit(new FormData(event.target), key ||= crypto.randomUUID());
        dialog.close();
      } catch (failure) { error.textContent = failure.message; error.hidden = false; }
      finally { button.disabled = false; }
    });
    dialog.addEventListener("close", () => dialog.remove());
    document.body.appendChild(dialog);
    dialog.showModal();
    return dialog;
  }

  function participantViewUrl(threadId, agentId, atPostId = "") {
    const query = new URLSearchParams({agent_id: agentId});
    if (atPostId) query.set("at_post_id", atPostId);
    return `/api/threads/${encodeURIComponent(threadId)}/participant-view?${query}`;
  }

  function forcePayload(form, key) {
    return {agent_id: form.get("agent_id"), thread_id: form.get("thread_id"),
      ...(form.get("at_post_id") ? {stimulus_post_id: form.get("at_post_id")} : {}),
      override_cooldown: form.get("override_cooldown") === "on", idempotency_key: key};
  }

  function renderPanel(root, {run, thread, threads = [], agents = [], onChange = () => {}}) {
    if (!root || !run || !thread) return;
    const allowed = run.config?.agent_ids;
    const participants = agents.filter(agent => !allowed || allowed.includes(agent.id));
    const choices = threads.filter(item => item.run_id === run.id);
    if (!choices.some(item => item.id === thread.id)) choices.unshift(thread);
    const runnable = ["created", "running", "paused"].includes(run.state) && !(run.config?.experiment && run.config?.interaction_mode !== "autonomous");
    const stateKey = `${run.id}:${thread.id}`;
    const saved = panelStates.get(stateKey) || {agent_id: participants[0]?.id, thread_id: thread.id,
      at_post_id: "", override_cooldown: false, open: false, busy: false, key: null, status: ""};
    panelStates.set(stateKey, saved);
    root.innerHTML = `<details class="session-tools" ${saved.open ? "open" : ""}><summary>Participant tools</summary><form>
      <label>Participant<select name="agent_id" required>${participants.map(agent => `<option value="${esc(agent.id)}" ${agent.id === saved.agent_id ? "selected" : ""}>@${esc(agent.handle)}</option>`).join("")}</select></label>
      <label>Thread<select name="thread_id">${choices.map(item => `<option value="${esc(item.id)}" ${item.id === saved.thread_id ? "selected" : ""}>${esc(item.title || item.id)}</option>`).join("")}</select></label>
      <label>Context through<select name="at_post_id"><option value="">Latest post</option>${(saved.thread_id === thread.id ? thread.posts || [] : []).map(post => `<option value="${esc(post.id)}" ${post.id === saved.at_post_id ? "selected" : ""}>Post ${esc(post.sequence)} · ${esc(post.author_handle)}</option>`).join("")}</select></label>
      ${runnable ? `<label class="check"><input type="checkbox" name="override_cooldown" ${saved.override_cooldown ? "checked" : ""}><span>Override cooldown for this turn</span></label>` : ""}
      <div class="session-tool-actions">${runnable ? `<button type="submit" data-command="force" ${saved.busy ? "disabled" : ""}>Force next</button>` : ""}<button type="button" data-command="view">Participant view</button></div>
      <p data-status role="status">${esc(saved.status)}</p></form></details>`;
    const form = root.querySelector("form");
    const details = root.querySelector("details");
    details.addEventListener("toggle", () => { saved.open = details.open; });
    form.addEventListener("input", () => {
      const values = new FormData(form);
      for (const name of ["agent_id", "thread_id", "at_post_id"]) saved[name] = values.get(name);
      saved.override_cooldown = values.get("override_cooldown") === "on";
      saved.key = null;
    });
    form.querySelector('[name="thread_id"]').addEventListener("change", () => {
      form.querySelector('[name="at_post_id"]').innerHTML = '<option value="">Latest post</option>';
      saved.at_post_id = "";
    });
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const button = form.querySelector('[data-command="force"]');
      if (!runnable || !button || button.disabled || saved.busy) return;
      saved.busy = true; button.disabled = true;
      try {
        await api(`/api/runs/${encodeURIComponent(run.id)}/force-turn`, forcePayload(new FormData(form), saved.key ||= crypto.randomUUID()));
        saved.key = null;
        saved.status = "Turn queued. Manual sessions use Step once to process it.";
        saved.busy = false;
        form.querySelector("[data-status]").textContent = saved.status;
        await onChange();
      } catch (error) { saved.status = error.message; form.querySelector("[data-status]").textContent = saved.status; }
      finally { saved.busy = false; button.disabled = false; }
    });
    form.querySelector('[data-command="view"]').addEventListener("click", async event => {
      const button = event.target;
      button.disabled = true;
      try {
        const values = new FormData(form);
        const view = await api(participantViewUrl(values.get("thread_id"), values.get("agent_id"), values.get("at_post_id")));
        commandDialog("Participant view", `<p>Prompt SHA-256: <code>${esc(view.prompt_sha256)}</code></p><pre class="participant-prompt">${esc(JSON.stringify(view.messages, null, 2) || view.prompt)}</pre>`, "Close", async () => {});
      } catch (error) { form.querySelector("[data-status]").textContent = error.message; }
      finally { button.disabled = false; }
    });
  }

  function forkPayload(form, key, atPostId) {
    const policy = form.get("policy_json")?.trim();
    const limits = {};
    for (const name of ["max_rounds", "max_tokens", "max_duration_seconds"]) {
      if (form.get(name) !== "" && form.get(name) != null) limits[name] = Number(form.get(name));
    }
    return {at_post_id: atPostId, agent_ids: form.getAll("agent_ids"),
      ...(policy ? {policy: JSON.parse(policy)} : form.get("policy") !== "inherit" ? {policy: form.get("policy")} : {}),
      ...(Object.keys(limits).length ? {limits} : {}), inherit_remaining: form.get("inherit_remaining") === "on",
      continuous: form.get("continuous") === "on", idempotency_key: key};
  }

  function openFork({thread, post, run, agents, onCreated}) {
    if (!isResearch(run)) return;
    const roster = run.config?.agent_ids || agents.map(agent => agent.id);
    commandDialog(`Fork after post ${post.sequence}`, `<p>The new research session inherits posts through this point and starts with fresh budgets. It waits for Step once by default.</p>
      <fieldset><legend>Participants</legend>${agents.filter(agent => agent.enabled !== false).map(agent => `<label class="check"><input type="checkbox" name="agent_ids" value="${esc(agent.id)}" ${roster.includes(agent.id) ? "checked" : ""}><span>@${esc(agent.handle)}</span></label>`).join("")}</fieldset>
      <label>Policy<select name="policy"><option value="inherit">Copy current policy</option><option value="production">Production</option><option value="permissive">Permissive</option></select></label>
      <details><summary>Custom policy and budgets</summary><label>Custom policy JSON<textarea name="policy_json" rows="3" placeholder='{"dedup":false}'></textarea></label><label>Turn ceiling<input name="max_rounds" type="number" min="1" max="10000" placeholder="Copy source ceiling"></label><label>Token budget<input name="max_tokens" type="number" min="1" placeholder="Copy source ceiling"></label><label>Seconds<input name="max_duration_seconds" type="number" min="1" placeholder="Copy source ceiling"></label></details>
      <label class="check"><input type="checkbox" name="inherit_remaining"><span>Use the source's remaining budgets</span></label><label class="check"><input type="checkbox" name="continuous"><span>Run automatically</span></label>`, "Create fork", async (form, key) => {
        const payload = forkPayload(form, key, post.id);
        if (!payload.agent_ids.length) throw new Error("Choose at least one participant.");
        const result = await api(`/api/threads/${encodeURIComponent(thread.id)}/fork`, payload);
        await onCreated(result);
      });
  }

  function resamplePayload(form, key) {
    return {n: Number(form.get("n")), reuse_prompt: form.get("reuse_prompt") === "on", idempotency_key: key};
  }

  function openResample({turnId, onCreated}) {
    commandDialog("Resample this turn", '<p>Each sample gets its own research fork. The original discussion stays intact.</p><label>Samples<input name="n" type="number" min="1" max="20" value="1" required></label><label class="check"><input name="reuse_prompt" type="checkbox" checked><span>Reuse the original captured prompt</span></label>', "Create samples", async (form, key) => {
      const result = await api(`/api/turns/${encodeURIComponent(turnId)}/resample`, resamplePayload(form, key));
      await onCreated(result);
    });
  }

  function navigationHtml(run, runs) {
    if (!isResearch(run)) return "";
    const parent = lineage(run);
    const siblings = groupId(run) ? runs.filter(item => groupId(item) === groupId(run)) : [];
    return `${parent.parent_thread_id ? `<p class="lineage-breadcrumb">Forked from <a href="/#thread=${encodeURIComponent(parent.parent_thread_id)}">source discussion</a>${parent.parent_post_id ? ` · <a href="/#thread=${encodeURIComponent(parent.parent_thread_id)}&post=${encodeURIComponent(parent.parent_post_id)}">source post</a>` : ""}</p>` : ""}${siblings.length > 1 ? `<nav class="sibling-navigation" aria-label="Resample siblings">${siblings.map((sibling, index) => `<a href="/#thread=${encodeURIComponent(sibling.thread_id || "")}" ${sibling.id === run.id ? 'aria-current="page"' : ""}>Sample ${index + 1}</a>`).join("")}</nav>` : ""}`;
  }

  function threadEntries(threads, runs, selectedId, hideResearch = false) {
    const runById = new Map(runs.map(run => [run.id, run]));
    const source = threads.filter(thread => !hideResearch || !isResearch(runById.get(thread.run_id)));
    const sourceById = new Map(source.map(thread => [thread.id, thread]));
    const groups = new Map();
    source.forEach(thread => {
      const group = groupId(runById.get(thread.run_id));
      if (group) groups.set(group, [...(groups.get(group) || []), thread]);
    });
    const collapsed = source.filter(thread => {
      const group = groups.get(groupId(runById.get(thread.run_id)));
      return !group || (group.find(item => item.id === selectedId) || group[0]).id === thread.id;
    });
    const seen = new Set(), output = [];
    const append = (thread, depth) => {
      if (seen.has(thread.id)) return;
      seen.add(thread.id);
      const run = runById.get(thread.run_id);
      output.push({thread, depth: Math.min(depth, 8), siblingCount: groups.get(groupId(run))?.length || 0});
      collapsed.filter(child => lineage(runById.get(child.run_id)).parent_thread_id === thread.id).forEach(child => append(child, depth + 1));
    };
    collapsed.filter(thread => !sourceById.has(lineage(runById.get(thread.run_id)).parent_thread_id)).forEach(thread => append(thread, 0));
    collapsed.forEach(thread => append(thread, 0));
    return output;
  }

  window.SwarmResearch = {creationOptions, bind, isResearch, renderPanel, openFork, openResample,
    navigationHtml, threadEntries, forcePayload, forkPayload, resamplePayload, participantViewUrl, api, commandDialog};
  document.addEventListener("DOMContentLoaded", () => document.querySelectorAll("[data-research-options]").forEach(bind));
})();
