// Solfish web UI. The server streams every session's events over SSE; this page draws the ones for
// the open session and keeps the sidebar up to date for the rest. No build step, no framework.
"use strict";

const $ = (id) => document.getElementById(id);
const state = { current: null, busy: false, cost: 0, waiting: new Set(), sessions: [] };

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  for (const c of children) if (c != null) node.append(c);
  return node;
}

// Model output is untrusted (it reads web pages). Raw HTML in it is shown as text, never parsed;
// DOMPurify and the page's CSP are the layers behind that.
const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
if (window.marked) marked.use({ renderer: { html: (html) => escapeHtml(html.text ?? html) } });

function markdown(text) {
  const div = el("div", { className: "msg assistant" });
  if (window.marked && window.DOMPurify) div.innerHTML = DOMPurify.sanitize(marked.parse(text));
  else div.textContent = text;
  for (const a of div.querySelectorAll("a")) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
  return div;
}

function preview(value, limit = 120) {
  const s = (typeof value === "string" ? value : JSON.stringify(value)).replace(/\s+/g, " ");
  return s.length > limit ? s.slice(0, limit - 1) + "…" : s;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json" },
    body: options.body && JSON.stringify(options.body),
  });
  if (!res.ok) {
    const detail = (await res.json().catch(() => ({}))).detail || res.statusText;
    throw new Error(detail);
  }
  return res.json();
}

// ---------- the conversation ----------

const log = $("log");

function scroll() {
  log.scrollTop = log.scrollHeight;
}

function addUser(text) {
  log.append(el("div", { className: "msg user", textContent: text }));
}

function addThinking(text) {
  const first = text.split("\n")[0];
  log.append(el("details", { className: "step thinking" },
    el("summary", { textContent: "💭 " + preview(first, 100) }),
    el("div", { textContent: text })));
}

function addToolCall(id, name, input) {
  const card = el("details", { className: "step tool" },
    el("summary", {},
      el("span", { className: "name", textContent: "→ " + name }), " ",
      el("span", { className: "args", textContent: preview(input) }),
      el("span", { className: "status spinner" })),
    el("pre", { textContent: JSON.stringify(input, null, 2) }));
  card.dataset.id = id;
  log.append(card);
}

function addToolResult(id, output, isError) {
  const card = log.querySelector(`details.tool[data-id="${CSS.escape(id)}"]`);
  if (!card) return;
  card.querySelector(".status").className = "status";
  if (isError) card.classList.add("failed");
  card.append(el("pre", { textContent: output }));
}

function addApproval(a) {
  if (log.querySelector(`.approval[data-id="${CSS.escape(a.id)}"]`)) return;
  const answer = async (allow) => {
    for (const b of card.querySelectorAll("button")) b.disabled = true;
    try {
      await api(`/api/approvals/${a.id}`, { method: "POST", body: { allow } });
    } catch (e) {
      settleApproval(a.id, null, e.message);
    }
  };
  const card = el("div", { className: "approval" },
    el("strong", { textContent: `Allow ${a.name}?` }),
    el("pre", { textContent: a.input.preview ?? JSON.stringify(a.input, null, 2) }),
    el("div", { className: "actions" },
      el("button", { className: "primary", type: "button", textContent: "Allow", onclick: () => answer(true) }),
      el("button", { type: "button", textContent: "Deny", onclick: () => answer(false) })));
  card.dataset.id = a.id;
  log.append(card);
}

function settleApproval(id, allow, note) {
  const card = log.querySelector(`.approval[data-id="${CSS.escape(id)}"]`);
  if (!card) return;
  card.classList.add("done");
  card.querySelector(".actions").replaceWith(
    el("div", { textContent: note || (allow ? "✓ Allowed" : "✗ Declined") }));
}

function addError(message) {
  log.append(el("div", { className: "msg error", textContent: message }));
}

function setBusy(busy) {
  state.busy = busy;
  $("send").textContent = busy ? "Stop" : "Send";
  $("send").className = busy ? "" : "primary";
  renderStatus();
}

function renderStatus() {
  const cost = state.cost ? `$${state.cost.toFixed(4)} this session` : "";
  $("status").textContent = state.busy ? "Working…" + (cost ? " · " + cost : "") : cost;
}

// ---------- sessions ----------

async function loadSessions() {
  const [sessions, pending] = await Promise.all([api("/api/sessions"), api("/api/approvals")]);
  state.sessions = sessions;
  state.waiting = new Set(pending.map((a) => a.session));
  const open = sessions.find((s) => s.id === state.current);
  if (open) $("title").textContent = open.title || "New chat";
  renderSessions();
}

function renderSessions() {
  const nav = $("sessions");
  nav.replaceChildren(...state.sessions.map((s) => {
    const dot = el("span", { className: "dot" });
    if (state.waiting.has(s.id)) dot.classList.add("waiting");
    else if (s.busy) dot.classList.add("busy");
    const a = el("a", { href: `#${s.id}`, title: s.title || "New chat" },
      dot,
      el("span", { className: "t", textContent: s.title || "New chat" }),
      s.channel !== "web" ? el("span", { className: "channel", textContent: s.channel }) : null);
    if (s.id === state.current) a.className = "current";
    return a;
  }));
}

async function openSession(id) {
  document.body.classList.remove("sidebar-open");
  if (!id) {
    state.current = null;
    state.cost = 0;
    log.replaceChildren();
    $("title").textContent = "New chat";
    setBusy(false);
    renderSessions();
    $("input").focus();
    return;
  }
  let s;
  try {
    s = await api(`/api/sessions/${encodeURIComponent(id)}`);
  } catch (e) {
    location.hash = "";
    return;
  }
  state.current = id;
  state.cost = s.cost_usd || 0;
  log.replaceChildren();
  for (const item of s.items) {
    if (item.kind === "user") addUser(item.text);
    else if (item.kind === "assistant") log.append(markdown(item.text));
    else if (item.kind === "thinking") addThinking(item.text);
    else if (item.kind === "tool_call") addToolCall(item.id, item.name, item.input);
    else if (item.kind === "tool_result") addToolResult(item.id, item.output, item.is_error);
  }
  // A call with no result yet is still running, unless the turn is over (then it was stopped).
  if (!s.busy) for (const sp of log.querySelectorAll(".spinner")) sp.className = "status";
  for (const a of s.pending) addApproval(a);
  $("title").textContent = s.title || "New chat";
  setBusy(s.busy);
  renderSessions();
  scroll();
}

async function loadFiles() {
  const files = await api("/api/files");
  $("files").replaceChildren(...files.slice(0, 15).map((f) =>
    el("li", {}, el("a", {
      href: "/api/files/" + f.path.split("/").map(encodeURIComponent).join("/"),
      target: "_blank",
      rel: "noopener",
      textContent: f.path,
      title: `${(f.size / 1024).toFixed(1)} KB`,
    }))));
  if (!files.length) $("files").append(el("li", { className: "channel", textContent: "Nothing yet." }));
}

// ---------- live events ----------

function onEvent(ev) {
  if (ev.kind === "approval_request") state.waiting.add(ev.session);
  if (ev.kind === "approval_resolved") state.waiting.delete(ev.session);
  if (["user_message", "turn_end", "error", "approval_request", "approval_resolved"].includes(ev.kind)) {
    loadSessions();
  }
  if (ev.kind === "turn_end") loadFiles();
  if (ev.session !== state.current) return;

  switch (ev.kind) {
    case "user_message": addUser(ev.text); setBusy(true); break;
    case "thinking": addThinking(ev.text); break;
    case "tool_call": addToolCall(ev.id, ev.name, ev.input); break;
    case "tool_result": addToolResult(ev.id, ev.output, ev.is_error); break;
    case "approval_request": addApproval(ev); break;
    case "approval_resolved": settleApproval(ev.id, ev.allow); break;
    case "llm_call": state.cost += ev.cost_usd || 0; renderStatus(); break;
    case "turn_end":
      if (ev.text) log.append(markdown(ev.text));
      for (const sp of log.querySelectorAll(".spinner")) sp.className = "status";
      setBusy(false);
      break;
    case "error": addError(ev.message); setBusy(false); break;
    default: return;
  }
  scroll();
}

function connect() {
  const source = new EventSource("/api/events");
  // EventSource reconnects by itself; after a gap, redraw from the server's copy.
  source.onopen = () => { loadSessions(); if (state.current) openSession(state.current); };
  source.onmessage = (m) => onEvent(JSON.parse(m.data));
}

// ---------- composer ----------

const input = $("input");

function grow() {
  input.style.height = "auto";
  input.style.height = input.scrollHeight + "px";
}

async function submit() {
  if (state.busy) {
    await api(`/api/sessions/${state.current}/cancel`, { method: "POST" }).catch(() => {});
    return;
  }
  const text = input.value.trim();
  if (!text) return;
  try {
    if (!state.current) {
      const { id } = await api("/api/sessions", { method: "POST" });
      state.current = id;
      history.replaceState(null, "", `#${id}`);
    }
    input.value = "";
    grow();
    await api(`/api/sessions/${state.current}/messages`, { method: "POST", body: { text } });
  } catch (e) {
    input.value = input.value || text;
    addError(e.message);
  }
}

$("composer").addEventListener("submit", (e) => { e.preventDefault(); submit(); });
input.addEventListener("input", grow);
input.addEventListener("keydown", (e) => {
  // isComposing: Enter that confirms a Chinese/Japanese IME candidate must not send.
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
});
$("new-chat").addEventListener("click", () => { location.hash = ""; openSession(null); });
$("toggle-sidebar").addEventListener("click", () => document.body.classList.toggle("sidebar-open"));
window.addEventListener("hashchange", () => {
  const id = location.hash.slice(1) || null;
  if (id !== state.current) openSession(id);
});

openSession(location.hash.slice(1) || null);
loadFiles();
connect();
