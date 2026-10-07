"use strict";
// Popup: extract from the active tab, show results with confidence flags,
// send to the local app. If the app is down, queue in localStorage and retry.

const SERVER = "http://127.0.0.1:8765";
const QUEUE_KEY = "jobtracker.queue";
const DECIDE_KEY = "jobtracker.needsDecision";
const DRAFTS_KEY = "jobtracker.drafts"; // unsaved popup edits, per page
const DRAFT_TTL_MS = 7 * 24 * 3600 * 1000;
let pageKey = "";        // the tab's URL (no #hash): which page a draft belongs to
let extracted = null;    // what the page gave us, for Reset // queued captures the app flagged as possible reposts
const FIELDS = ["company", "title", "location", "url", "source", "job_id", "posted_date", "jd_text"];
const REQUIRED = new Set(["company", "title", "url", "jd_text"]);
const $ = (id) => document.getElementById(id);
const form = $("form");

function setStatus(msg, kind = "") {
  const s = $("status");
  s.textContent = msg;
  s.className = "status " + kind;
}

// ---------- queue (localStorage on the extension origin; no extra permission) ----------

function readQueue() {
  try { return JSON.parse(localStorage.getItem(QUEUE_KEY) || "[]"); } catch (e) { return []; }
}
function writeQueue(q) {
  localStorage.setItem(QUEUE_KEY, JSON.stringify(q));
  const box = $("queue");
  box.hidden = q.length === 0;
  box.textContent = q.length ? `${q.length} capture(s) waiting for the app. They are retried every time this popup opens.` : "";
}
function enqueue(payload) {
  const q = readQueue();
  q.push({ ...payload, queued_at: new Date().toISOString() });
  writeQueue(q);
}

// Returns {ok, data} on an HTTP response, or {ok:false, offline:true} if unreachable.
async function post(payload) {
  try {
    const res = await fetch(`${SERVER}/api/capture`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (res.status >= 500) return { ok: false, offline: true, data };
    return { ok: res.ok, data, status: res.status };
  } catch (e) {
    return { ok: false, offline: true };
  }
}

function readDecisions() {
  try { return JSON.parse(localStorage.getItem(DECIDE_KEY) || "[]"); } catch (e) { return []; }
}
function writeDecisions(d) { localStorage.setItem(DECIDE_KEY, JSON.stringify(d)); }

async function flushQueue() {
  const q = readQueue();
  const remaining = [];
  const decisions = readDecisions();
  let sent = 0;
  for (const item of q) {
    const r = await post(item);
    if (r.offline || !r.ok) { remaining.push(item); continue; } // never drop a capture silently
    if (r.data.result === "possible_repost") decisions.push({ payload: item, matches: r.data.matches });
    else sent++;
  }
  writeQueue(remaining);
  writeDecisions(decisions);
  return sent;
}

// Show "possible repost of ..." and resolve with true (add anyway) or false (skip).
function askRepost(payload, matches, queued) {
  const box = $("repost");
  const list = $("repost-list");
  list.replaceChildren();
  box.querySelector("strong").textContent = (queued ? `Queued capture "${payload.company} — ${payload.title}" ` : "") + "Possible repost of:";
  for (const m of matches) {
    const li = document.createElement("li");
    li.textContent = `${m.company} — ${m.title} (applied ${m.date_applied}, ${m.status}; ${m.reason})`;
    list.append(li);
  }
  box.hidden = false;
  return new Promise((resolve) => {
    const done = (v) => { box.hidden = true; $("add-anyway").onclick = $("skip").onclick = null; resolve(v); };
    $("add-anyway").onclick = () => done(true);
    $("skip").onclick = () => done(false);
  });
}

async function resolveQueuedDecisions() {
  let d = readDecisions();
  while (d.length) {
    const { payload, matches } = d[0];
    const add = await askRepost(payload, matches, true);
    if (add) {
      const r = await post({ ...payload, force: true });
      if (r.offline || !r.ok) { setStatus("Couldn't reach the app; will ask again next time.", "warn"); return; }
    }
    d = readDecisions().slice(1);
    writeDecisions(d);
  }
}

// ---------- drafts: keep your edits when the popup closes ----------
// The popup is destroyed every time it loses focus, so edits are written to
// localStorage as you type and put back the next time it opens on that page.

function readDrafts() {
  try { return JSON.parse(localStorage.getItem(DRAFTS_KEY) || "{}"); } catch (e) { return {}; }
}
function writeDrafts(d) {
  try { localStorage.setItem(DRAFTS_KEY, JSON.stringify(d)); } catch (e) { /* storage full or blocked */ }
}
function pruneDrafts() {
  const d = readDrafts();
  const now = Date.now();
  for (const k of Object.keys(d)) if (!d[k] || now - d[k].saved_at > DRAFT_TTL_MS) delete d[k];
  writeDrafts(d);
}
function saveDraft(name) {
  if (!pageKey) return;
  const d = readDrafts();
  const draft = d[pageKey] || { values: {}, saved_at: 0 };
  draft.values[name] = form.elements[name].value;
  draft.saved_at = Date.now();
  d[pageKey] = draft;
  writeDrafts(d);
}
function clearDraft() {
  if (!pageKey) return;
  const d = readDrafts();
  delete d[pageKey];
  writeDrafts(d);
}
// Put saved edits back over the freshly extracted values. Returns how many.
function restoreDraft() {
  const draft = readDrafts()[pageKey];
  if (!draft) return 0;
  let n = 0;
  for (const [name, value] of Object.entries(draft.values)) {
    const input = form.elements[name];
    if (!input || !FIELDS.includes(name)) continue;
    input.value = value;
    markEdited(name);
    n++;
  }
  updateJdLen();
  return n;
}
function markEdited(name) {
  const label = form.querySelector(`label[data-f="${name}"]`);
  label.classList.remove("low", "empty");
  label.querySelector(".why")?.remove();
}

// ---------- extraction ----------

async function extractFromTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !/^https?:/.test(tab.url || "")) throw new Error("Open a job posting, then click the extension. (This tab isn't a web page.)");
  if (/^https?:\/\/(localhost|127\.0\.0\.1):8765\//.test(tab.url)) throw new Error("This is the tracker board. Open a job posting to capture it.");
  pageKey = tab.url.split("#")[0];
  const run = async (allFrames) => {
    const target = { tabId: tab.id, allFrames };
    await chrome.scripting.executeScript({ target, files: ["extract.js"] });
    return chrome.scripting.executeScript({
      target,
      func: () => ({ fields: JobExtract.extract(document, location.href), top: window === window.top }),
    });
  };
  let results;
  try {
    try { results = await run(true); } catch (e) { results = await run(false); }
  } catch (e) {
    if (/error page/i.test(e.message)) throw new Error("This tab is showing a browser error page (site didn't load). Reload it, then try again.");
    throw e;
  }
  results = results.map((r) => r.result).filter(Boolean);
  const top = results.find((r) => r.top) || results[0];
  if (!top) throw new Error("Nothing could be read from this page.");
  // Some ATSs (iCIMS, embedded Greenhouse) render the posting in an iframe.
  // Use whichever frame extracted the most, but keep the top page's URL.
  let best = top;
  for (const r of results) {
    const s = scoreOf(r.fields);
    if (s > scoreOf(best.fields)) best = r;
  }
  const fields = { ...best.fields };
  if (best !== top) {
    fields.url = top.fields.url;
    if (!fields.job_id.value && top.fields.job_id.value) fields.job_id = top.fields.job_id;
  }
  return fields;
}

function scoreOf(f) {
  let s = 0;
  for (const k of ["title", "company", "location", "job_id"]) {
    const c = f[k] && f[k].confidence;
    s += c === "high" ? 3 : c === "medium" ? 2 : c === "low" ? 1 : 0;
  }
  return s + Math.min(5, ((f.jd_text && f.jd_text.value) || "").length / 500);
}

function showFields(fields) {
  for (const name of FIELDS) {
    const f = fields[name] || { value: "", confidence: "" };
    const input = form.elements[name];
    input.value = f.value || "";
    const label = form.querySelector(`label[data-f="${name}"]`);
    label.classList.remove("low", "empty");
    label.querySelector(".why")?.remove();
    let why = "";
    if (!f.value) {
      if (REQUIRED.has(name) || name === "location") { label.classList.add("empty"); why = "not found"; }
    } else if (f.confidence === "low" || f.confidence === "medium") {
      label.classList.add("low");
      why = `guessed from ${f.source}`;
    }
    if (why) {
      const span = document.createElement("span");
      span.className = "why";
      span.textContent = why;
      label.insertBefore(span, input);
    }
  }
  updateJdLen();
  form.hidden = false;
}

function updateJdLen() {
  $("jd-len").textContent = `(${form.elements.jd_text.value.length.toLocaleString()} chars)`;
}

function payloadFromForm() {
  const p = {};
  for (const name of FIELDS) p[name] = form.elements[name].value.trim();
  // Stamp the capture date now, so a queued capture keeps the day you applied.
  const d = new Date();
  p.date_applied = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  return p;
}

// ---------- save ----------

async function save(payload) {
  $("save").disabled = true;
  let r = await post(payload);
  if (r.offline && (await ensureApp())) r = await post(payload);
  if (r.offline) {
    enqueue(payload);
    clearDraft();
    setStatus("Couldn't reach or start the app. Capture saved locally; it is sent the next time this popup opens and the app is running.", "warn");
    return;
  }
  if (!r.ok) {
    setStatus("Rejected by app: " + (r.data.error || r.status), "bad");
    $("save").disabled = false;
    return;
  }
  handleResult(r.data, payload);
}

async function handleResult(data, payload) {
  if (data.result === "added") {
    const flag = data.app.repost_of ? " (flagged as possible repost)" : "";
    clearDraft();
    setStatus(`Logged: ${data.app.company} — ${data.app.title}${flag}`, "ok");
    form.hidden = true;
    return;
  }
  if (data.result === "possible_repost") {
    form.hidden = true;
    setStatus("This looks like something you already logged.", "warn");
    if (await askRepost(payload, data.matches, false)) {
      save({ ...payload, force: true });
    } else {
      clearDraft();
      setStatus("Skipped. Nothing was logged.", "");
    }
  }
}

// One listener for all fields: clear the flag and save the draft on every keystroke.
form.addEventListener("input", (e) => {
  const name = e.target.name;
  if (!FIELDS.includes(name)) return;
  markEdited(name);
  saveDraft(name);
});

$("reset").addEventListener("click", (e) => {
  e.preventDefault();
  clearDraft();
  if (extracted) showFields(extracted);
  setStatus("Edits discarded; showing what the page gave.", "");
});

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const p = payloadFromForm();
  if (!p.company && !p.title) { setStatus("Need at least a company or a title.", "bad"); return; }
  save(p);
});
form.elements.jd_text.addEventListener("input", updateJdLen);

// ---------- app lifecycle (start via native host, stop via HTTP) ----------

const HOST = "com.jobtracker.launcher";

async function appRunning() {
  try {
    const res = await fetch(`${SERVER}/api/health`, { cache: "no-store" });
    return res.ok;
  } catch (e) { return false; }
}

function setAppState(running, note) {
  $("app-dot").className = "dot " + (running ? "on" : "off");
  $("app-label").textContent = note || (running ? "App running" : "App stopped");
  $("stop-app").hidden = !running;
  $("start-app").hidden = running;
}

// Ask the native host to start the app. Resolves to {ok, error?}.
function startApp() {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendNativeMessage(HOST, { cmd: "start" }, (reply) => {
        const err = chrome.runtime.lastError;
        if (err) {
          const raw = err.message || String(err);
          let hint = "";
          if (/not found/i.test(raw)) hint = "Brave can't find the launcher registration. Run install-launcher.bat, then fully quit and reopen Brave.";
          else if (/forbidden/i.test(raw)) hint = `The launcher doesn't allow this extension's ID (${chrome.runtime.id}). Run install-launcher.bat again.`;
          else if (/exited|failed to start|communicating/i.test(raw)) hint = "The launcher started but crashed. Run install-launcher.bat; its self-test prints the error.";
          resolve({ ok: false, error: `${hint ? hint + " " : ""}[${raw}]` });
        } else resolve(reply || { ok: false, error: "No reply from launcher." });
      });
    } catch (e) {
      resolve({ ok: false, error: e.message });
    }
  });
}

async function ensureApp() {
  if (await appRunning()) { setAppState(true); return true; }
  setAppState(false, "Starting app…");
  const r = await startApp();
  const up = r.ok && (await appRunning());
  setAppState(up, up ? "App started" : "App not running");
  if (!up) $("app-error").textContent = r.error || "App did not start.";
  $("app-error").hidden = up;
  return up;
}

$("start-app").addEventListener("click", ensureApp);
$("stop-app").addEventListener("click", async () => {
  try { await fetch(`${SERVER}/api/shutdown`, { method: "POST" }); } catch (e) { /* already down */ }
  setTimeout(async () => setAppState(await appRunning()), 400);
});
$("open-board").addEventListener("click", async () => {
  if (await ensureApp()) chrome.tabs.create({ url: "http://localhost:8765/" });
});

(async function init() {
  pruneDrafts();
  writeQueue(readQueue());
  // Start extraction right away; it doesn't need the app.
  const extraction = extractFromTab().then((f) => ({ f }), (e) => ({ e }));
  const up = await ensureApp();
  const sent = up ? await flushQueue() : 0;
  if (up) await resolveQueuedDecisions();
  const { f: fields, e: err } = await extraction;
  if (err) {
    setStatus(err.message, "bad");
  } else {
    extracted = fields;
    showFields(fields);
    const restored = restoreDraft();
    const flagged = form.querySelectorAll("label.low, label.empty").length;
    if (restored) setStatus(`Your edits are back (${restored} field${restored > 1 ? "s" : ""}).` + (flagged ? ` ${flagged} field(s) still flagged.` : ""), flagged ? "warn" : "ok");
    else setStatus(flagged ? `Check the ${flagged} highlighted field(s), then log.` : "Looks good. Log it?", flagged ? "warn" : "ok");
  }
  if (sent) setStatus(`${$("status").textContent}  (Sent ${sent} queued capture(s).)`, $("status").className.replace("status ", ""));
})();
