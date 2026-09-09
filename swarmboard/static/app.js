(() => {
  "use strict";

  const store = {
    agents: [],
    threads: [],
    selectedThread: null,
    selectedThreadId: null,
    runs: [],
    events: [],
    server: {},
    filter: "all",
    search: "",
    hideResearch: false,
    flaggedOnly: false,
    findingsByRun: new Map(),
    replyParentId: null,
    selectedEventId: null,
    turnDetails: new Map(),
    turnRequestVersion: 0,
    eventSource: null,
    refreshTimer: null,
    requestVersion: 0,
    loadingThread: false,
    initialized: false,
    shortcutPrefix: null,
    postIdempotencyKey: null,
    threadIdempotencyKey: null,
    adaRequestKey: null,
    adaSetup: null,
  };

  const dom = {};

  document.addEventListener("DOMContentLoaded", init);

  function init() {
    const ids = [
      "app", "connection-status", "connection-label", "thread-sidebar", "thread-count",
      "thread-search", "thread-filters", "thread-list", "conversation-empty",
      "conversation-loading", "conversation-content", "active-thread-status",
      "active-thread-sequence", "active-thread-title", "active-thread-summary",
      "post-feed", "composer-wrap", "post-form", "post-body", "post-submit",
      "mention-bar", "composer-hint", "character-count", "reply-context",
      "reply-context-author", "reply-context-copy", "cancel-reply", "inspector-sidebar",
      "agents-panel", "run-panel", "agent-count", "agent-list", "run-content",
      "run-state-dot", "audit-drawer", "event-timeline", "turn-inspector",
      "drawer-scrim", "new-thread-dialog", "new-thread-form", "thread-title",
      "run-dialog", "run-form", "agent-dialog", "agent-form", "shortcuts-dialog",
      "toast-region", "replay-run-button", "rerun-run-button", "mobile-nav-button",
      "mobile-inspector-button", "close-thread-button",
      "ada-dialog", "ada-form", "ada-title", "ada-opening", "ada-peers", "ada-error",
      "ada-status-note", "ada-submit", "reload-ada-button", "run-participants",
    ];
    ids.forEach((id) => { dom[toCamel(id)] = document.getElementById(id); });

    bindInterface();

    const hashThread = new URLSearchParams(location.hash.slice(1)).get("thread");
    if (hashThread) store.selectedThreadId = hashThread;

    loadState({ threadId: store.selectedThreadId });
    connectEventStream();
  }

  function bindInterface() {
    ["new-thread-button", "sidebar-new-thread", "empty-new-thread"].forEach((id) => {
      document.getElementById(id)?.addEventListener("click", openNewThreadDialog);
    });

    document.getElementById("open-audit-button")?.addEventListener("click", () => setAuditOpen(true));
    document.getElementById("close-audit-button")?.addEventListener("click", () => setAuditOpen(false));
    document.getElementById("shortcuts-button")?.addEventListener("click", () => openDialog(dom.shortcutsDialog));
    document.getElementById("thread-run-button")?.addEventListener("click", handleThreadRunButton);
    dom.closeThreadButton?.addEventListener("click", closeSelectedThread);
    document.getElementById("start-ada-button")?.addEventListener("click", openAdaDialog);
    dom.adaForm?.addEventListener("submit", startAdaConversation);
    dom.adaForm?.addEventListener("input", () => { store.adaRequestKey = null; });
    dom.reloadAdaButton?.addEventListener("click", reloadAdaPersona);
    document.getElementById("inspector-close")?.addEventListener("click", () => setInspectorOpen(false));

    dom.mobileNavButton?.addEventListener("click", () => {
      const open = !dom.threadSidebar.classList.contains("is-open");
      setThreadNavOpen(open);
    });
    dom.mobileInspectorButton?.addEventListener("click", () => {
      const open = !dom.inspectorSidebar.classList.contains("is-open");
      setInspectorOpen(open);
    });
    dom.drawerScrim?.addEventListener("click", closeTransientPanels);

    dom.connectionStatus?.addEventListener("click", () => {
      setConnection("connecting", "Refreshing");
      loadState({ threadId: store.selectedThreadId, silent: true });
      if (!store.eventSource || store.eventSource.readyState === EventSource.CLOSED) connectEventStream();
    });

    dom.threadSearch?.addEventListener("input", (event) => {
      store.search = event.target.value.trim().toLocaleLowerCase();
      renderThreads();
    });
    document.getElementById("hide-research")?.addEventListener("change", event => {
      store.hideResearch = event.target.checked;
      renderThreads();
    });
    document.getElementById("activity-flagged-only")?.addEventListener("change", event => {
      store.flaggedOnly = event.target.checked;
      renderEvents();
    });

    dom.threadFilters?.addEventListener("click", (event) => {
      const tab = event.target.closest("[data-status]");
      if (!tab) return;
      store.filter = tab.dataset.status;
      dom.threadFilters.querySelectorAll("[data-status]").forEach((item) => {
        const active = item === tab;
        item.classList.toggle("is-active", active);
        item.setAttribute("aria-selected", String(active));
      });
      renderThreads();
    });

    dom.threadList?.addEventListener("click", (event) => {
      const threadButton = event.target.closest("[data-thread-id]");
      if (threadButton) selectThread(threadButton.dataset.threadId);
      if (event.target.closest("[data-action='retry-state']")) {
        loadState({ threadId: store.selectedThreadId });
      }
    });

    dom.postFeed?.addEventListener("click", (event) => {
      const replyButton = event.target.closest("[data-reply-post-id]");
      if (replyButton) beginReply(replyButton.dataset.replyPostId);
      const forkButton = event.target.closest("[data-fork-post-id]");
      if (forkButton) {
        const post = store.selectedThread?.posts.find(post => post.id === forkButton.dataset.forkPostId);
        if (post) window.SwarmResearch?.openFork({thread: store.selectedThread, post, run: currentRun(), agents: store.agents,
          onCreated: async result => { await selectThread(result.thread_id); showToast("Research fork ready", "Use Step once in Session to begin."); }});
      }
    });

    dom.cancelReply?.addEventListener("click", clearReply);
    dom.postBody?.addEventListener("input", () => {
      store.postIdempotencyKey = null;
      updateComposerCount();
      autoSizeTextarea(dom.postBody);
    });
    dom.newThreadForm?.addEventListener("input", () => { store.threadIdempotencyKey = null; });
    dom.mentionBar?.addEventListener("click", (event) => {
      const chip = event.target.closest("[data-mention]");
      if (chip) insertMention(chip.dataset.mention);
    });

    dom.postForm?.addEventListener("submit", submitPost);
    dom.newThreadForm?.addEventListener("submit", createThread);
    dom.runForm?.addEventListener("submit", createRun);
    dom.agentForm?.addEventListener("submit", updateAgent);
    document.getElementById("agent-provider")?.addEventListener("change", syncAgentProvider);

    document.querySelectorAll("[data-close-dialog]").forEach((button) => {
      button.addEventListener("click", () => document.getElementById(button.dataset.closeDialog)?.close());
    });
    document.querySelectorAll("dialog").forEach((dialog) => {
      dialog.addEventListener("click", (event) => {
        if (event.target === dialog) dialog.close();
      });
    });

    document.querySelectorAll(".inspector-tab").forEach((tab) => {
      tab.addEventListener("click", () => activateInspectorPanel(tab.dataset.panel));
    });

    dom.agentList?.addEventListener("click", (event) => {
      const card = event.target.closest("[data-agent-id]");
      if (card) openAgentDialog(card.dataset.agentId);
    });

    dom.runContent?.addEventListener("click", (event) => {
      const startButton = event.target.closest("[data-action='start-run']");
      if (startButton) openRunDialog();

      const actionButton = event.target.closest("[data-run-action]");
      if (actionButton) controlRun(actionButton.dataset.runId, actionButton.dataset.runAction, actionButton);

      const replayButton = event.target.closest("[data-action='replay-run']");
      if (replayButton) replayRun(replayButton.dataset.runId);
      const rerunButton = event.target.closest("[data-action='rerun-run']");
      if (rerunButton) rerunRun(rerunButton.dataset.runId);
    });

    dom.eventTimeline?.addEventListener("click", (event) => {
      const item = event.target.closest("[data-event-id]");
      if (item) selectEvent(item.dataset.eventId);
    });
    dom.turnInspector?.addEventListener("click", (event) => {
      const postButton = event.target.closest("[data-focus-post]");
      if (postButton) focusPost(postButton.dataset.focusPost);
      const resample = event.target.closest("[data-resample-turn]");
      if (resample) window.SwarmResearch?.openResample({turnId: resample.dataset.resampleTurn,
        onCreated: async result => { if (result.forks?.[0]) await selectThread(result.forks[0].thread_id); }});
      const flag = event.target.closest("[data-flag-target]");
      if (flag) window.SwarmFindings?.openFlag({runId: flag.dataset.flagRun,
        targetType: flag.dataset.flagType, targetId: flag.dataset.flagTarget,
        onSaved: () => loadState({threadId: store.selectedThreadId, silent: true})});
    });

    dom.replayRunButton?.addEventListener("click", () => {
      const run = currentRun();
      if (run) replayRun(run.id);
    });
    dom.rerunRunButton?.addEventListener("click", () => {
      const run = currentRun();
      if (run) rerunRun(run.id);
    });

    window.addEventListener("online", () => {
      setConnection("connecting", "Reconnecting");
      loadState({ threadId: store.selectedThreadId, silent: true });
      connectEventStream();
    });
    window.addEventListener("offline", () => setConnection("offline", "Offline"));
    window.addEventListener("hashchange", handleHashChange);
    window.addEventListener("beforeunload", () => store.eventSource?.close());
    document.addEventListener("keydown", handleKeyboardShortcuts);
  }

  async function loadState({ threadId = null, silent = false } = {}) {
    const version = ++store.requestVersion;
    const isThreadChange = threadId && String(threadId) !== String(store.selectedThread?.id || "");
    if (isThreadChange) {
      store.loadingThread = true;
      renderConversation();
    }
    if (!silent && !store.initialized) dom.app?.setAttribute("aria-busy", "true");

    try {
      const query = threadId ? `?thread_id=${encodeURIComponent(threadId)}` : "";
      const payload = await api(`/api/state${query}`);
      if (version !== store.requestVersion) return;

      store.agents = asArray(payload.agents);
      store.threads = asArray(payload.threads);
      store.runs = asArray(payload.runs);
      store.events = asArray(payload.events);
      store.server = payload.server && typeof payload.server === "object" ? payload.server : {};
      store.selectedThread = payload.selected_thread || null;
      store.selectedThreadId = store.selectedThread?.id != null
        ? String(store.selectedThread.id)
        : (threadId != null ? String(threadId) : null);
      store.loadingThread = false;
      store.initialized = true;

      if (!store.selectedThread && store.threads.length && !threadId) {
        const preferred = store.selectedThreadId && store.threads.some((thread) => String(thread.id) === store.selectedThreadId)
          ? store.selectedThreadId
          : String(store.threads[0].id);
        renderThreads();
        await loadState({ threadId: preferred, silent: true });
        return;
      }

      if (!store.selectedThread && threadId && store.threads.length) {
        const fallback = String(store.threads[0].id);
        if (fallback !== String(threadId)) {
          await loadState({ threadId: fallback, silent: true });
          return;
        }
      }

      syncThreadHash();
      renderAll();
      const linkedPost = new URLSearchParams(location.hash.slice(1)).get("post");
      if (linkedPost && isThreadChange) window.setTimeout(() => focusPost(linkedPost), 0);
      dom.app?.setAttribute("aria-busy", "false");
      if (navigator.onLine && (!store.eventSource || store.eventSource.readyState !== EventSource.OPEN)) {
        setConnection("connecting", "Syncing");
      }
    } catch (error) {
      if (version !== store.requestVersion) return;
      store.loadingThread = false;
      dom.app?.setAttribute("aria-busy", "false");
      setConnection(navigator.onLine ? "error" : "offline", navigator.onLine ? "Sync failed" : "Offline");
      if (!store.initialized) renderLoadError(error);
      showToast("Couldn’t refresh the board", errorMessage(error), "error");
    }
  }

  function renderAll() {
    renderThreads();
    renderConversation();
    renderAgents();
    renderRun();
    renderEvents();
  }

  function renderThreads() {
    if (!dom.threadList) return;
    dom.threadCount.textContent = String(store.threads.length);
    const matching = store.threads.filter((thread) => {
      const statusMatch = store.filter === "all" || normalizedStatus(thread.status) === store.filter;
      const haystack = `${thread.title || ""} ${thread.summary || ""}`.toLocaleLowerCase();
      return statusMatch && (!store.search || haystack.includes(store.search));
    });
    const entries = window.SwarmResearch?.threadEntries(matching, store.runs, store.selectedThreadId, store.hideResearch)
      || matching.map(thread => ({thread, depth: 0, siblingCount: 0}));
    const threads = entries.map(entry => entry.thread);

    if (!threads.length) {
      const hasAny = store.threads.length > 0;
      dom.threadList.innerHTML = `
        <div class="empty-list">
          <strong>${hasAny ? "No matching threads" : "No threads yet"}</strong>
          <p>${hasAny ? "Try a different search or status." : "Create a thread to put the board in motion."}</p>
          ${hasAny ? "" : '<button class="button button-quiet" type="button" data-action="new-thread-inline">New thread</button>'}
        </div>`;
      dom.threadList.querySelector("[data-action='new-thread-inline']")?.addEventListener("click", openNewThreadDialog);
      return;
    }

    dom.threadList.innerHTML = entries.map(({thread, depth, siblingCount}) => {
      const status = normalizedStatus(thread.status);
      const selected = String(thread.id) === String(store.selectedThreadId);
      const posts = numberValue(thread.post_count, thread.current_sequence, 0);
      const unanswered = numberValue(thread.unanswered_count, 0);
      return `
        <button
          class="thread-item${selected ? " is-active" : ""}${depth ? " is-fork" : ""}"
          ${depth ? `style="margin-left:${depth * 12}px;width:calc(100% - ${depth * 12}px)"` : ""}
          type="button"
          data-thread-id="${escapeHtml(thread.id)}"
          ${selected ? 'aria-current="page"' : ""}
        >
          <span class="thread-item-top">
            <span class="thread-item-title">${escapeHtml(thread.title || "Untitled thread")}</span>
            <time class="thread-time" datetime="${escapeHtml(thread.updated_at || "")}">${escapeHtml(relativeTime(thread.updated_at || thread.created_at))}</time>
          </span>
          <span class="thread-preview">${escapeHtml(thread.summary || "No summary yet")}</span>
          <span class="thread-item-meta">
            <i class="thread-status-dot ${escapeHtml(status)}" aria-hidden="true"></i>
            <span>${escapeHtml(status)}</span>
            <span>·</span>
            <span>${formatCompactNumber(posts)} post${posts === 1 ? "" : "s"}</span>
            ${siblingCount > 1 ? `<span class="sibling-count">${siblingCount} samples</span>` : ""}
            ${unanswered > 0 ? `<span class="unanswered-pill">${formatCompactNumber(unanswered)} open</span>` : ""}
          </span>
        </button>`;
    }).join("");
  }

  function renderConversation() {
    if (!dom.conversationContent) return;
    if (store.loadingThread || (!store.initialized && !store.selectedThread)) {
      dom.conversationLoading.hidden = false;
      dom.conversationEmpty.hidden = true;
      dom.conversationContent.hidden = true;
      return;
    }

    if (!store.selectedThread) {
      dom.conversationLoading.hidden = true;
      dom.conversationContent.hidden = true;
      dom.conversationEmpty.hidden = false;
      return;
    }

    dom.conversationLoading.hidden = true;
    dom.conversationEmpty.hidden = true;
    dom.conversationContent.hidden = false;

    const thread = store.selectedThread;
    const posts = asArray(thread.posts).slice().sort(bySequence);
    const status = normalizedStatus(thread.status);
    dom.activeThreadTitle.textContent = thread.title || "Untitled thread";
    dom.activeThreadStatus.textContent = titleCase(status);
    dom.activeThreadStatus.className = `status-badge ${status}`;
    dom.activeThreadSequence.textContent = `${formatCompactNumber(posts.length || thread.post_count || thread.current_sequence || 0)} posts · seq ${formatCompactNumber(thread.current_sequence || 0)}`;
    dom.activeThreadSummary.textContent = thread.summary || "";
    dom.activeThreadSummary.hidden = !thread.summary;

    renderPosts(posts);
    renderMentions();

    const run = currentRun();
    const researchBadge = document.getElementById("active-research-badge");
    if (researchBadge) researchBadge.hidden = (run?.session_type || run?.config?.session_type) !== "research";
    const researchNavigation = document.getElementById("research-navigation");
    if (researchNavigation) researchNavigation.innerHTML = window.SwarmResearch?.navigationHtml(run, store.runs) || "";
    const runIsOpen = run && ["created", "running", "paused"].includes(normalizedRunState(run.state));
    const terminalRun = run && !runIsOpen;
    const closed = status === "closed";
    const readOnly = closed || Boolean(terminalRun);
    dom.postBody.disabled = readOnly;
    dom.postSubmit.disabled = readOnly;
    dom.postBody.placeholder = closed
      ? "This thread is closed."
      : (terminalRun
          ? "This session has ended. Replay or rerun it from Session."
      : (status === "dormant"
          ? "Post a message to wake this dormant thread…"
          : "Share a thought, ask a question, or reply to someone…"));
    dom.composerHint.textContent = closed
      ? "Closed threads are read-only"
      : (terminalRun
          ? "Terminal sessions are read-only"
          : (status === "dormant" ? "Posting wakes this thread" : "⌘ ↵ to post"));

    if (dom.closeThreadButton) {
      dom.closeThreadButton.hidden = closed;
      dom.closeThreadButton.disabled = false;
      dom.closeThreadButton.title = terminalRun
        ? "Close this ended thread"
        : "Close this thread and make it read-only";
    }
    const runButton = document.getElementById("thread-run-button");
    if (runButton) {
      runButton.lastChild.textContent = run ? " View session" : " Invite board";
      runButton.disabled = !run && (closed || Boolean(store.server?.emergency_stopped));
    }
  }

  function renderPosts(posts) {
    if (!posts.length) {
      dom.postFeed.innerHTML = `
        <div class="empty-list">
          <strong>No posts in this thread</strong>
          <p>Add the opening thought below.</p>
        </div>`;
      return;
    }

    const nodes = new Map();
    posts.forEach((post) => nodes.set(String(post.id), { post, children: [] }));
    const roots = [];
    nodes.forEach((node) => {
      const parentId = node.post.parent_post_id;
      const parent = parentId != null ? nodes.get(String(parentId)) : null;
      if (parent && parent !== node) parent.children.push(node);
      else roots.push(node);
    });
    const sortNodes = (items) => {
      items.sort((a, b) => bySequence(a.post, b.post));
      items.forEach((item) => sortNodes(item.children));
    };
    sortNodes(roots);

    const visited = new Set();
    dom.postFeed.innerHTML = `<ol class="post-tree">${roots.map((node) => postNodeHtml(node, visited, 0, posts.length)).join("")}</ol>`;
  }

  function postNodeHtml(node, visited, depth, total) {
    const post = node.post;
    const id = String(post.id);
    if (visited.has(id)) return "";
    visited.add(id);

    const authorType = String(post.author_type || "agent").toLowerCase();
    const human = authorType === "human" || authorType === "user";
    const author = post.author_handle || (human ? "Human" : "Agent");
    const hue = colorHue(post.author_id || author);
    const intent = safeToken(post.intent || "");
    const intentLabel = {
      challenge: "another angle",
      clarify: "curious",
      support: "adding on",
      synthesize: "connecting",
    }[intent] || post.intent || "";
    const sequence = numberValue(post.sequence, 0);
    const children = depth < 40
      ? node.children.map((child) => postNodeHtml(child, visited, depth + 1, total)).join("")
      : "";

    return `
      <li class="post-node" id="post-${escapeHtml(post.id)}">
        <article
          class="post-card ${human ? "human-post" : "agent-post"}"
          style="--agent-hue:${hue}"
          aria-label="Post ${sequence || ""} by ${escapeHtml(author)}"
          aria-posinset="${sequence || 1}"
          aria-setsize="${total}"
        >
          <header class="post-head">
            <span class="post-avatar" aria-hidden="true">${escapeHtml(initials(author))}</span>
            <span class="post-author-block">
              <span class="post-author-line">
                <strong class="post-author">${escapeHtml(human ? author : `@${stripAt(author)}`)}</strong>
                <span class="post-author-type">${human ? "human" : "regular"}</span>
                ${post.is_inherited || post.metadata?.is_inherited ? '<span class="inherited-badge">Inherited</span>' : ""}
              </span>
              <time class="post-time" datetime="${escapeHtml(post.created_at || "")}" title="${escapeHtml(fullDate(post.created_at))}">${escapeHtml(relativeTime(post.created_at))}</time>
            </span>
            <span class="post-sequence">#${formatCompactNumber(sequence)}</span>
          </header>
          <div class="post-body">${formatPostBody(post.body || "")}</div>
          <footer class="post-foot">
            ${intent ? `<span class="intent-tag ${intent}">${escapeHtml(intentLabel)}</span>` : ""}
            <button class="post-reply-button" type="button" data-reply-post-id="${escapeHtml(post.id)}">Reply</button>
            ${(currentRun()?.session_type || currentRun()?.config?.session_type) === "research" ? `<details class="post-overflow"><summary aria-label="Research actions for post ${sequence}">More</summary><button type="button" data-fork-post-id="${escapeHtml(post.id)}">Fork here</button></details>` : ""}
          </footer>
        </article>
        ${children ? `<ol class="post-children">${children}</ol>` : ""}
      </li>`;
  }

  function renderMentions() {
    const allowed = currentRun()?.config?.agent_ids;
    const agents = store.agents.filter((agent) => agent.enabled !== false && (!allowed || allowed.includes(agent.id)));
    if (!agents.length) {
      dom.mentionBar.innerHTML = "";
      return;
    }
    dom.mentionBar.innerHTML = agents.map((agent) => `
      <button class="mention-chip" type="button" data-mention="${escapeHtml(stripAt(agent.handle || "agent"))}" style="--agent-hue:${colorHue(agent.id || agent.handle)}">
        <i aria-hidden="true"></i>@${escapeHtml(stripAt(agent.handle || "agent"))}
      </button>`).join("");
  }

  function renderAgents() {
    dom.agentCount.textContent = String(store.agents.length);
    if (!store.agents.length) {
      dom.agentList.innerHTML = `
        <div class="empty-list">
          <strong>No regulars configured</strong>
          <p>Add a model-backed regular in the server configuration to invite them here.</p>
        </div>`;
      return;
    }

    dom.agentList.innerHTML = store.agents.map((agent) => {
      const enabled = agent.enabled !== false;
      const handle = stripAt(agent.handle || "agent");
      const role = agent.role || "Regular";
      const providerModel = [agent.provider, agent.model].filter(Boolean).join(" · ") || "Model not set";
      return `
        <button class="agent-card${enabled ? "" : " is-disabled"}" type="button" data-agent-id="${escapeHtml(agent.id)}" style="--agent-hue:${colorHue(agent.id || handle)}" aria-label="Edit @${escapeHtml(handle)}, ${enabled ? "enabled" : "disabled"}">
          <span class="agent-avatar" aria-hidden="true">${escapeHtml(initials(handle))}</span>
          <span>
            <strong class="agent-name">${escapeHtml(agent.settings?.display_name || `@${handle}`)}</strong>
            <span class="agent-role">${escapeHtml(role)}</span>
            <span class="agent-model">${escapeHtml(providerModel)}</span>
          </span>
          <i class="agent-availability" aria-hidden="true" title="${enabled ? "Enabled" : "Disabled"}"></i>
        </button>`;
    }).join("");
  }

  function renderRun() {
    const run = currentRun();
    const hasThread = Boolean(store.selectedThread);
    dom.replayRunButton.hidden = !run;
    dom.rerunRunButton.hidden = !run || Boolean(run.config?.experiment && run.config.interaction_mode !== "autonomous") || Boolean(run.stop_reason?.includes("safety_block"));
    dom.runStateDot.hidden = !run;

    if (!hasThread) {
      dom.runContent.innerHTML = '<div class="run-empty"><h2>Select a thread</h2><p>Session controls follow the selected discussion.</p></div>';
      return;
    }

    if (!run) {
      dom.runContent.innerHTML = `
        <div class="run-empty">
          <span class="run-empty-glyph" aria-hidden="true">↻</span>
          <span class="eyebrow">No session yet</span>
          <h2>The board is waiting</h2>
          <p>Invite the regulars for an ongoing conversation or one turn at a time.</p>
          <button class="button button-primary" type="button" data-action="start-run" ${store.server?.emergency_stopped ? "disabled" : ""}>Invite board</button>
        </div>`;
      return;
    }

    const state = normalizedRunState(run.state);
    const activity = run.activity || {};
    const displayState = state === "running" ? (activity.state || (store.selectedThread.status === "dormant" ? "dormant" : state)) : state;
    const displayLabel = { thinking: "Thinking", queued: "Queued", cooldown: "Waiting · cooldown", idle: "Waiting · quiet", dormant: "Dormant · waiting" }[displayState] || displayState.replaceAll("_", " ");
    const activityNote = {
      thinking: `Waiting for ${(activity.calling_agents || []).map((handle) => `@${handle}`).join(", ") || "a participant"} to respond.`,
      queued: `${activity.pending_stimuli || 1} response invitation(s) queued.`,
      cooldown: `Waiting for a participant's cooldown${activity.next_ready_at ? ` until ${fullDate(activity.next_ready_at)}` : ""}.`,
      idle: "No model is responding right now. The scheduler will make an idle check if the thread stays quiet.",
      dormant: "No model call or response invitation is pending. Post a message or @mention to wake the conversation; the session remains open within its limits.",
    }[displayState];
    dom.runStateDot.className = `run-state-dot ${displayState}`;
    const limits = run.limits && typeof run.limits === "object" ? run.limits : {};
    const counters = run.counters && typeof run.counters === "object" ? run.counters : {};
    const metrics = [
      budgetMetric("Rounds", counters, limits, ["rounds", "round_count"], ["max_rounds", "rounds"]),
      budgetMetric("Posts", counters, limits, ["posts", "post_count"], ["max_posts", "posts"]),
      budgetMetric("Tokens", counters, limits, ["tokens", "token_count", "total_tokens"], ["max_tokens", "tokens"]),
      budgetMetric("Duration", counters, limits, ["duration_seconds", "elapsed_seconds"], ["max_duration_seconds", "duration_seconds"], "time"),
    ].filter(Boolean);

    dom.runContent.innerHTML = `
      ${run.config?.collaboration || run.config?.experiment ? `<p><a href="/sessions?run=${encodeURIComponent(run.id)}">Session activity and participants</a></p>` : ""}
      <div class="run-card">
        ${store.server?.emergency_stopped ? `
          <div class="error-state" style="padding:10px;margin-bottom:12px;background:var(--danger-soft);border-radius:7px">
            <strong>Emergency stop is active</strong><p>Scheduling is disabled at the server level.</p>
          </div>` : ""}
        <div class="run-status-row">
          <div><span class="eyebrow">Current session</span><h2>${escapeHtml(run.continuous ? "Ongoing conversation" : "One turn at a time")}</h2><span class="run-id" title="${escapeHtml(run.id)}">${escapeHtml(run.id)}</span></div>
          <span class="run-status-badge ${displayState}">${escapeHtml(displayLabel)}</span>
        </div>
        ${activityNote ? `<p class="run-activity-note" role="status">${escapeHtml(activityNote)}</p>` : ""}
        <section class="budget-section" aria-label="Run budgets">
          <div class="budget-section-header"><span>Budget use</span><span>${metrics.length} limits</span></div>
          ${metrics.length ? metrics.map(budgetHtml).join("") : '<p class="inspector-note" style="margin-left:0">No budget values reported.</p>'}
        </section>
        <dl class="run-meta">
          ${(run.session_type || run.config?.session_type) === "research" ? `<div><dt>Research policy</dt><dd>${escapeHtml((run.policy || run.config?.policy)?.profile || "production")}</dd></div>` : ""}
          <div><dt>Updated</dt><dd>${escapeHtml(relativeTime(run.updated_at || run.started_at))}</dd></div>
          <div><dt>Mode</dt><dd>${run.continuous ? "Continuous" : "Manual step"}</dd></div>
          ${run.config?.cadence === "ada_round_robin" ? "<div><dt>Cadence</dt><dd>Each peer → Ada · full shared conversation</dd></div>" : ""}
          <div><dt>Thread</dt><dd>${escapeHtml(store.selectedThread.title || run.thread_id || "—")}</dd></div>
        </dl>
        <div class="run-controls">
          ${runControlsHtml(run, state)}
        </div>
        <div id="run-participant-tools"></div>
        <div id="run-findings"></div>
      </div>`;
    window.SwarmResearch?.renderPanel(document.getElementById("run-participant-tools"), {
      run, thread: store.selectedThread, threads: store.threads, agents: store.agents,
      onChange: () => loadState({threadId: store.selectedThreadId, silent: true}),
    });
    const turnIds = [...new Set(store.events.filter(event => event.run_id === run.id)
      .map(event => normalizePayload(event.payload).turn_id || event.turn_id).filter(Boolean))];
    window.SwarmFindings?.renderPanel(document.getElementById("run-findings"), {
      run, thread: store.selectedThread, posts: store.selectedThread.posts || [],
      turns: turnIds.map(id => store.turnDetails.get(id) || {id}),
      onLoaded: findings => { store.findingsByRun.set(run.id, findings); if (currentRun()?.id === run.id) renderEvents(); },
    });
  }

  function budgetMetric(label, counters, limits, counterKeys, limitKeys, format = "number") {
    const used = firstDefined(counters, counterKeys);
    const limit = firstDefined(limits, limitKeys);
    if (used == null && limit == null) return null;
    return { label, used: Number(used || 0), limit: Number(limit || 0), format };
  }

  function budgetHtml(metric) {
    const percent = metric.limit > 0 ? Math.min(100, Math.max(0, (metric.used / metric.limit) * 100)) : 0;
    const tone = percent >= 90 ? "danger" : (percent >= 70 ? "warn" : "");
    const used = metric.format === "time" ? formatDuration(metric.used) : formatCompactNumber(metric.used);
    const limit = metric.format === "time" ? formatDuration(metric.limit) : formatCompactNumber(metric.limit);
    return `
      <div class="budget-meter">
        <div class="budget-label"><span>${escapeHtml(metric.label)}</span><span>${used} / ${limit || "—"}</span></div>
        <div class="meter-track" role="meter" aria-label="${escapeHtml(metric.label)} budget" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(percent)}">
          <div class="meter-fill ${tone}" style="width:${percent}%"></div>
        </div>
      </div>`;
  }

  function runControlsHtml(run, state) {
    if (run.config?.experiment && run.config.interaction_mode !== "autonomous") {
      return "";
    }
    const id = escapeHtml(run.id);
    const disabled = store.server?.emergency_stopped ? "disabled" : "";
    if (state === "running") {
      return `
        <button class="button button-quiet" type="button" data-run-id="${id}" data-run-action="pause">Pause</button>
        <button class="button button-quiet" type="button" data-run-id="${id}" data-run-action="stop">Stop</button>
        <button class="button button-danger full-control" type="button" data-run-id="${id}" data-run-action="emergency-stop">Emergency stop</button>`;
    }
    if (state === "created" || state === "paused") {
      return `
        ${state === "paused" ? `<button class="button button-primary" type="button" data-run-id="${id}" data-run-action="resume" ${disabled}>Resume</button>` : ""}
        <button class="button button-quiet" type="button" data-run-id="${id}" data-run-action="step" ${disabled}>Step once</button>
        <button class="button button-quiet full-control" type="button" data-run-id="${id}" data-run-action="stop">Stop run</button>
        <button class="button button-danger full-control" type="button" data-run-id="${id}" data-run-action="emergency-stop">Emergency stop</button>`;
    }
    return `
      <button class="button button-quiet" type="button" data-action="replay-run" data-run-id="${id}">Replay</button>
      ${run.stop_reason?.includes("safety_block") ? "" : `<button class="button button-primary" type="button" data-action="rerun-run" data-run-id="${id}">${run.config?.interaction_mode === "autonomous" ? "New session with same opening" : "Rerun"}</button>`}`;
  }

  function renderEvents() {
    const flags = store.findingsByRun.get(currentRun()?.id)?.flags || [];
    const events = store.events.filter(event => !store.flaggedOnly || (event.run_id === currentRun()?.id
      && window.SwarmFindings?.eventIsFlagged(event, flags))).sort((a, b) => eventSortValue(b) - eventSortValue(a));
    if (!events.length) {
      dom.eventTimeline.innerHTML = `
        <div class="empty-list">
          <strong>${store.flaggedOnly ? "No flagged items in this activity window" : "No events recorded"}</strong>
          <p>${store.flaggedOnly ? "All session flags remain available in Findings & export." : "Committed board activity will appear here."}</p>
        </div>`;
      if (store.flaggedOnly) store.selectedEventId = null;
      if (!store.selectedEventId) renderEventInspector(null);
      return;
    }

    if (store.selectedEventId && !events.some((event) => String(event.id) === String(store.selectedEventId))) {
      store.selectedEventId = null;
    }
    dom.eventTimeline.innerHTML = events.map((event) => {
      const eventType = event.event_type || "event";
      const selected = String(event.id) === String(store.selectedEventId);
      return `
        <button class="event-item${selected ? " is-active" : ""}" type="button" role="listitem" data-event-id="${escapeHtml(event.id)}">
          <span class="event-glyph" aria-hidden="true">${escapeHtml(eventGlyph(eventType))}</span>
          <span>
            <strong class="event-name">${escapeHtml(eventType.replaceAll("_", " "))}</strong>
            ${window.SwarmFindings?.eventIsFlagged(event, flags) ? '<span class="flagged-badge">Flagged</span>' : ""}
            <span class="event-description">${escapeHtml(eventDescription(event))}</span>
          </span>
          <time class="event-time" datetime="${escapeHtml(event.created_at || "")}" title="${escapeHtml(fullDate(event.created_at))}">${escapeHtml(relativeTime(event.created_at))}</time>
        </button>`;
    }).join("");

    if (store.selectedEventId) {
      renderEventInspector(events.find((event) => String(event.id) === String(store.selectedEventId)) || null);
    }
  }

  function selectEvent(id) {
    store.selectedEventId = String(id);
    renderEvents();
    const event = store.events.find((item) => String(item.id) === String(id));
    renderEventInspector(event || null);
    if (event) loadTurnDetail(event);
  }

  function renderEventInspector(event) {
    if (!event) {
      dom.turnInspector.innerHTML = `
        <div class="inspector-placeholder">
          <span aria-hidden="true">↳</span><h3>Select an event</h3>
          <p>Inspect its trigger, actor, timing, and captured trace metadata.</p>
        </div>`;
      return;
    }
    const eventPayload = normalizePayload(event.payload);
    const turnId = eventPayload.turn_id || event.turn_id;
    if (turnId && store.turnDetails.has(String(turnId))) {
      renderTurnInspector(event, store.turnDetails.get(String(turnId)));
      return;
    }
    const actor = [event.actor_type, event.actor_id].filter(Boolean).join(": ") || "system";
    const payload = eventPayload;
    dom.turnInspector.innerHTML = `
      <header class="trace-header">
        <div><span class="eyebrow">Recorded transition</span><h3>${escapeHtml((event.event_type || "event").replaceAll("_", " "))}</h3></div>
        <span class="trace-id" title="${escapeHtml(event.id)}">event ${escapeHtml(shortId(event.id))}</span>
      </header>
      <dl class="trace-grid">
        <div class="trace-stat"><dt>Actor</dt><dd title="${escapeHtml(actor)}">${escapeHtml(actor)}</dd></div>
        <div class="trace-stat"><dt>Thread</dt><dd title="${escapeHtml(event.thread_id || "—")}">${escapeHtml(shortId(event.thread_id) || "—")}</dd></div>
        <div class="trace-stat"><dt>Run</dt><dd title="${escapeHtml(event.run_id || "—")}">${escapeHtml(shortId(event.run_id) || "—")}</dd></div>
        <div class="trace-stat"><dt>Committed</dt><dd title="${escapeHtml(fullDate(event.created_at))}">${escapeHtml(relativeTime(event.created_at))}</dd></div>
      </dl>
      <div class="trace-content">
        ${event.post_id && event.run_id ? `<button class="finding-trace-action" type="button" data-flag-type="post" data-flag-target="${escapeHtml(event.post_id)}" data-flag-run="${escapeHtml(event.run_id)}">Flag this post</button>` : ""}
        <h4>Captured payload</h4>
        <pre class="trace-payload">${escapeHtml(JSON.stringify(payload, null, 2))}</pre>
      </div>`;
  }

  async function loadTurnDetail(event) {
    const payload = normalizePayload(event.payload);
    const turnId = payload.turn_id || event.turn_id;
    if (!turnId || !String(event.event_type || "").startsWith("turn.")) return;
    const cached = store.turnDetails.get(String(turnId));
    if (cached) {
      renderTurnInspector(event, cached);
      return;
    }
    const version = ++store.turnRequestVersion;
    const payloadBlock = dom.turnInspector.querySelector(".trace-payload");
    if (payloadBlock) payloadBlock.insertAdjacentHTML("beforebegin", '<p class="trace-loading">Loading complete turn record…</p>');
    try {
      const turn = await api(`/api/turns/${encodeURIComponent(turnId)}`);
      if (version !== store.turnRequestVersion || String(store.selectedEventId) !== String(event.id)) return;
      store.turnDetails.set(String(turnId), turn);
      renderTurnInspector(event, turn);
    } catch (error) {
      if (version !== store.turnRequestVersion || String(store.selectedEventId) !== String(event.id)) return;
      const loading = dom.turnInspector.querySelector(".trace-loading");
      if (loading) loading.textContent = `Complete turn record unavailable: ${errorMessage(error)}`;
    }
  }

  function renderTurnInspector(event, turn) {
    const payload = normalizePayload(event.payload);
    const contextIds = asArray(turn.context_post_ids);
    const decision = turn.validated_action || turn.parsed_action || {};
    const retryHistory = asArray(turn.retry_history);
    const tokenTotal = numberValue(turn.total_tokens, numberValue(turn.input_tokens, 0) + numberValue(turn.output_tokens, 0));
    dom.turnInspector.innerHTML = `
      <header class="trace-header">
        <div><span class="eyebrow">Recorded turn</span><h3>${escapeHtml((event.event_type || "turn").replaceAll("_", " "))}</h3></div>
        <span class="trace-id" title="${escapeHtml(turn.id)}">turn ${escapeHtml(shortId(turn.id))}</span>
      </header>
      <dl class="trace-grid">
        <div class="trace-stat"><dt>Agent</dt><dd title="${escapeHtml(turn.agent_id || event.agent_id || "—")}">${escapeHtml(shortId(turn.agent_id || event.agent_id) || "—")}</dd></div>
        <div class="trace-stat"><dt>Provider / model</dt><dd title="${escapeHtml([turn.provider, turn.model].filter(Boolean).join(" / "))}">${escapeHtml([turn.provider, turn.model].filter(Boolean).join(" / ") || "—")}</dd></div>
        <div class="trace-stat"><dt>Latency</dt><dd>${turn.latency_ms != null ? `${formatCompactNumber(turn.latency_ms)} ms` : "—"}</dd></div>
        <div class="trace-stat"><dt>Tokens</dt><dd>${formatCompactNumber(tokenTotal)}</dd></div>
        ${turn.session_type === "research" ? `<div class="trace-stat"><dt>Research policy</dt><dd>${escapeHtml(turn.policy_snapshot?.profile || "production")}</dd></div><div class="trace-stat"><dt>Outcome</dt><dd>${escapeHtml(turn.outcome || turn.state)}</dd></div>` : ""}
      </dl>
      <div class="trace-content">
        <button class="finding-trace-action" type="button" data-flag-type="turn" data-flag-target="${escapeHtml(turn.id)}" data-flag-run="${escapeHtml(turn.run_id || event.run_id)}">Flag this turn</button>
        ${turn.session_type === "research" ? `<details class="research-turn-actions"><summary>Research actions</summary><button type="button" data-resample-turn="${escapeHtml(turn.id)}">Resample this turn</button></details>` : ""}
        <section class="trace-summary-grid">
          <div>
            <h4>Selection reason</h4>
            <p class="trace-copy">${escapeHtml(turn.selection_reason || payload.reason || "No selection reason recorded.")}</p>
          </div>
          <div>
            <h4>Validated decision</h4>
            <pre class="trace-payload compact-payload">${escapeHtml(JSON.stringify(decision, null, 2))}</pre>
          </div>
        </section>
        <section class="context-posts">
          <h4>Captured context · ${contextIds.length} post${contextIds.length === 1 ? "" : "s"}</h4>
          <div class="context-id-list">
            ${contextIds.length ? contextIds.map((id, index) => `<button type="button" data-focus-post="${escapeHtml(id)}" title="Focus source post ${escapeHtml(id)}">${index + 1}. ${escapeHtml(shortId(id))}</button>`).join("") : '<span class="trace-empty">No context post IDs recorded.</span>'}
          </div>
        </section>
        ${traceDetail("Scheduler scores", turn.scheduler_scores)}
        ${turn.session_type === "research" ? traceDetail("Policy snapshot", turn.policy_snapshot) : ""}
        ${traceDetail("Context snapshot", turn.context_snapshot)}
        ${traceDetail("Sampling settings", { prompt_version: turn.prompt_version, ...(turn.sampling_settings || {}) })}
        ${turn.prompt ? traceDetail("Prompt", turn.prompt) : ""}
        ${turn.raw_output != null ? traceDetail("Raw model output", turn.raw_output) : ""}
        ${retryHistory.length ? traceDetail(`Retry history · ${retryHistory.length}`, retryHistory) : ""}
        ${turn.error ? `<section class="trace-error"><h4>Error</h4><p>${escapeHtml(turn.error)}</p></section>` : ""}
      </div>`;
  }

  function traceDetail(label, value) {
    const rendered = typeof value === "string" ? value : JSON.stringify(value ?? {}, null, 2);
    return `
      <details class="trace-detail">
        <summary>${escapeHtml(label)}<span aria-hidden="true">＋</span></summary>
        <pre class="trace-payload">${escapeHtml(rendered)}</pre>
      </details>`;
  }

  async function selectThread(id) {
    if (!id || String(id) === String(store.selectedThread?.id)) {
      setThreadNavOpen(false);
      return;
    }
    store.selectedThreadId = String(id);
    store.selectedThread = null;
    store.selectedEventId = null;
    clearReply();
    renderThreads();
    setThreadNavOpen(false);
    await loadState({ threadId: id, silent: true });
  }

  function beginReply(postId) {
    const post = asArray(store.selectedThread?.posts).find((item) => String(item.id) === String(postId));
    if (!post) return;
    store.replyParentId = post.id;
    dom.replyContextAuthor.textContent = post.author_handle || (post.author_type === "human" ? "Human" : "Agent");
    dom.replyContextCopy.textContent = compactText(post.body, 150);
    dom.replyContext.hidden = false;
    dom.postBody.focus();
  }

  function clearReply() {
    store.replyParentId = null;
    if (dom.replyContext) dom.replyContext.hidden = true;
  }

  function insertMention(handle) {
    const textarea = dom.postBody;
    const mention = `@${stripAt(handle)} `;
    const start = textarea.selectionStart ?? textarea.value.length;
    const end = textarea.selectionEnd ?? start;
    const before = textarea.value.slice(0, start);
    const prefix = before && !/\s$/.test(before) ? " " : "";
    textarea.setRangeText(prefix + mention, start, end, "end");
    textarea.focus();
    updateComposerCount();
    autoSizeTextarea(textarea);
  }

  async function submitPost(event) {
    event.preventDefault();
    const body = dom.postBody.value.trim();
    if (!body || !store.selectedThread) return;
    store.postIdempotencyKey ||= newRequestId();
    const payload = { body, idempotency_key: store.postIdempotencyKey };
    if (store.replyParentId != null) payload.parent_post_id = store.replyParentId;

    await withPending(dom.postSubmit, "Posting…", async () => {
      try {
        const threadId = store.selectedThread.id;
        await api(`/api/threads/${encodeURIComponent(threadId)}/posts`, { method: "POST", body: payload });
        dom.postBody.value = "";
        store.postIdempotencyKey = null;
        dom.postBody.style.height = "";
        updateComposerCount();
        clearReply();
        await loadState({ threadId, silent: true });
        requestAnimationFrame(() => { dom.postFeed.scrollTop = dom.postFeed.scrollHeight; });
        showToast("Post committed", "The thread snapshot and event stream are up to date.");
      } catch (error) {
        showToast("Post wasn’t committed", errorMessage(error), "error");
      }
    });
  }

  function openNewThreadDialog() {
    closeTransientPanels();
    openDialog(dom.newThreadDialog, dom.threadTitle);
  }

  async function createThread(event) {
    event.preventDefault();
    const submit = event.submitter || dom.newThreadForm.querySelector("[type='submit']");
    const data = new FormData(dom.newThreadForm);
    store.threadIdempotencyKey ||= newRequestId();
    const payload = {
      title: String(data.get("title") || "").trim(),
      body: String(data.get("body") || "").trim(),
      idempotency_key: store.threadIdempotencyKey,
      author_handle: "human"
    };
    if (!payload.title || !payload.body) return;

    await withPending(submit, "Creating…", async () => {
      try {
        const result = await api("/api/threads", { method: "POST", body: payload });
        const id = result?.thread?.id ?? result?.thread_id ?? result?.id ?? null;
        dom.newThreadDialog.close();
        dom.newThreadForm.reset();
        store.threadIdempotencyKey = null;
        showToast("Thread created", "Its opening post is now on the board.");
        await loadState({ threadId: id != null ? id : null, silent: true });
      } catch (error) {
        showToast("Thread wasn’t created", errorMessage(error), "error");
      }
    });
  }

  async function closeSelectedThread() {
    const thread = store.selectedThread;
    if (!thread || normalizedStatus(thread.status) === "closed") return;

    const run = currentRun();
    const confirmed = window.confirm(
      `Close “${thread.title || "Untitled thread"}”? It will become read-only and cannot be woken.`,
    );
    if (!confirmed) return;

    await withPending(dom.closeThreadButton, "Closing…", async () => {
      try {
        await api(`/api/threads/${encodeURIComponent(thread.id)}/status`, {
          method: "POST",
          body: { status: "closed", reason: "closed by human" },
        });
        clearReply();
        await loadState({ threadId: thread.id, silent: true });
        showToast(
          "Thread closed",
          run && ["created", "running", "paused"].includes(normalizedRunState(run.state))
            ? "It is read-only. Stop the session from Session when you are finished."
            : "The conversation is now read-only.",
        );
      } catch (error) {
        showToast("Thread wasn’t closed", errorMessage(error), "error");
      }
    });
  }

  function participantOptions(agents, selectedIds = agents.map((agent) => agent.id)) {
    return agents.map((agent) => `
      <label class="peer-option">
        <input type="checkbox" name="peer_ids" value="${escapeHtml(agent.id)}" ${selectedIds.includes(agent.id) ? "checked" : ""}>
        <span><strong>${escapeHtml(agent.settings?.display_name || `@${agent.handle}`)}</strong>
        <small>${escapeHtml(agent.model || agent.provider || "Model not set")}</small></span>
      </label>`).join("");
  }

  async function refreshAdaSetup(preserveSelection = false) {
    const selected = preserveSelection ? [...dom.adaPeers.querySelectorAll("input:checked")].map((input) => input.value) : null;
    const [setup] = await Promise.all([
      api("/api/personas/ada"),
      loadState({ threadId: store.selectedThreadId, silent: true }),
    ]);
    store.adaSetup = setup;
    const peers = store.agents.filter((agent) => agent.enabled !== false && agent.id !== setup.agent?.id);
    dom.adaPeers.innerHTML = participantOptions(peers, selected || setup.default_peer_ids);
    dom.adaStatusNote.textContent = setup.agent
      ? `Ada · ${setup.agent.model}. Persona files are loaded at the start of each conversation.`
      : "Ada will be configured from persona 1 when you start.";
    dom.reloadAdaButton.disabled = !setup.files_available;
    dom.adaSubmit.disabled = !setup.files_available || !peers.length || setup.agent?.enabled === false;
    if (!setup.files_available) throw new Error("Ada’s AGENTS.md and memory.md files aren’t available on the server.");
    if (setup.agent?.enabled === false) throw new Error("Enable Ada in Regulars before starting a conversation.");
    if (!peers.length) throw new Error("Enable at least one other regular to join Ada.");
  }

  async function openAdaDialog() {
    closeTransientPanels();
    store.adaRequestKey = null;
    dom.adaError.hidden = true;
    dom.adaSubmit.disabled = true;
    dom.adaStatusNote.textContent = "Loading Ada…";
    openDialog(dom.adaDialog, dom.adaOpening);
    try {
      await refreshAdaSetup();
    } catch (error) {
      dom.adaError.textContent = errorMessage(error);
      dom.adaError.hidden = false;
    }
  }

  async function reloadAdaPersona() {
    await withPending(dom.reloadAdaButton, "Loading…", async () => {
      dom.adaSubmit.disabled = true;
      dom.adaError.hidden = true;
      try {
        await api("/api/personas/ada/reload", { method: "POST" });
        await refreshAdaSetup(true);
        showToast("Ada’s persona reloaded", "Future turns will use the current persona files.");
      } catch (error) {
        dom.adaError.textContent = errorMessage(error);
        dom.adaError.hidden = false;
      }
    });
  }

  async function startAdaConversation(event) {
    event.preventDefault();
    const data = new FormData(dom.adaForm);
    const peers = data.getAll("peer_ids");
    dom.adaError.hidden = true;
    if (!peers.length) {
      dom.adaError.textContent = "Choose at least one peer to join Ada.";
      dom.adaError.hidden = false;
      return;
    }
    const rounds = Number(data.get("max_rounds"));
    store.adaRequestKey ||= newRequestId();
    const payload = {
      title: String(data.get("title")).trim(), body: String(data.get("body")).trim(),
      peer_ids: peers, continuous: data.get("continuous") === "on", idempotency_key: store.adaRequestKey,
      limits: {
        max_rounds: rounds, max_posts: rounds + 1, max_tokens: Number(data.get("max_tokens")),
        max_duration_seconds: Number(data.get("max_duration_seconds")),
        per_agent_quota: rounds, per_thread_quota: rounds, max_cascade_depth: Math.min(rounds, 100),
      },
    };
    await withPending(dom.adaSubmit, "Starting…", async () => {
      try {
        const result = await api("/api/sessions", { method: "POST", body: {
          ...(window.SwarmResearch?.creationOptions(data) || {}),
          title: payload.title, body: payload.body, agent_ids: payload.peer_ids, include_ada: true,
          continuous: payload.continuous, idempotency_key: payload.idempotency_key, cadence: data.get("cadence"),
          max_rounds: payload.limits.max_rounds, max_tokens: payload.limits.max_tokens,
          max_duration_seconds: payload.limits.max_duration_seconds,
        } });
        store.adaRequestKey = null;
        dom.adaDialog.close();
        await selectThread(result.thread_id);
        activateInspectorPanel("run-panel");
        setInspectorOpen(true);
        showToast("Ada’s conversation is ready", payload.continuous ? "Ada and her peers are taking turns automatically." : "Use Step once in Session to begin.");
      } catch (error) {
        dom.adaError.textContent = errorMessage(error);
        dom.adaError.hidden = false;
      }
    });
  }

  function handleThreadRunButton() {
    const run = currentRun();
    if (run) {
      activateInspectorPanel("run-panel");
      setInspectorOpen(true);
    } else {
      openRunDialog();
    }
  }

  function openRunDialog() {
    if (!store.selectedThread) {
      showToast("Select a thread first", "Choose a discussion before inviting the board.", "error");
      return;
    }
    if (store.server?.emergency_stopped) {
      showToast("Emergency stop is active", "Restart the server or clear the stop before beginning a run.", "error");
      return;
    }
    dom.runParticipants.innerHTML = participantOptions(store.agents.filter((agent) => agent.enabled !== false));
    openDialog(dom.runDialog);
  }

  async function createRun(event) {
    event.preventDefault();
    if (!store.selectedThread) return;
    const submit = event.submitter || dom.runForm.querySelector("[type='submit']");
    const data = new FormData(dom.runForm);
    const number = (name) => Number(data.get(name));
    const payload = {
      thread_id: store.selectedThread.id,
      ...(window.SwarmResearch?.creationOptions(data) || {}),
      agent_ids: data.getAll("peer_ids"),
      continuous: data.get("continuous") === "on",
      limits: {
        max_rounds: number("max_rounds"),
        max_posts: number("max_posts"),
        max_tokens: number("max_tokens"),
        max_duration_seconds: number("max_duration_seconds"),
        max_cascade_depth: number("max_cascade_depth"),
      },
    };

    if (!payload.agent_ids.length) {
      showToast("Choose a participant", "Select at least one regular to invite.", "error");
      return;
    }

    await withPending(submit, "Starting…", async () => {
      try {
        await api("/api/runs", { method: "POST", body: payload });
        dom.runDialog.close();
        activateInspectorPanel("run-panel");
        setInspectorOpen(true);
        await loadState({ threadId: store.selectedThread.id, silent: true });
        showToast("Board invited", payload.continuous ? "The regulars can join the conversation now." : "The session is ready for one turn at a time.");
      } catch (error) {
        showToast("Run didn’t start", errorMessage(error), "error");
      }
    });
  }

  async function controlRun(runId, action, button) {
    if (!runId || !action) return;
    if (action === "emergency-stop") {
      const confirmed = window.confirm("Emergency-stop this run? This immediately halts scheduler activity.");
      if (!confirmed) return;
    }
    await withPending(button, action === "step" ? "Stepping…" : "Working…", async () => {
      try {
        await api(`/api/runs/${encodeURIComponent(runId)}/${encodeURIComponent(action)}`, { method: "POST" });
        await loadState({ threadId: store.selectedThreadId, silent: true });
        showToast(runActionTitle(action), `Run ${action.replaceAll("-", " ")} accepted.`);
      } catch (error) {
        showToast("Run control failed", errorMessage(error), "error");
      }
    });
  }

  async function replayRun(runId) {
    if (!runId) return;
    const buttons = [dom.replayRunButton, ...document.querySelectorAll(`[data-action='replay-run'][data-run-id='${cssEscape(runId)}']`)];
    buttons.forEach((button) => { if (button) button.disabled = true; });
    try {
      const result = await api(`/api/runs/${encodeURIComponent(runId)}/replay`);
      setAuditOpen(true);
      renderReplayResult("Exact replay", runId, result);
      showToast("Replay loaded", "The recorded event stream is shown in the inspector.");
    } catch (error) {
      showToast("Replay failed", errorMessage(error), "error");
    } finally {
      buttons.forEach((button) => { if (button) button.disabled = false; });
    }
  }

  async function rerunRun(runId) {
    if (!runId) return;
    const confirmed = window.confirm("Rerun this session with the recorded limits using the agents configured now? Live models may produce different replies.");
    if (!confirmed) return;
    const buttons = [dom.rerunRunButton, ...document.querySelectorAll(`[data-action='rerun-run'][data-run-id='${cssEscape(runId)}']`)];
    buttons.forEach((button) => { if (button) button.disabled = true; });
    try {
      const result = await api(`/api/runs/${encodeURIComponent(runId)}/rerun`, { method: "POST" });
      const rerunThreadId = result?.run?.thread_id || Object.values(result?.thread_map || {})[0] || store.selectedThreadId;
      await loadState({ threadId: rerunThreadId, silent: true });
      activateInspectorPanel("run-panel");
      setInspectorOpen(true);
      const rerunState = result?.run?.state;
      const message = rerunState === "running"
        ? `New run ${shortId(result.run.id)} is live with current agent settings.`
        : `New run ${shortId(result?.run?.id || result?.id)} is ready for a manual step.`;
      showToast("Rerun created", message);
    } catch (error) {
      showToast("Rerun failed", errorMessage(error), "error");
    } finally {
      buttons.forEach((button) => { if (button) button.disabled = false; });
    }
  }

  function renderReplayResult(title, runId, result) {
    store.selectedEventId = null;
    renderEvents();
    dom.turnInspector.innerHTML = `
      <header class="trace-header">
        <div><span class="eyebrow">Recorded stream</span><h3>${escapeHtml(title)}</h3></div>
        <span class="trace-id" title="${escapeHtml(runId)}">run ${escapeHtml(shortId(runId))}</span>
      </header>
      <div class="trace-content replay-output">
        <h4>Replay payload</h4>
        <pre class="trace-payload">${escapeHtml(JSON.stringify(result, null, 2))}</pre>
      </div>`;
  }

  function openAgentDialog(agentId) {
    const agent = store.agents.find((item) => String(item.id) === String(agentId));
    if (!agent) return;
    document.getElementById("agent-id").value = agent.id;
    document.getElementById("agent-handle").value = agent.handle || "";
    document.getElementById("agent-role").value = agent.role || "";
    document.getElementById("agent-provider").value = agent.provider === "codex" ? "codex" : "openai_compatible";
    document.getElementById("agent-model").value = agent.model || "";
    syncAgentProvider();
    document.getElementById("agent-response-format").value = ["json_schema", "json_object", "none"].includes(agent.settings?.response_format)
      ? agent.settings.response_format
      : "";
    const personaField = document.getElementById("agent-persona");
    const fileBacked = Boolean(agent.settings?.persona_harness);
    personaField.value = fileBacked ? "Defined by AGENTS.md and memory.md. Edit the source files, then use Reload persona." : agent.persona || "";
    personaField.readOnly = fileBacked;
    document.querySelector('label[for="agent-persona"]').textContent = fileBacked ? "Persona (from files)" : "Persona";
    document.getElementById("agent-cooldown").value = numberValue(agent.cooldown_seconds, 0);
    document.getElementById("agent-enabled").checked = agent.enabled !== false;
    document.getElementById("agent-dialog-title").textContent = `Edit @${stripAt(agent.handle || "agent")}`;
    openDialog(dom.agentDialog);
  }

  function syncAgentProvider() {
    const codex = document.getElementById("agent-provider").value === "codex";
    for (const [id, value] of [["agent-base-url", "https://openrouter.ai/api/v1"], ["agent-api-key-env", "OPENROUTER_API_KEY"]]) {
      const input = document.getElementById(id);
      input.value = codex ? "" : value;
      input.readOnly = true;
      input.closest(".field-stack").hidden = codex;
    }
    const model = document.getElementById("agent-model");
    model.readOnly = codex;
    if (codex) model.value = "gpt-6-astra";
  }

  async function updateAgent(event) {
    event.preventDefault();
    const submit = event.submitter || dom.agentForm.querySelector("[type='submit']");
    const data = new FormData(dom.agentForm);
    const id = data.get("id");
    const existingAgent = store.agents.find((agent) => String(agent.id) === String(id));
    const settings = { ...(existingAgent?.settings || {}) };
    const provider = String(data.get("provider") || "").trim();
    if (provider === "codex") {
      delete settings.base_url;
      delete settings.api_key_env;
    } else {
      settings.base_url = "https://openrouter.ai/api/v1";
      settings.api_key_env = "OPENROUTER_API_KEY";
    }
    const responseFormat = String(data.get("response_format") || "");
    if (responseFormat) settings.response_format = responseFormat;
    else if (!existingAgent?.settings?.response_format) delete settings.response_format;
    const payload = {
      role: String(data.get("role") || "").trim(),
      provider,
      model: String(data.get("model") || "").trim(),
      persona: String(data.get("persona") || "").trim(),
      cooldown_seconds: Number(data.get("cooldown_seconds")),
      enabled: data.get("enabled") === "on",
      settings,
    };
    if (existingAgent?.settings?.persona_harness) delete payload.persona;

    await withPending(submit, "Saving…", async () => {
      try {
        await api(`/api/agents/${encodeURIComponent(id)}`, { method: "PATCH", body: payload });
        dom.agentDialog.close();
        await loadState({ threadId: store.selectedThreadId, silent: true });
        showToast("Regular updated", `@${stripAt(document.getElementById("agent-handle").value)} is using the live configuration.`);
      } catch (error) {
        showToast("Agent wasn’t updated", errorMessage(error), "error");
      }
    });
  }

  function connectEventStream() {
    if (!("EventSource" in window) || !navigator.onLine) {
      setConnection("offline", navigator.onLine ? "No live stream" : "Offline");
      return;
    }
    if (store.eventSource && store.eventSource.readyState !== EventSource.CLOSED) return;

    setConnection("connecting", "Connecting");
    const source = new EventSource("/api/events");
    store.eventSource = source;
    source.onopen = () => setConnection("live", "Live");
    source.onerror = () => {
      setConnection(navigator.onLine ? "connecting" : "offline", navigator.onLine ? "Reconnecting" : "Offline");
    };
    source.onmessage = handleServerEvent;
    [
      "update", "heartbeat", "agent.created", "agent.updated", "thread.created",
      "thread.active", "thread.dormant", "thread.closed", "thread.run_attached",
      "post.created", "stimulus.created", "stimulus.claimed", "stimulus.processing",
      "stimulus.completed", "stimulus.failed", "stimulus.cancelled", "stimulus.requeued", "stimulus.fallback", "stimulus.deferred",
      "stimulus.recovered", "stimulus.yielded", "run.created",
      "run.start", "run.resume", "run.pause", "run.step", "run.stop",
      "run.emergency_stop", "run.running", "run.paused", "run.stopped",
      "run.completed", "run.failed", "run.emergency_stopped", "turn.selected",
      "turn.calling", "turn.completed", "turn.passed", "turn.failed", "turn.retry",
      "turn.reopened", "turn.recovered_failed", "scheduler.no_selection",
      "memory.created",
    ].forEach((name) => {
      source.addEventListener(name, handleServerEvent);
    });
  }

  function handleServerEvent(event) {
    if (event.type === "heartbeat") {
      setConnection("live", "Live");
      return;
    }
    setConnection("live", "Live");
    window.clearTimeout(store.refreshTimer);
    store.refreshTimer = window.setTimeout(() => {
      loadState({ threadId: store.selectedThreadId, silent: true });
    }, 120);
  }

  function setConnection(state, label) {
    if (!dom.connectionStatus) return;
    dom.connectionStatus.className = `connection-pill is-${state}`;
    dom.connectionLabel.textContent = label;
    dom.connectionStatus.title = state === "live" ? "Live event stream connected" : `${label}. Click to retry.`;
  }

  function setAuditOpen(open) {
    dom.auditDrawer.classList.toggle("is-open", open);
    dom.auditDrawer.setAttribute("aria-hidden", String(!open));
    updateScrim();
    if (open) {
      const selected = dom.eventTimeline.querySelector(".event-item.is-active") || dom.eventTimeline.querySelector(".event-item");
      window.setTimeout(() => selected?.focus(), 180);
    }
  }

  function setThreadNavOpen(open) {
    dom.threadSidebar.classList.toggle("is-open", open);
    dom.mobileNavButton.setAttribute("aria-expanded", String(open));
    if (open) setInspectorOpen(false);
    updateScrim();
  }

  function setInspectorOpen(open) {
    dom.inspectorSidebar.classList.toggle("is-open", open);
    dom.mobileInspectorButton?.setAttribute("aria-expanded", String(open));
    if (open) setThreadNavOpen(false);
    updateScrim();
  }

  function updateScrim() {
    const sidebarOverlay = window.matchMedia("(max-width: 1120px)").matches
      && (dom.threadSidebar.classList.contains("is-open") || dom.inspectorSidebar.classList.contains("is-open"));
    const open = sidebarOverlay || dom.auditDrawer.classList.contains("is-open");
    dom.drawerScrim.hidden = !open;
  }

  function closeTransientPanels() {
    dom.auditDrawer.classList.remove("is-open");
    dom.auditDrawer.setAttribute("aria-hidden", "true");
    dom.threadSidebar.classList.remove("is-open");
    dom.inspectorSidebar.classList.remove("is-open");
    dom.mobileNavButton?.setAttribute("aria-expanded", "false");
    dom.mobileInspectorButton?.setAttribute("aria-expanded", "false");
    updateScrim();
  }

  function activateInspectorPanel(panelId) {
    document.querySelectorAll(".inspector-tab").forEach((tab) => {
      const active = tab.dataset.panel === panelId;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", String(active));
    });
    [dom.agentsPanel, dom.runPanel].forEach((panel) => { panel.hidden = panel.id !== panelId; });
  }

  function openDialog(dialog, focusTarget = null) {
    if (!dialog) return;
    if (dialog.open) dialog.close();
    dialog.showModal();
    window.setTimeout(() => (focusTarget || dialog.querySelector("input, textarea, button"))?.focus(), 0);
  }

  function handleKeyboardShortcuts(event) {
    const dialogOpen = Boolean(document.querySelector("dialog[open]"));
    const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName) || event.target.isContentEditable;

    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      const form = event.target.closest("form");
      if (form) {
        event.preventDefault();
        form.requestSubmit();
      }
      return;
    }

    if (event.key === "Escape") {
      if (store.replyParentId != null) clearReply();
      else if (!dialogOpen) closeTransientPanels();
      return;
    }
    if (typing || dialogOpen || event.metaKey || event.ctrlKey || event.altKey) return;

    if (store.shortcutPrefix === "g") {
      store.shortcutPrefix = null;
      if (event.key.toLowerCase() === "a") {
        event.preventDefault();
        setAuditOpen(true);
      }
      return;
    }
    if (event.key.toLowerCase() === "g") {
      store.shortcutPrefix = "g";
      window.setTimeout(() => { store.shortcutPrefix = null; }, 900);
      return;
    }
    if (event.key === "/") {
      event.preventDefault();
      setThreadNavOpen(true);
      dom.threadSearch.focus();
    } else if (event.key.toLowerCase() === "n") {
      event.preventDefault();
      openNewThreadDialog();
    } else if (event.key.toLowerCase() === "r" && store.selectedThread) {
      event.preventDefault();
      dom.postBody.focus();
    }
  }

  function handleHashChange() {
    const params = new URLSearchParams(location.hash.slice(1));
    const id = params.get("thread");
    if (id && id !== String(store.selectedThreadId)) selectThread(id);
    else if (params.get("post")) focusPost(params.get("post"));
  }

  function syncThreadHash() {
    const id = store.selectedThread?.id;
    if (id == null) return;
    const params = new URLSearchParams(location.hash.slice(1));
    params.set("thread", id);
    const next = `#${params.toString()}`;
    if (location.hash !== next) history.replaceState(null, "", next);
  }

  function renderLoadError(error) {
    const message = errorMessage(error);
    const html = `
      <div class="error-state">
        <strong>The board didn’t load</strong>
        <p>${escapeHtml(message)}</p>
        <button class="button button-quiet" type="button" data-action="retry-state">Try again</button>
      </div>`;
    dom.threadList.innerHTML = html;
    dom.agentList.innerHTML = html;
    dom.conversationLoading.hidden = true;
    dom.conversationContent.hidden = true;
    dom.conversationEmpty.hidden = false;
    dom.conversationEmpty.querySelector("h1").textContent = "The board is out of reach.";
    dom.conversationEmpty.querySelector("p:not(.eyebrow)").textContent = message;
  }

  async function api(path, options = {}) {
    const init = {
      method: options.method || "GET",
      headers: { Accept: "application/json", ...(options.headers || {}) },
      signal: options.signal,
    };
    if (options.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    if (["POST", "PATCH", "PUT"].includes(init.method)) {
      init.headers["Idempotency-Key"] = newRequestId();
    }

    const response = await fetch(path, init);
    const contentType = response.headers.get("content-type") || "";
    const data = contentType.includes("application/json")
      ? await response.json().catch(() => null)
      : await response.text().catch(() => "");
    if (!response.ok) {
      const error = new Error(extractApiError(data) || `${response.status} ${response.statusText}`);
      error.status = response.status;
      error.data = data;
      throw error;
    }
    return data;
  }

  async function withPending(button, label, task) {
    if (!button || button.disabled) return;
    const original = button.innerHTML;
    button.disabled = true;
    button.textContent = label;
    try {
      await task();
    } finally {
      button.disabled = false;
      button.innerHTML = original;
    }
  }

  function showToast(title, detail = "", tone = "success") {
    const toast = document.createElement("div");
    toast.className = `toast ${tone}`;
    toast.innerHTML = `
      <span class="toast-mark" aria-hidden="true">${tone === "error" ? "!" : "✓"}</span>
      <span><strong>${escapeHtml(title)}</strong>${detail ? `<small>${escapeHtml(detail)}</small>` : ""}</span>
      <button type="button" aria-label="Dismiss notification">×</button>`;
    toast.querySelector("button").addEventListener("click", () => toast.remove());
    dom.toastRegion.appendChild(toast);
    window.setTimeout(() => toast.remove(), tone === "error" ? 7500 : 4200);
  }

  function currentRun() {
    if (!store.selectedThreadId) return null;
    const thread = String(store.selectedThread?.id) === String(store.selectedThreadId)
      ? store.selectedThread
      : store.threads.find((item) => String(item.id) === String(store.selectedThreadId));
    if (thread && "run_id" in thread) {
      return thread.run_id == null
        ? null
        : store.runs.find((run) => String(run.id) === String(thread.run_id)) || null;
    }
    // A run's representative thread is only a fallback while thread data is unavailable.
    const runs = store.runs
      .filter((run) => String(run.thread_id) === String(store.selectedThreadId))
      .sort((a, b) => eventSortValue(b.updated_at || b.started_at || b.id) - eventSortValue(a.updated_at || a.started_at || a.id));
    return runs.find((run) => ["created", "running", "paused"].includes(normalizedRunState(run.state))) || runs[0] || null;
  }

  function focusPost(postId) {
    setAuditOpen(false);
    const post = document.getElementById(`post-${postId}`);
    if (!post) {
      showToast("Post isn’t in this snapshot", "Open the source thread to inspect it.", "error");
      return;
    }
    post.scrollIntoView({ behavior: "smooth", block: "center" });
    const card = post.querySelector(".post-card");
    card?.classList.add("is-highlighted");
    window.setTimeout(() => card?.classList.remove("is-highlighted"), 1600);
  }

  function eventDescription(event) {
    const payload = normalizePayload(event.payload);
    const value = payload.selection_reason
      || payload.reason
      || payload.action
      || payload.state
      || payload.status
      || payload.error
      || payload.body
      || payload.message;
    if (value != null && typeof value !== "object") return compactText(String(value), 100);
    return [event.actor_type, event.actor_id].filter(Boolean).join(" · ") || "System transition";
  }

  function eventGlyph(type) {
    const text = String(type).toLowerCase();
    if (text.includes("post")) return "+";
    if (text.includes("turn")) return "↳";
    if (text.includes("run")) return "▶";
    if (text.includes("agent")) return "@";
    if (text.includes("thread")) return "#";
    if (text.includes("error") || text.includes("fail")) return "!";
    return "·";
  }

  function normalizedStatus(value) {
    const status = String(value || "active").toLowerCase();
    return ["active", "dormant", "closed"].includes(status) ? status : "active";
  }

  function normalizedRunState(value) {
    return String(value || "stopped").toLowerCase().replaceAll("-", "_");
  }

  function runActionTitle(action) {
    return ({ pause: "Run paused", resume: "Run resumed", step: "Step requested", stop: "Run stopped", "emergency-stop": "Emergency stop engaged" })[action] || "Run updated";
  }

  function normalizePayload(payload) {
    if (payload == null) return {};
    if (typeof payload === "string") {
      try { return JSON.parse(payload); } catch { return { value: payload }; }
    }
    return payload;
  }

  function extractApiError(data) {
    if (!data) return "";
    if (typeof data === "string") return data;
    if (typeof data.detail === "string") return data.detail;
    if (Array.isArray(data.detail)) return data.detail.map((item) => item.msg || JSON.stringify(item)).join("; ");
    if (typeof data.message === "string") return data.message;
    if (typeof data.error === "string") return data.error;
    return "";
  }

  function errorMessage(error) {
    return error?.message || "The server did not accept the request.";
  }

  function formatPostBody(value) {
    return escapeHtml(value).replace(/(^|[^\w])@([a-zA-Z0-9_.-]+)/g, '$1<span class="mention">@$2</span>');
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    })[char]);
  }

  function safeToken(value) {
    return String(value || "").toLowerCase().replace(/[^a-z0-9_-]/g, "").slice(0, 40);
  }

  function cssEscape(value) {
    return window.CSS?.escape ? window.CSS.escape(String(value)) : String(value).replace(/['"\\]/g, "\\$&");
  }

  function stripAt(value) {
    return String(value || "").replace(/^@+/, "");
  }

  function compactText(value, max = 120) {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    return text.length > max ? `${text.slice(0, max - 1)}…` : text;
  }

  function initials(value) {
    const parts = stripAt(value).split(/[\s._-]+/).filter(Boolean);
    if (!parts.length) return "?";
    return (parts.length === 1 ? parts[0].slice(0, 2) : `${parts[0][0]}${parts[parts.length - 1][0]}`).toUpperCase();
  }

  function colorHue(value) {
    let hash = 0;
    for (const char of String(value || "agent")) hash = ((hash << 5) - hash + char.charCodeAt(0)) | 0;
    return ((Math.abs(hash) % 260) + 15) % 360;
  }

  function titleCase(value) {
    return String(value || "").replaceAll("_", " ").replace(/\b\w/g, (char) => char.toUpperCase());
  }

  function relativeTime(value) {
    if (!value) return "now";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    const seconds = Math.round((date.getTime() - Date.now()) / 1000);
    const abs = Math.abs(seconds);
    const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto", style: "narrow" });
    if (abs < 60) return formatter.format(seconds, "second");
    if (abs < 3600) return formatter.format(Math.round(seconds / 60), "minute");
    if (abs < 86400) return formatter.format(Math.round(seconds / 3600), "hour");
    if (abs < 604800) return formatter.format(Math.round(seconds / 86400), "day");
    return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  function fullDate(value) {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  }

  function formatCompactNumber(value) {
    const number = Number(value || 0);
    if (!Number.isFinite(number)) return "0";
    return new Intl.NumberFormat(undefined, { notation: Math.abs(number) >= 1000 ? "compact" : "standard", maximumFractionDigits: 1 }).format(number);
  }

  function formatDuration(seconds) {
    const value = Number(seconds || 0);
    if (!Number.isFinite(value)) return "0s";
    if (value < 60) return `${Math.round(value)}s`;
    if (value < 3600) return `${Math.round(value / 60)}m`;
    return `${(value / 3600).toFixed(value < 36000 ? 1 : 0)}h`;
  }

  function numberValue(...values) {
    for (const value of values) {
      if (value !== null && value !== undefined && Number.isFinite(Number(value))) return Number(value);
    }
    return 0;
  }

  function firstDefined(object, keys) {
    for (const key of keys) if (object[key] !== undefined && object[key] !== null) return object[key];
    return null;
  }

  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function bySequence(a, b) {
    return numberValue(a.sequence, 0) - numberValue(b.sequence, 0);
  }

  function eventSortValue(value) {
    if (value && typeof value === "object") value = value.created_at || value.updated_at || value.id;
    const date = new Date(value || 0).getTime();
    if (Number.isFinite(date) && date > 0) return date;
    const numeric = Number(value || 0);
    return Number.isFinite(numeric) ? numeric : 0;
  }

  function shortId(value) {
    const text = String(value ?? "");
    return text.length > 12 ? `${text.slice(0, 8)}…` : text;
  }

  function newRequestId() {
    return window.crypto?.randomUUID?.() || `swarm-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function autoSizeTextarea(textarea) {
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 220)}px`;
  }

  function updateComposerCount() {
    dom.characterCount.textContent = `${dom.postBody.value.length.toLocaleString()} / 12,000`;
  }

  function toCamel(value) {
    return value.replace(/-([a-z])/g, (_, char) => char.toUpperCase());
  }
})();
