"use strict";
(() => {
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const states = new Map();
  const cache = new Map();
  const api = (...args) => window.SwarmResearch.api(...args);
  const dialog = (...args) => window.SwarmResearch.commandDialog(...args);
  const tags = value => [...new Set(String(value || "").split(",").map(tag => tag.trim()).filter(Boolean))];
  const payload = (form, key) => ({tags: tags(form.get("tags")), body: form.get("body") || "", idempotency_key: key});
  const isFlagged = (flags, type, id) => id != null && (flags || []).some(flag =>
    (flag.target_type === type && String(flag.target_id) === String(id)) || String(flag[`${type}_id`] || "") === String(id));

  function eventIsFlagged(event, flags) {
    const data = event.payload || {};
    return isFlagged(flags, "post", event.post_id || data.post_id || data.target_post_id)
      || isFlagged(flags, "turn", event.turn_id || data.turn_id || data.target_turn_id)
      || (flags || []).some(flag => String(flag.id) === String(event.id) || String(flag.id) === String(data.flag_event_id || data.flag_id));
  }

  function tagFields(suggested = [], bodyRequired = false) {
    return `<label>Tags (comma separated)<input name="tags" maxlength="2000" autocomplete="off"></label>${suggested.length ? `<p class="suggested-tags">Suggestions: ${suggested.map(tag => `<button type="button" data-suggested-tag="${esc(tag)}">${esc(tag)}</button>`).join(" ")}</p>` : ""}<label>Note<textarea name="body" rows="4" maxlength="20000" ${bodyRequired ? "required" : ""}></textarea></label>`;
  }

  function bindSuggestions(modal) {
    modal.querySelectorAll("[data-suggested-tag]").forEach(button => button.addEventListener("click", () => {
      const input = modal.querySelector('[name="tags"]');
      input.value = [...new Set([...tags(input.value), button.dataset.suggestedTag])].join(", ");
      input.dispatchEvent(new Event("input", {bubbles: true}));
    }));
  }

  function openFlag({runId, targetType, targetId, suggestedTags, onSaved = () => {}}) {
    const modal = dialog(`Flag ${targetType}`, tagFields(suggestedTags || cache.get(runId)?.suggested_tags), "Save flag", async (form, key) => {
      await api(`/api/${targetType}s/${encodeURIComponent(targetId)}/flags`, payload(form, key));
      await onSaved();
    });
    bindSuggestions(modal);
  }

  async function openFlagSelector({runId, thread, posts = [], turns = [], suggestedTags = [], onSaved = () => {}}) {
    if (!posts.length && thread?.id) posts = (await api(`/api/threads/${encodeURIComponent(thread.id)}`)).posts || [];
    const targets = [...posts.map(post => ({type: "post", id: post.id, label: `Post ${post.sequence} · ${post.author_handle}`})),
      ...turns.map(turn => ({type: "turn", id: turn.turn_id || turn.id, label: `Turn ${(turn.turn_id || turn.id).slice(0, 8)} · ${turn.handle || turn.agent_id || "participant"}`}))];
    if (!targets.length) throw new Error("Open a discussion or turn before adding a flag.");
    const modal = dialog("Add flag", `<label>Post or turn<select name="target" required>${targets.map((target, index) => `<option value="${index}">${esc(target.label)}</option>`).join("")}</select></label>${tagFields(suggestedTags)}`, "Save flag", async (form, key) => {
      const target = targets[Number(form.get("target"))];
      await api(`/api/${target.type}s/${encodeURIComponent(target.id)}/flags`, payload(form, key));
      await onSaved();
    });
    bindSuggestions(modal);
  }

  function openNote({runId, suggestedTags = [], onSaved = () => {}}) {
    const modal = dialog("Add session note", tagFields(suggestedTags, true), "Save note", async (form, key) => {
      await api(`/api/runs/${encodeURIComponent(runId)}/notes`, payload(form, key));
      await onSaved();
    });
    bindSuggestions(modal);
  }

  function openResolve({flag, suggestedTags = [], onSaved = () => {}}) {
    const modal = dialog("Resolve flag", `<p>The original flag remains in the history.</p><blockquote>${esc(flag.body || flag.tags.join(", "))}</blockquote>${tagFields(suggestedTags)}`, "Record resolution", async (form, key) => {
      await api(`/api/flags/${encodeURIComponent(flag.id)}/resolve`, payload(form, key));
      await onSaved();
    });
    bindSuggestions(modal);
  }

  function exportUrls(runId, includePrompts = true) {
    const base = `/api/runs/${encodeURIComponent(runId)}`;
    const query = `?include_prompts=${includePrompts ? "true" : "false"}`;
    return {jsonl: `${base}/export.jsonl${query}`, events: `${base}/events.jsonl`, zip: `${base}/export.zip${query}`};
  }

  function findingsHtml(findings) {
    const attribution = entry => `${esc(entry.author || "human")} · ${esc(entry.created_at || "")}`;
    const flagRows = (findings.flags || []).map(flag => `<article class="finding-item ${flag.resolved ? "is-resolved" : ""}">
      <p><strong>${esc(flag.target_type)} ${esc(String(flag.target_id).slice(0, 8))}</strong> <span class="finding-state">${flag.resolved ? "Resolved" : "Open"}</span></p>
      <p class="finding-tags">${(flag.tags || []).map(esc).join(" · ")}</p><p class="finding-body">${esc(flag.body)}</p><small>${attribution(flag)}</small>
      ${flag.resolved ? `<details><summary>Resolution history</summary><p class="finding-body">${esc(flag.resolution?.body || "Resolved")}</p><small>${attribution(flag.resolution || {})}</small></details>` : `<button type="button" data-resolve-flag="${esc(flag.id)}">Resolve</button>`}</article>`).join("");
    const notes = (findings.notes || []).map(note => `<article class="finding-item"><p><strong>Session note</strong></p><p class="finding-tags">${(note.tags || []).map(esc).join(" · ")}</p><p class="finding-body">${esc(note.body)}</p><small>${attribution(note)}</small></article>`).join("");
    return flagRows + notes || '<p class="finding-empty">No flags or notes yet.</p>';
  }

  async function renderPanel(root, {run, thread, posts = [], turns = [], onLoaded = () => {}}) {
    if (!root || !run) return;
    const saved = states.get(run.id) || {open: false, includePrompts: true};
    states.set(run.id, saved);
    const token = {};
    root.findingsToken = token;
    const urls = exportUrls(run.id, saved.includePrompts);
    root.innerHTML = `<details class="findings-panel" ${saved.open ? "open" : ""}><summary>Findings &amp; export</summary><div class="findings-actions"><button type="button" data-add-flag>Add flag</button><button type="button" data-add-note>Add note</button></div><p data-findings-status role="status"></p><div data-findings-list>Loading findings…</div><section class="findings-export"><label class="check"><input type="checkbox" data-include-prompts ${saved.includePrompts ? "checked" : ""}><span>Include captured prompts</span></label><nav aria-label="Download session"><a data-export-jsonl href="${esc(urls.jsonl)}" download>Turns JSONL</a><a href="${esc(urls.events)}" download>Events JSONL</a><a data-export-zip href="${esc(urls.zip)}" download>ZIP bundle</a></nav></section></details>`;
    const section = root.querySelector?.("details");
    if (!section) return;
    section.addEventListener("toggle", () => { saved.open = section.open; });
    root.querySelector("[data-include-prompts]").addEventListener("change", event => {
      saved.includePrompts = event.target.checked;
      const links = exportUrls(run.id, saved.includePrompts);
      root.querySelector("[data-export-jsonl]").href = links.jsonl;
      root.querySelector("[data-export-zip]").href = links.zip;
    });
    const status = root.querySelector("[data-findings-status]");
    let findings = cache.get(run.id) || {flags: [], notes: [], suggested_tags: []};
    const refresh = async () => {
      const data = await api(`/api/runs/${encodeURIComponent(run.id)}/findings`);
      cache.set(run.id, data); findings = data;
      if (root.findingsToken !== token) return;
      root.querySelector("[data-findings-list]").innerHTML = findingsHtml(data);
      onLoaded(data);
    };
    root.querySelector("[data-add-flag]").addEventListener("click", async () => {
      try { await openFlagSelector({runId: run.id, thread, posts, turns, suggestedTags: findings.suggested_tags, onSaved: refresh}); }
      catch (error) { status.textContent = error.message; }
    });
    root.querySelector("[data-add-note]").addEventListener("click", () => openNote({runId: run.id,
      suggestedTags: findings.suggested_tags, onSaved: refresh}));
    root.querySelector("[data-findings-list]").addEventListener("click", event => {
      const button = event.target.closest("[data-resolve-flag]");
      const flag = button && findings.flags.find(item => String(item.id) === button.dataset.resolveFlag);
      if (flag && !flag.resolved) openResolve({flag, suggestedTags: findings.suggested_tags, onSaved: refresh});
    });
    try { await refresh(); }
    catch (error) { status.textContent = error.message; root.querySelector("[data-findings-list]").textContent = "Findings could not be loaded."; }
  }

  window.SwarmFindings = {renderPanel, openFlag, openNote, openResolve, openFlagSelector,
    tags, payload, isFlagged, eventIsFlagged, exportUrls, findingsHtml};
})();
