"use strict";
(() => {
  const WARNING_SECONDS = 120;
  const HEARTBEAT_MS = 30000;
  const STATUS_MS = 60000;
  const CHANNEL = "swarmboard-auth-status";
  const nativeFetch = window.fetch.bind(window);
  const monotonic = () => window.performance?.now?.() ?? Date.now();
  const byId = id => document.getElementById(id);
  const loginPage = Boolean(byId("auth-login-form"));
  const notices = {
    idle_expired: "Your sign-in expired after 30 minutes of inactivity. Sign in to return.",
    absolute_expired: "Your 8-hour sign-in has ended. Sign in again to continue.",
    logged_out: "You’re signed out. Conversations keep running within their limits.",
    credentials_changed: "Your sign-in is no longer valid. Sign in again to continue.",
    reauthenticate: "Sign in again to start a new session.",
    authentication_required: "Sign in to join the board.",
  };
  let enabled = null, session = null, locked = false, clockAnchor = null;
  let checkPromise = null, heartbeatPending = false, lastHeartbeat = -Infinity, lastStatus = -Infinity;
  let warningKind = null, channel = null, statusScreen = null;

  function safeNext(value) {
    if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//") || /[\\\x00-\x20]/.test(value)) return "/";
    try {
      const parsed = new URL(value, location.origin);
      if (parsed.origin !== location.origin || parsed.pathname === "/login") return "/";
      return parsed.pathname + parsed.search + parsed.hash;
    } catch { return "/"; }
  }

  const nextPath = () => safeNext(loginPage ? new URLSearchParams(location.search).get("next") : location.pathname + location.search + location.hash);
  const serverNow = () => clockAnchor ? clockAnchor.seconds + (monotonic() - clockAnchor.monotonic) / 1000 : Date.now() / 1000;
  const visibleHumanTab = () => !document.hidden && (typeof document.hasFocus !== "function" || document.hasFocus());
  const authPath = value => {
    try {
      const url = new URL(typeof value === "string" ? value : value.url, location.href);
      return url.origin === location.origin && url.pathname.startsWith("/api/") ? url.pathname : null;
    } catch { return null; }
  };
  const errorText = data => typeof data?.detail === "string" ? data.detail : "The request could not be completed. Please try again.";

  function broadcast(message) {
    try {
      if (channel) channel.postMessage(message);
      else {
        // Only deadlines and logout state are shared; no credentials or content.
        localStorage.setItem(CHANNEL, JSON.stringify(message));
        localStorage.removeItem(CHANNEL);
      }
    } catch { /* A tab still enforces its own deadline when browser storage is unavailable. */ }
  }

  function lockPage(message = "Returning to sign in…") {
    if (loginPage) return;
    locked = true;
    document.documentElement.classList.remove("auth-checking");
    document.documentElement.classList.add("auth-locked");
    document.title = "Sign in · Swarmboard";
    document.querySelectorAll("input, textarea").forEach(input => { input.value = ""; });
    const screen = document.createElement("main");
    screen.className = "auth-lock-screen";
    const title = document.createElement("h1");
    title.textContent = message;
    screen.appendChild(title);
    document.body.replaceChildren(screen);
    window.dispatchEvent(new Event("swarmboard:auth-lock"));
    return screen;
  }

  function expire(reason = "authentication_required", share = true) {
    if (locked || enabled === false) return;
    const knownReason = Object.hasOwn(notices, reason) ? reason : "authentication_required";
    if (share) broadcast({type: "logout", reason: knownReason});
    if (loginPage) {
      byId("auth-password").value = "";
      byId("auth-login-notice").textContent = notices[knownReason];
      return;
    }
    const next = nextPath();
    lockPage();
    location.replace(`/login?next=${encodeURIComponent(next)}&reason=${encodeURIComponent(knownReason)}`);
  }

  window.fetch = async (input, options) => {
    const path = authPath(input);
    if (locked && path && !path.startsWith("/api/auth/")) throw new Error("Your sign-in has ended.");
    const response = await nativeFetch(input, options);
    if (!loginPage && enabled !== false && path && !path.startsWith("/api/auth/") && response.status === 401) {
      document.documentElement.classList.add("auth-checking");
      let reason = "authentication_required";
      try { reason = (await response.clone().json()).reason || reason; } catch { /* The status is sufficient. */ }
      expire(reason);
    }
    return response;
  };

  async function request(path, options = {}) {
    const response = await nativeFetch(path, {credentials: "same-origin", cache: "no-store", ...options});
    let data = {};
    try { data = await response.json(); } catch { /* An unavailable service need not return JSON. */ }
    if (!response.ok) {
      if (response.status === 401 && !loginPage) expire(data.reason);
      throw new Error(errorText(data));
    }
    return data;
  }

  function applySession(data, share = true) {
    if (locked) return;
    if (data.enabled === false) {
      enabled = false;
      document.documentElement.classList.remove("auth-checking");
      statusScreen?.remove(); statusScreen = null;
      if (loginPage) location.replace(nextPath());
      return;
    }
    if (data.enabled !== true) throw new Error("Unable to verify your sign-in. Please try again.");
    enabled = true;
    if (!data.authenticated) {
      if (!loginPage) expire(data.reason);
      return;
    }
    if (![data.server_time, data.idle_expires_at, data.absolute_expires_at].every(Number.isFinite)) throw new Error("Unable to verify your sign-in. Please try again.");
    if (session && session.absolute_expires_at === data.absolute_expires_at && data.server_time < session.server_time) {
      document.documentElement.classList.remove("auth-checking");
      statusScreen?.remove(); statusScreen = null;
      renderWarning();
      return;
    }
    const changed = !session || session.idle_expires_at !== data.idle_expires_at || session.absolute_expires_at !== data.absolute_expires_at;
    session = data;
    clockAnchor = {seconds: data.server_time, monotonic: monotonic()};
    document.documentElement.classList.remove("auth-checking");
    statusScreen?.remove(); statusScreen = null;
    if (share && changed) broadcast({type: "deadline", idle_expires_at: data.idle_expires_at,
      absolute_expires_at: data.absolute_expires_at, server_time: data.server_time});
    if (loginPage) { location.replace(nextPath()); return; }
    document.querySelectorAll("[data-auth-controls]").forEach(control => { control.hidden = false; });
    document.querySelectorAll("[data-auth-logout]").forEach(button => { button.title = data.user ? `Log out ${data.user}` : "Log out"; });
    renderWarning();
  }

  function verificationFailure(message) {
    if (loginPage || locked || !document.documentElement.classList.contains("auth-checking")) return;
    if (!statusScreen) {
      statusScreen = document.createElement("div");
      statusScreen.className = "auth-status-screen";
      const heading = document.createElement("h1");
      heading.textContent = "Checking sign-in";
      const messageNode = document.createElement("p");
      const retry = document.createElement("button");
      retry.className = "auth-primary"; retry.textContent = "Try again";
      retry.addEventListener("click", refreshSession);
      statusScreen.append(heading, messageNode, retry);
      document.body.appendChild(statusScreen);
    }
    statusScreen.querySelector("p").textContent = message;
  }

  function refreshSession() {
    if (locked || checkPromise) return checkPromise;
    lastStatus = monotonic();
    checkPromise = request("/api/auth/session").then(data => applySession(data)).catch(error => {
      verificationFailure(error.message);
    }).finally(() => { checkPromise = null; });
    return checkPromise;
  }

  function renderWarning() {
    if (!session || locked || loginPage || enabled === false) return;
    const now = serverNow();
    const absoluteFirst = session.absolute_expires_at <= session.idle_expires_at;
    const remaining = Math.min(session.idle_expires_at, session.absolute_expires_at) - now;
    if (remaining <= 0) { expire(absoluteFirst ? "absolute_expired" : "idle_expired"); return; }
    const warning = byId("auth-session-warning");
    if (!warning) return;
    warning.hidden = remaining > WARNING_SECONDS;
    if (warning.hidden) { warningKind = null; return; }
    const kind = absoluteFirst ? "absolute" : "idle";
    if (warningKind !== kind) {
      byId("auth-warning-message").textContent = absoluteFirst
        ? "Your 8-hour sign-in is ending. Sign in again to continue."
        : "You’ll be signed out soon due to inactivity. Keep using the board or choose Stay signed in.";
      byId("auth-warning-error").hidden = true;
      warningKind = kind;
    }
    const seconds = Math.max(1, Math.ceil(remaining));
    byId("auth-warning-countdown").textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")} remaining`;
    byId("auth-stay-signed-in").hidden = absoluteFirst;
    byId("auth-sign-in-again").hidden = !absoluteFirst;
  }

  async function heartbeat(explicit = false) {
    if (!session || enabled !== true || locked || heartbeatPending || !visibleHumanTab()) return;
    if (serverNow() >= session.absolute_expires_at || serverNow() >= session.idle_expires_at) { renderWarning(); return; }
    if (!explicit && monotonic() - lastHeartbeat < HEARTBEAT_MS) return;
    heartbeatPending = true; lastHeartbeat = monotonic();
    const button = byId("auth-stay-signed-in");
    if (button) button.disabled = true;
    try {
      const data = await request("/api/auth/activity", {method: "POST", keepalive: true, headers: {"X-Swarmboard-Activity": "1"}});
      applySession(data);
    } catch (error) {
      lastHeartbeat = -Infinity;
      const errorNode = byId("auth-warning-error");
      if (errorNode) { errorNode.textContent = error.message; errorNode.hidden = false; }
    } finally { heartbeatPending = false; if (button) button.disabled = false; }
  }

  async function logout(reason = "logged_out") {
    const next = nextPath();
    const screen = lockPage("Signing out…");
    try {
      await request("/api/auth/logout", {method: "POST"});
      broadcast({type: "logout", reason});
      location.replace(`/login?next=${encodeURIComponent(next)}&reason=${encodeURIComponent(reason)}`);
    } catch {
      if (!screen) return;
      screen.querySelector("h1").textContent = "Finish signing out";
      const explanation = document.createElement("p");
      explanation.textContent = "Your screen is cleared. Reconnect to finish signing out on the server.";
      const retry = document.createElement("button");
      retry.className = "auth-primary"; retry.textContent = "Try again";
      retry.addEventListener("click", () => logout(reason));
      screen.append(explanation, retry);
    }
  }

  function receive(message) {
    if (!message || typeof message !== "object" || locked) return;
    if (message.type === "logout") { expire(message.reason, false); return; }
    if (message.type !== "deadline" || ![message.idle_expires_at, message.absolute_expires_at, message.server_time].every(Number.isFinite)) return;
    if (!session || session.absolute_expires_at !== message.absolute_expires_at) { refreshSession(); return; }
    applySession({...session, ...message}, false);
  }

  try {
    if (typeof BroadcastChannel === "function") {
      channel = new BroadcastChannel(CHANNEL);
      channel.addEventListener("message", event => receive(event.data));
    }
  } catch { channel = null; }
  if (!channel) window.addEventListener("storage", event => {
    if (event.key !== CHANNEL || !event.newValue) return;
    try { receive(JSON.parse(event.newValue)); } catch { /* Ignore unrelated or incomplete storage data. */ }
  });

  function init() {
    if (loginPage) {
      const reason = new URLSearchParams(location.search).get("reason");
      byId("auth-login-notice").textContent = notices[reason] || notices.authentication_required;
      const form = byId("auth-login-form"), password = byId("auth-password"), button = byId("auth-login-submit"), error = byId("auth-login-error");
      form.addEventListener("submit", async event => {
        event.preventDefault();
        if (button.disabled) return;
        button.disabled = true; error.hidden = true; form.setAttribute("aria-busy", "true");
        try {
          const result = await request("/api/auth/login", {method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({username: byId("auth-username").value, password: password.value})});
          if (!result.authenticated && result.enabled !== false) throw new Error("Sign-in was not completed. Please try again.");
          applySession(result);
        } catch (failure) { error.textContent = failure.message; error.hidden = false; password.focus(); }
        finally { password.value = ""; button.disabled = false; form.setAttribute("aria-busy", "false"); }
      });
    } else {
      document.documentElement.classList.add("auth-checking");
      document.querySelectorAll("[data-auth-logout]").forEach(button => button.addEventListener("click", () => logout()));
      byId("auth-sign-in-again")?.addEventListener("click", () => logout("reauthenticate"));
      byId("auth-stay-signed-in")?.addEventListener("click", event => { if (event.isTrusted) heartbeat(true); });
      for (const name of ["pointerdown", "pointermove", "keydown", "input", "wheel", "touchstart"]) {
        document.addEventListener(name, event => {
          if (event.isTrusted && !event.target?.closest?.(".auth-warning, [data-auth-logout]")) heartbeat();
        }, {passive: true});
      }
      document.addEventListener("visibilitychange", () => {
        if (enabled === false || locked) return;
        if (document.hidden) document.documentElement.classList.add("auth-checking");
        else refreshSession();
      });
      window.addEventListener("pagehide", () => { if (enabled !== false && !locked) document.documentElement.classList.add("auth-checking"); });
      window.addEventListener("pageshow", event => { if (event.persisted && !locked) refreshSession(); });
      window.addEventListener("focus", () => { if (!document.hidden) refreshSession(); });
      window.setInterval(() => {
        renderWarning();
        if (!document.hidden && monotonic() - lastStatus >= STATUS_MS) refreshSession();
      }, 1000);
    }
    refreshSession();
  }

  window.SwarmAuth = {safeNext, get locked() { return locked; }};
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
