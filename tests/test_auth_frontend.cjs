const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../swarmboard/static/auth.js"), "utf8");
const settle = () => new Promise(resolve => setImmediate(resolve));
const response = (status, data) => ({status, ok: status >= 200 && status < 300,
  json: async () => data, clone: () => ({json: async () => data})});

function broadcastHub() {
  const channels = new Set(), messages = [];
  return {messages, Channel: class {
    constructor() { this.listeners = []; channels.add(this); }
    addEventListener(name, callback) { this.listeners.push(callback); }
    postMessage(message) {
      messages.push(message);
      for (const other of channels) if (other !== this) other.listeners.forEach(fn => fn({data: message}));
    }
  }};
}

function browser({login = false, search = "", session = {}, handler, hub} = {}) {
  let milliseconds = 0, cleared = false;
  const requests = [], redirects = [], shared = [], intervals = [], windowListeners = {}, documentListeners = {};
  const nodes = new Map();
  const classes = new Set();
  function node(name) {
    if (nodes.has(name)) return nodes.get(name);
    const value = {name, listeners: {}, children: [], hidden: true, disabled: false, value: "", textContent: "", innerHTML: "", className: "",
      addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); },
      setAttribute() {}, focus() { this.focused = true; }, closest() { return null; },
      append(...children) { this.children.push(...children); }, appendChild(child) { this.children.push(child); },
      querySelector(tag) { return this.children.find(child => child.name.startsWith(tag)); },
      remove() { this.removed = true; },
    };
    nodes.set(name, value);
    return value;
  }
  const names = ["auth-session-warning", "auth-warning-message", "auth-warning-countdown", "auth-warning-error",
    "auth-stay-signed-in", "auth-sign-in-again", "auth-controls", "auth-logout", "draft"];
  if (login) names.push("auth-login-form", "auth-username", "auth-password", "auth-login-submit", "auth-login-error", "auth-login-notice");
  names.forEach(node);
  const body = node("body");
  body.children = names.map(node);
  body.replaceChildren = (...children) => { body.children = children; cleared = true; };
  let created = 0;
  const info = {enabled: true, authenticated: !login, user: login ? null : "collaborator",
    idle_expires_at: 2800, absolute_expires_at: 29800, ...session};
  const data = () => ({...info, server_time: 1000 + milliseconds / 1000});
  const context = {
    URL, URLSearchParams, Object, Number, Date, JSON, Math, String,
    Event: class {constructor(type) { this.type = type; }},
    location: {origin: "https://board.test", href: `https://board.test${login ? "/login" : "/sessions"}${search}`,
      pathname: login ? "/login" : "/sessions", search, hash: "", replace(url) { redirects.push(url); }},
    performance: {now: () => milliseconds},
    document: {readyState: "interactive", hidden: false, focused: true, hasFocus() { return this.focused; },
      title: "Sensitive conversation", body,
      documentElement: {classList: {add(name) { classes.add(name); }, remove(name) { classes.delete(name); }, contains(name) { return classes.has(name); }}},
      getElementById: id => cleared ? null : nodes.get(id) || null,
      createElement: tag => node(tag + ++created),
      querySelectorAll(selector) {
        if (cleared) return [];
        return selector === "[data-auth-controls]" ? [node("auth-controls")] : selector === "[data-auth-logout]" ? [node("auth-logout")]
          : selector === "input, textarea" ? [node("draft"), ...(login ? [node("auth-password")] : [])] : [];
      },
      addEventListener(name, callback) { (documentListeners[name] ||= []).push(callback); },
    },
    addEventListener(name, callback) { (windowListeners[name] ||= []).push(callback); },
    dispatchEvent(event) { (windowListeners[event.type] || []).forEach(callback => callback(event)); },
    setInterval(callback) { intervals.push(callback); },
    localStorage: {setItem(key, value) { shared.push({key, value}); }, removeItem() {}},
    fetch: async (url, options = {}) => {
      const request = {url, ...options}; requests.push(request);
      if (handler) {
        const custom = await handler(request, data(), info);
        if (custom) return custom;
      }
      if (url === "/api/auth/activity") info.idle_expires_at = 1000 + milliseconds / 1000 + 1800;
      if (url === "/api/auth/logout") info.authenticated = false;
      return response(200, data());
    },
  };
  context.window = context;
  if (hub) context.BroadcastChannel = hub.Channel;
  vm.runInNewContext(source, context);
  return {context, node, requests, redirects, shared, body, classes, info,
    async event(name, values = {}, window = false) {
      for (const callback of (window ? windowListeners : documentListeners)[name] || []) callback({type: name, ...values});
      await settle();
    },
    async click(id, values = {isTrusted: true}) {
      for (const callback of node(id).listeners.click || []) callback(values);
      await settle();
    },
    async submit() {
      for (const callback of node("auth-login-form").listeners.submit || []) callback({preventDefault() {}});
      await settle();
    },
    async advance(ms) { milliseconds += ms; intervals.forEach(callback => callback()); await settle(); },
  };
}

test("login sends credentials only in the POST body and redirects only after successful authentication", async () => {
  let succeeds = false;
  const b = browser({login: true, search: "?next=%2Fsessions%3Frun%3Dselected", handler(request, snapshot) {
    if (request.url !== "/api/auth/login") return;
    return succeeds ? response(200, {...snapshot, authenticated: true, user: "collaborator"})
      : response(401, {detail: '<img src="https://observer.invalid/pixel"> Invalid sign-in'});
  }});
  await settle();
  b.node("auth-username").value = "collaborator";
  b.node("auth-password").value = "planted-login-secret";
  await b.submit();
  assert.equal(b.redirects.length, 0);
  assert.equal(b.node("auth-password").value, "");
  assert.match(b.node("auth-login-error").textContent, /<img/);
  assert.equal(b.node("auth-login-error").innerHTML, "");
  assert.equal(b.node("auth-login-submit").disabled, false);
  const sent = b.requests.find(request => request.url === "/api/auth/login");
  assert.equal(sent.method, "POST");
  assert.equal(sent.credentials, "same-origin");
  assert.deepEqual(JSON.parse(sent.body), {username: "collaborator", password: "planted-login-secret"});
  succeeds = true;
  b.node("auth-password").value = "planted-login-secret";
  await b.submit();
  assert.deepEqual(b.redirects, ["/sessions?run=selected"]);
  assert.doesNotMatch(JSON.stringify(b.shared), /planted-login-secret|collaborator/);
  assert.doesNotMatch(b.requests.map(request => request.url).join(" "), /planted-login-secret/);
});

test("login next redirects accept only local paths and avoid sign-in loops", async () => {
  const b = browser({login: true}); await settle();
  for (const unsafe of ["//observer.invalid", "https://observer.invalid", "javascript:alert(1)", "/\\observer.invalid", "/login?next=/", "/\n/observer.invalid"]) {
    assert.equal(b.context.SwarmAuth.safeNext(unsafe), "/", unsafe);
  }
  assert.equal(b.context.SwarmAuth.safeNext("/#thread=thread&post=post"), "/#thread=thread&post=post");
});

test("idle warnings preserve drafts and trusted typing refreshes the deadline", async () => {
  const b = browser(); await settle();
  b.node("draft").value = "Unsubmitted private thought";
  await b.advance(1681000);
  assert.equal(b.node("auth-session-warning").hidden, false);
  assert.equal(b.node("auth-stay-signed-in").hidden, false);
  assert.equal(b.node("auth-sign-in-again").hidden, true);
  assert.equal(b.node("draft").value, "Unsubmitted private thought");
  assert.match(b.node("auth-warning-countdown").textContent, /1:59/);
  await b.event("keydown", {isTrusted: true});
  assert.equal(b.node("auth-session-warning").hidden, true);
  assert.equal(b.node("draft").value, "Unsubmitted private thought");
  const activity = b.requests.filter(request => request.url === "/api/auth/activity");
  assert.equal(activity.length, 1);
  assert.equal(activity[0].headers["X-Swarmboard-Activity"], "1");
  assert.equal(b.info.absolute_expires_at, 29800);
  assert.equal(b.redirects.length, 0);
});

test("polling, visibility, focus, synthetic events and background input never count as activity", async () => {
  const b = browser(); await settle();
  await b.advance(61000);
  await b.event("focus", {}, true);
  await b.event("input", {isTrusted: false});
  b.context.document.hidden = true;
  await b.event("visibilitychange");
  await b.event("pointerdown", {isTrusted: true});
  b.context.document.hidden = false;
  await b.event("visibilitychange");
  b.context.document.focused = false;
  await b.event("keydown", {isTrusted: true});
  assert.equal(b.requests.filter(request => request.url === "/api/auth/activity").length, 0);
  b.context.document.focused = true;
  await b.event("pointermove", {isTrusted: true});
  await b.event("keydown", {isTrusted: true});
  assert.equal(b.requests.filter(request => request.url === "/api/auth/activity").length, 1);
  await b.advance(30001);
  await b.event("keydown", {isTrusted: true});
  assert.equal(b.requests.filter(request => request.url === "/api/auth/activity").length, 2);
});

test("absolute warnings offer a fresh sign-in by logging out before redirecting", async () => {
  const b = browser({session: {absolute_expires_at: 1119}}); await settle();
  b.node("draft").value = "Sensitive draft";
  assert.equal(b.node("auth-stay-signed-in").hidden, true);
  assert.equal(b.node("auth-sign-in-again").hidden, false);
  assert.match(b.node("auth-warning-message").textContent, /8-hour/);
  await b.click("auth-sign-in-again");
  assert.equal(b.requests.filter(request => request.url === "/api/auth/logout" && request.method === "POST").length, 1);
  assert.match(b.redirects[0], /^\/login\?.*reason=reauthenticate/);
  assert.equal(b.node("draft").value, "");
  assert.equal(b.context.SwarmAuth.locked, true);
});

test("expiry clears the page and late successful status responses cannot reopen it", async () => {
  let checks = 0, release;
  const b = browser({session: {idle_expires_at: 1001}, handler(request, snapshot) {
    if (request.url === "/api/auth/session" && ++checks === 2) return new Promise(resolve => { release = () => resolve(response(200, {...snapshot, idle_expires_at: 9000})); });
  }});
  await settle();
  b.node("draft").value = "Sensitive draft";
  await b.event("focus", {}, true);
  await b.advance(2000);
  assert.equal(b.context.SwarmAuth.locked, true);
  assert.equal(b.node("draft").value, "");
  assert.equal(b.body.children.length, 1);
  assert.equal(b.body.children[0].className, "auth-lock-screen");
  assert.match(b.redirects[0], /reason=idle_expired/);
  release(); await settle();
  assert.equal(b.classes.has("auth-locked"), true);
  assert.equal(b.node("auth-controls").hidden, false); // Detached nodes are never reattached.
  assert.equal(b.body.children.length, 1);
  assert.equal(b.redirects.length, 1);
  const before = b.requests.length;
  await assert.rejects(b.context.fetch("/api/state"), /sign-in has ended/);
  assert.equal(b.requests.length, before);
});

test("an unauthorized application fetch immediately hides content and redirects with the reason", async () => {
  let release;
  const b = browser({handler(request) {
    if (request.url === "/api/state") return {status: 401, ok: false,
      clone: () => ({json: () => new Promise(resolve => { release = () => resolve({reason: "credentials_changed"}); })})};
  }});
  await settle();
  const pending = b.context.fetch("/api/state"); await settle();
  assert.equal(b.classes.has("auth-checking"), true);
  release(); await pending;
  assert.equal(b.classes.has("auth-locked"), true);
  assert.match(b.redirects[0], /reason=credentials_changed/);
});

test("tabs share updated deadlines and logout without sharing credentials or discussion data", async () => {
  const hub = broadcastHub();
  const a = browser({hub, session: {idle_expires_at: 1119}}); await settle();
  const b = browser({hub, session: {idle_expires_at: 1119}}); await settle();
  assert.equal(b.node("auth-session-warning").hidden, false);
  await a.event("pointerdown", {isTrusted: true});
  assert.equal(b.node("auth-session-warning").hidden, true);
  b.node("draft").value = "Private second-tab draft";
  await a.click("auth-logout");
  assert.equal(b.context.SwarmAuth.locked, true);
  assert.equal(b.node("draft").value, "");
  assert.match(b.redirects[0], /reason=logged_out/);
  assert.ok(hub.messages.every(message => Object.keys(message).every(key => ["type", "reason", "idle_expires_at", "absolute_expires_at", "server_time"].includes(key))));
});

test("failed logout keeps sensitive content cleared and offers a server logout retry", async () => {
  let failures = 1;
  const b = browser({handler(request) {
    if (request.url === "/api/auth/logout" && failures-- > 0) throw new Error("offline");
  }});
  await settle();
  b.node("draft").value = "Private draft";
  await b.click("auth-logout");
  assert.equal(b.context.SwarmAuth.locked, true);
  assert.equal(b.node("draft").value, "");
  assert.equal(b.redirects.length, 0);
  const screen = b.body.children[0];
  assert.equal(screen.querySelector("h1").textContent, "Finish signing out");
  screen.querySelector("button").listeners.click[0](); await settle();
  assert.match(b.redirects[0], /reason=logged_out/);
});

test("storage-event fallback synchronizes logout with no persistent credential data", async () => {
  const b = browser(); await settle();
  assert.ok(b.shared.length > 0);
  assert.doesNotMatch(JSON.stringify(b.shared), /password|cookie|collaborator/);
  await b.event("storage", {key: "swarmboard-auth-status", newValue: JSON.stringify({type: "logout", reason: "logged_out"})}, true);
  assert.equal(b.context.SwarmAuth.locked, true);
});
