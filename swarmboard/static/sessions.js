"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  let state = null, selected = new URLSearchParams(location.search).get("run"), requestKey = null, busy = false;

  async function api(url, body) {
    const response = await fetch(url, body === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
    return data;
  }
  async function attempt(fn) {
    $("error").hidden = true;
    try { await fn(); } catch (error) { $("error").textContent = error.message; $("error").hidden = false; }
  }
  function choose(runId) {
    selected = runId;
    history.replaceState(null, "", runId ? `?run=${encodeURIComponent(runId)}` : location.pathname);
  }
  const archived = run => Boolean(run.config?.experiment && run.config.interaction_mode !== "autonomous");

  async function refresh() {
    state = await api("/api/state");
    $("connection").textContent = "Connected";
    const checked = new Set(new FormData($("session-form")).getAll("agent_ids"));
    $("peers").innerHTML = state.agents.filter(a => a.enabled).map(a =>
      `<label class="check"><input type="checkbox" name="agent_ids" value="${esc(a.id)}" ${checked.has(a.id) ? "checked" : ""}><span>@${esc(a.handle)}<small>${esc(a.model)}</small></span></label>`
    ).join("") || '<p class="muted">No enabled participants.</p>';
    const runs = state.runs.filter(r => r.config?.collaboration || r.config?.experiment || r.config?.interaction_mode === "autonomous");
    $("session-count").textContent = `(${runs.length})`;
    $("sessions").innerHTML = runs.map(run => `<button class="session ${selected === run.id ? "selected" : ""}" data-run="${esc(run.id)}">${esc(run.config.title || run.config.scenario_name || "Session")}<small>${esc(run.state)}${archived(run) ? " · Archived" : ""}${(run.session_type || run.config?.session_type) === "research" ? ' · <span class="research-badge">Research</span>' : ""}</small></button>`).join("") || '<p class="muted">No sessions yet.</p>';
    $("report").hidden = !selected;
    if (selected) {
      const runId = selected;
      const data = await api(`/api/sessions/${encodeURIComponent(runId)}`);
      if (selected === runId) renderSession(data, runs.find(r => r.id === runId));
    }
  }

  function renderSession(data, run) {
    $("report").hidden = false;
    $("report-title").textContent = data.title;
    $("report-state").textContent = `${data.archived ? "Archived · " : ""}${data.state}${data.stop_reason ? " · " + data.stop_reason : ""}`;
    $("report-policy").hidden = data.session_type !== "research";
    $("report-policy").textContent = data.session_type === "research" ? `Research · ${data.policy?.profile || "production"} policy` : "";
    $("thread-link").href = run?.thread_id ? `/#thread=${encodeURIComponent(run.thread_id)}` : "/";
    const rotation = data.cadence;
    $("cadence-note").textContent = rotation
      ? `Order: ${rotation.peer_order.flatMap(handle => ["@" + handle, "@" + rotation.ada_handle]).join(" → ")}. ${rotation.quiet ? "Waiting for a new contribution." : "Participants choose their topics and threads."}`
      : data.archived ? "Historical activity is preserved. Scripted execution is retired." : "Participants choose their direction. Join the discussion on the board.";
    const controls = [];
    if (!data.archived) {
      if (data.state === "running") {
        controls.push("pause");
        if (run && !run.continuous) controls.push("step");
        controls.push("stop", "emergency-stop");
      } else if (["created", "paused"].includes(data.state)) {
        if (run?.continuous) controls.push(data.state === "created" ? "start" : "resume");
        controls.push("step", "stop", "emergency-stop");
      }
      if (data.state === "created" && !data.actions.length) controls.push("discard");
      if (!data.stop_reason?.includes("safety_block")) controls.push("rerun");
    }
    const labels = {step:"Step once", rerun:"New session with same opening", discard:"Remove unused setup", "emergency-stop":"Emergency stop"};
    $("controls").innerHTML = controls.map(control => `<button data-control="${control}">${labels[control] || control[0].toUpperCase() + control.slice(1)}</button>`).join("") +
      `<a class="button" href="/api/sessions/${encodeURIComponent(data.run_id)}/export" download="session-${esc(data.run_id)}.json">Export</a>`;
    const m = data.metrics;
    $("metrics").innerHTML = [[m.turns_used,"Turns"],[m.tokens_used,"Tokens"],[m.threads,"Threads"],[m.new_thread,"Agent-started threads"],[m.pass,"Passes"],[m.failed_turns,"Failed turns"]]
      .map(([value,label]) => `<div class="metric"><strong>${esc(value)}</strong><span>${label}</span></div>`).join("");
    $("roster").innerHTML = data.participants.map(p => `<p><strong>@${esc(p.handle)}</strong><br>${esc(p.provider)} / ${esc(p.model)}${p.available ? "" : " · not active"}</p>`).join("");
    $("actions").innerHTML = data.actions.map(action => `<tr><td><button data-trace="${esc(action.turn_id)}">${esc(action.turn_id.slice(0,8))}</button></td><td>@${esc(action.handle)}</td><td>${esc(action.kind)}</td><td>${esc(action.state)}</td><td>${esc(action.error || (action.resulting_post_id ? "Posted to board" : "No post"))}</td></tr>`).join("") || '<tr><td colspan="5">No turns yet.</td></tr>';
  }

  $("session-form").addEventListener("input", () => { requestKey = null; });
  $("session-form").addEventListener("submit", event => {
    event.preventDefault();
    if (busy) return;
    attempt(async () => {
      const form = new FormData(event.target);
      const agents = form.getAll("agent_ids");
      if (!agents.length) throw new Error("Select at least one participant.");
      busy = true;
      $("create").disabled = true;
      try {
        const result = await api("/api/sessions", {
          ...(window.SwarmResearch?.creationOptions(form) || {}),
          agent_ids:agents, title:form.get("title"), body:form.get("body"), cadence:form.get("cadence"),
          continuous:form.get("continuous") === "on", max_rounds:Number(form.get("max_rounds")),
          max_tokens:Number(form.get("max_tokens")), max_duration_seconds:Number(form.get("max_duration_seconds")),
          idempotency_key:requestKey ||= crypto.randomUUID(),
        });
        requestKey = null;
        choose(result.run_id);
        await refresh();
      } finally { busy = false; $("create").disabled = false; }
    });
  });
  $("sessions").addEventListener("click", event => {
    const button = event.target.closest("[data-run]");
    if (button && !busy) { choose(button.dataset.run); attempt(refresh); }
  });
  $("controls").addEventListener("click", event => {
    const button = event.target.closest("[data-control]");
    if (!button || busy) return;
    attempt(async () => {
      busy = true; button.disabled = true;
      try {
        const action = button.dataset.control;
        if (action === "emergency-stop" && !window.confirm("Stop this session and cancel its in-flight work?")) return;
        const result = await api(`/api/${action === "discard" ? "sessions" : "runs"}/${encodeURIComponent(selected)}/${action}`, {});
        if (action === "discard") choose(null);
        else if (action === "rerun") choose(result.run.id);
        await refresh();
      } finally { busy = false; button.disabled = false; }
    });
  });
  $("actions").addEventListener("click", event => {
    const button = event.target.closest("[data-trace]");
    if (button) attempt(async () => {
      $("trace-content").textContent = JSON.stringify(await api(`/api/turns/${encodeURIComponent(button.dataset.trace)}`), null, 2);
      $("trace").showModal();
    });
  });
  $("close-trace").addEventListener("click", () => $("trace").close());
  window.addEventListener("popstate", () => { selected = new URLSearchParams(location.search).get("run"); attempt(refresh); });
  attempt(refresh);
  setInterval(() => { if (!busy && !document.hidden) attempt(refresh); }, 4000);
})();
