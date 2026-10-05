"use strict";
// Kanban board. All captured text is rendered via textContent (never innerHTML).

const state = { apps: [], statuses: [], openId: null, lastDeleted: null, toastTimer: null,
  query: "", searchIds: null, searchTimer: null };
const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const k of kids) if (k != null) e.append(k);
  return e;
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function showError(msg) {
  const e = $("err");
  e.textContent = msg || "";
  e.hidden = !msg;
}

async function load() {
  try {
    const data = await api("GET", "/api/apps");
    state.apps = data.apps;
    state.statuses = data.statuses;
    showError("");
    if (state.query) await runSearch();
  } catch (e) {
    showError("Can't reach the tracker server: " + e.message);
  }
  render();
}

function visibleApps() {
  if (!state.searchIds) return state.apps;
  return state.apps.filter((a) => state.searchIds.has(a.id));
}

async function runSearch() {
  const q = state.query.trim();
  if (!q) { state.searchIds = null; return; }
  const { ids } = await api("GET", "/api/search?q=" + encodeURIComponent(q));
  if (q === state.query.trim()) state.searchIds = new Set(ids);
}

function onSearchInput() {
  state.query = $("search").value;
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(async () => {
    try { await runSearch(); showError(""); } catch (e) { showError("Search failed: " + e.message); }
    render();
    if (state.openId) loadJdText(state.openId);
  }, 150);
}

function cardFor(app) {
  const card = el("div", { class: "card", draggable: "true", "data-id": app.id },
    el("div", { class: "co" }, app.company || "(no company)"),
    el("div", { class: "ti" }, app.title || "(no title)"),
    el("div", { class: "meta" },
      el("span", {}, app.location || ""),
      el("span", { title: "Date applied" }, app.date_applied)),
  );
  if (app.repost_of) card.append(el("span", { class: "badge", title: "Flagged as a possible repost when captured" }, "possible repost"));
  if (app.status_changed_by === "auto") card.append(el("span", { class: "badge auto", title: "Moved here automatically after 90 days in Applied, not by you" }, "auto-ghosted"));
  card.addEventListener("click", () => openPanel(app.id));
  card.addEventListener("dragstart", (ev) => {
    ev.dataTransfer.setData("text/plain", app.id);
    card.classList.add("dragging");
  });
  card.addEventListener("dragend", () => card.classList.remove("dragging"));
  return card;
}

function render() {
  const board = $("board");
  board.replaceChildren();
  const apps = visibleApps();
  $("count").textContent = `${apps.length} of ${state.apps.length} applications`;
  for (const status of state.statuses) {
    const inCol = apps.filter((a) => a.status === status)
      .sort((a, b) => (b.date_applied + b.id).localeCompare(a.date_applied + a.id));
    const cards = el("div", { class: "cards" }, ...inCol.map(cardFor));
    const col = el("section", { class: "col", "data-status": status },
      el("h2", {}, el("span", {}, status), el("span", {}, String(inCol.length))), cards);
    col.addEventListener("dragover", (ev) => { ev.preventDefault(); col.classList.add("over"); });
    col.addEventListener("dragleave", () => col.classList.remove("over"));
    col.addEventListener("drop", (ev) => {
      ev.preventDefault();
      col.classList.remove("over");
      const id = ev.dataTransfer.getData("text/plain");
      const app = state.apps.find((a) => a.id === id);
      if (app && app.status !== status) saveField(id, "status", status);
    });
    board.append(col);
  }
  if (state.openId) fillPanel(state.openId, false);
}

async function saveField(id, field, value, inputEl) {
  try {
    const { app } = await api("PATCH", `/api/apps/${id}`, { [field]: value });
    const i = state.apps.findIndex((a) => a.id === id);
    if (i >= 0) state.apps[i] = app;
    showError("");
    if (inputEl) {
      inputEl.classList.add("saved");
      setTimeout(() => inputEl.classList.remove("saved"), 700);
    }
    if (field === "status" || !inputEl) render();
    else updateCardsOnly();
  } catch (e) {
    showError("Save failed: " + e.message);
    load();
  }
}

function updateCardsOnly() {
  // Re-render the board but keep the panel form (and focus) intact.
  const open = state.openId;
  state.openId = null;
  render();
  state.openId = open;
}

const PANEL_FIELDS = [
  ["company", "Company"], ["title", "Title"], ["location", "Location"],
  ["status", "Status"], ["date_applied", "Date applied"], ["posted_date", "Posted"],
  ["source", "Source"], ["job_id", "Job ID"], ["url", "URL"], ["notes", "Notes"],
];

function openPanel(id) {
  state.openId = id;
  fillPanel(id, true);
}

function fillPanel(id, loadJd) {
  const app = state.apps.find((a) => a.id === id);
  if (!app) return closePanel();
  $("panel").hidden = false;
  $("p-heading").textContent = `${app.company} — ${app.title}`;
  const form = $("p-form");
  form.replaceChildren();
  for (const [field, label] of PANEL_FIELDS) {
    let input;
    if (field === "status") {
      input = el("select", {}, ...state.statuses.map((s) => el("option", { value: s }, s)));
      input.value = app.status;
      input.addEventListener("change", () => saveField(id, field, input.value, input));
    } else {
      input = el(field === "notes" ? "textarea" : "input", field === "date_applied" ? { type: "date" } : {});
      input.value = app[field] || "";
      input.addEventListener("change", () => saveField(id, field, input.value.trim(), input));
    }
    form.append(el("label", {}, label, input));
  }
  if (app.repost_of) {
    const orig = state.apps.find((a) => a.id === app.repost_of);
    form.append(el("p", { class: "badge" }, "Possible repost of: " +
      (orig ? `${orig.company} — ${orig.title} (${orig.date_applied})` : app.repost_of + " (deleted)")));
  }
  const link = $("p-link");
  if (/^https?:\/\//.test(app.url)) { link.href = app.url; link.hidden = false; } else link.hidden = true;
  if (loadJd) loadJdText(id);
}

async function loadJdText(id) {
  const pre = $("p-jd");
  pre.textContent = "Loading…";
  try {
    const { text } = await api("GET", `/api/apps/${id}/jd`);
    if (state.openId !== id) return;
    renderJd(text || "(no job description saved)");
  } catch (e) {
    pre.textContent = "Could not load JD: " + e.message;
  }
}

// Highlight search terms using text nodes + <mark>, never innerHTML.
function renderJd(text) {
  const pre = $("p-jd");
  const terms = state.query.trim().split(/\s+/).filter(Boolean)
    .map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!terms.length) { pre.textContent = text; return; }
  const re = new RegExp(`(${terms.join("|")})`, "gi");
  pre.replaceChildren(...text.split(re).map((part, i) => (i % 2 ? el("mark", {}, part) : document.createTextNode(part))));
  const first = pre.querySelector("mark");
  if (first) first.scrollIntoView({ block: "center" });
}

function closePanel() {
  state.openId = null;
  $("panel").hidden = true;
}

async function deleteOpen() {
  const id = state.openId;
  if (!id) return;
  try {
    const { app } = await api("DELETE", `/api/apps/${id}`);
    state.apps = state.apps.filter((a) => a.id !== id);
    state.lastDeleted = app;
    closePanel();
    render();
    showToast(`Deleted ${app.company} — ${app.title}`);
  } catch (e) {
    showError("Delete failed: " + e.message);
  }
}

function showToast(msg) {
  $("toast-msg").textContent = msg;
  $("toast").hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => { $("toast").hidden = true; state.lastDeleted = null; }, 10000);
}

async function undoDelete() {
  const row = state.lastDeleted;
  if (!row) return;
  $("toast").hidden = true;
  state.lastDeleted = null;
  try {
    await api("POST", "/api/restore", row);
    await load();
  } catch (e) {
    showError("Undo failed: " + e.message);
  }
}

$("search").addEventListener("input", onSearchInput);
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) { e.preventDefault(); $("search").focus(); }
});
$("p-close").addEventListener("click", closePanel);
$("p-delete").addEventListener("click", deleteOpen);
$("toast-undo").addEventListener("click", undoDelete);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePanel(); });
// Pick up captures made from the extension while the board is open.
window.addEventListener("focus", load);
load();
