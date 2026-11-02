"use strict";
// Leads page: the night-hunt agent's CSV. Rendered via textContent only.

const $ = (id) => document.getElementById(id);
const state = { leads: [], filter: "all", query: "" };
const FLAG_LABEL = { bad_link: "No job link", duplicate: "Duplicate", tracked: "In tracker", draft_ready: "Draft ready" };
const FILTERS = [
  ["all", "All", () => true],
  ["todo", "Not applied", (l) => (l.application_filled || "").toLowerCase() !== "yes"],
  ["applied", "Applied", (l) => (l.application_filled || "").toLowerCase() === "yes"],
  ["draft_ready", "Draft ready", (l) => has(l, "draft_ready")],
  ["contact", "Has contact", (l) => !!l.contact_name],
  ["replied", "Replied", (l) => !["", "none"].includes((l.reply_status || "").toLowerCase())],
  ["bad_link", "No job link", (l) => has(l, "bad_link")],
  ["duplicate", "Duplicates", (l) => has(l, "duplicate")],
  ["tracked", "In tracker", (l) => has(l, "tracked")],
];
const has = (l, kind) => l.flags.some((f) => f.kind === kind);

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const k of kids) if (k != null && k !== false) e.append(k);
  return e;
}

async function api(method, path, body) {
  const res = await fetch(path, { method, headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function showError(msg) { $("err").textContent = msg || ""; $("err").hidden = !msg; }
let toastTimer;
function toast(msg) {
  $("toast-msg").textContent = msg; $("toast").hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { $("toast").hidden = true; }, 3500);
}

async function load() {
  try {
    const data = await api("GET", "/api/leads");
    showError("");
    if (!data.found) {
      state.leads = [];
      $("src").textContent = "";
      $("leads").replaceChildren(el("div", { class: "empty" },
        el("strong", {}, "No agent CSV yet."), el("br"),
        "Drop the night-hunt CSV at:", el("br"), el("code", {}, data.path), el("br"),
        "then reload this page. Replace it after every run; edits you make here are kept."));
      $("count").textContent = "0 leads";
      return;
    }
    state.leads = data.leads.slice().reverse(); // newest night first
    $("src").textContent = "CSV updated " + data.modified.replace("T", " ");
    $("src").title = data.path;
  } catch (e) {
    showError("Can't load leads: " + e.message);
  }
  render();
}

function visible() {
  const f = FILTERS.find((x) => x[0] === state.filter)[2];
  const q = state.query.trim().toLowerCase();
  return state.leads.filter((l) => f(l) && (!q ||
    [l.company, l.title, l.location, l.contact_name, l.notes].join(" ").toLowerCase().includes(q)));
}

function renderFilters() {
  $("filters").replaceChildren(...FILTERS.map(([id, label, fn]) =>
    el("button", { class: "chip" + (state.filter === id ? " on" : ""),
      onclick: () => { state.filter = id; render(); } },
      label, el("span", {}, String(state.leads.filter(fn).length)))));
}

function select(lead, field, options) {
  const s = el("select", { onchange: () => save(lead, field, s.value, s) });
  const cur = lead[field] || "";
  const opts = options.includes(cur) ? options : [cur, ...options];
  for (const o of opts) s.append(el("option", o === cur ? { value: o, selected: "" } : { value: o }, o || "—"));
  return s;
}

function input(lead, field, attrs = {}) {
  const i = el("input", { value: lead[field] || "", ...attrs });
  i.addEventListener("change", () => save(lead, field, i.value, i));
  return i;
}

async function save(lead, field, value, inputEl) {
  try {
    const { lead: updated } = await api("PATCH", `/api/leads/${lead.key}`, { [field]: value });
    Object.assign(lead, updated);
    if (inputEl) { inputEl.classList.add("saved"); setTimeout(() => inputEl.classList.remove("saved"), 900); }
    showError("");
    if (["application_filled", "draft_created", "reply_status", "job_link"].includes(field)) load();
  } catch (e) { showError("Save failed: " + e.message); }
}

async function track(lead, force) {
  try {
    const res = await api("POST", `/api/leads/${lead.key}/track`, force ? { force: true } : {});
    if (res.result === "possible_repost") {
      const m = res.matches[0];
      if (confirm(`Looks like it's already on the board:\n${m.company} — ${m.title} (${m.status}, ${m.date_applied}, ${m.reason}).\n\nAdd anyway?`))
        return track(lead, true);
      return;
    }
    toast(`Added to board as Applied: ${lead.company}`);
    load();
  } catch (e) { showError("Couldn't add to board: " + e.message); }
}

function leadRow(l) {
  const applied = (l.application_filled || "").toLowerCase() === "yes";
  const cls = ["lead"];
  if (applied) cls.push("is-applied");
  for (const f of l.flags) cls.push("f-" + f.kind);
  const linkOk = !has(l, "bad_link");
  const flags = el("div", { class: "flags" }, ...l.flags.map((f) =>
    el("span", { class: "flag " + f.kind, title: f.text }, FLAG_LABEL[f.kind] + " · " + f.text)));

  const contact = l.contact_name
    ? el("div", { class: "contact" },
        el("strong", {}, l.contact_name), l.contact_school ? " · " + l.contact_school : "",
        l.contact_linkedin_url ? el("a", { href: l.contact_linkedin_url, target: "_blank", rel: "noopener noreferrer" }, " LinkedIn ↗") : "",
        l.contact_email_or_blank ? el("span", {}, " · " + l.contact_email_or_blank) : "",
        l.draft_subject ? el("div", { class: "subj" }, "Draft: " + l.draft_subject) : "")
    : el("div", { class: "contact none" }, "No contact");

  return el("article", { class: cls.join(" ") },
    el("div", { class: "lead-main" },
      el("div", { class: "top" }, el("span", { class: "co" }, l.company), el("span", { class: "date" }, l.date)),
      el("div", { class: "ti" }, l.title),
      el("div", { class: "loc" }, l.location),
      flags, contact,
      el("details", {}, el("summary", {}, "Notes & link"),
        el("label", {}, "Job link", input(l, "job_link", { type: "url", placeholder: "paste the real /jobs/view/ link" })),
        el("label", {}, "Notes", (() => { const t = el("textarea", { rows: "3" }); t.value = l.notes || "";
          t.addEventListener("change", () => save(l, "notes", t.value, t)); return t; })()),
        el("label", {}, "Follow-up draft", select(l, "followup_draft", ["no", "yes"])))),
    el("div", { class: "lead-side" },
      el("label", {}, "Applied", select(l, "application_filled", ["no", "yes"])),
      el("label", {}, "Draft", select(l, "draft_created", ["no", "yes"])),
      el("label", {}, "Reply", select(l, "reply_status", ["none", "sent", "replied", "no reply", "declined"])),
      el("label", {}, "Reply date", input(l, "reply_date", { type: "date" })),
      el("div", { class: "actions" },
        linkOk ? el("a", { class: "btn", href: l.job_link, target: "_blank", rel: "noopener noreferrer" }, "Open job ↗")
               : el("a", { class: "btn warn", href: l.job_link || "#", target: "_blank", rel: "noopener noreferrer" }, "Search ↗"),
        has(l, "tracked") ? el("a", { class: "btn ghost", href: "/" }, "On board")
                          : el("button", { class: "btn", onclick: () => track(l) }, "Add to board"))));
}

function render() {
  renderFilters();
  const list = visible();
  $("count").textContent = `${list.length} / ${state.leads.length} leads`;
  if (!state.leads.length && $("leads").querySelector(".empty")) return;
  $("leads").replaceChildren(...(list.length ? list.map(leadRow) : [el("div", { class: "empty" }, "Nothing matches.")]));
}

$("search").addEventListener("input", () => { state.query = $("search").value; render(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement.tagName !== "INPUT" && document.activeElement.tagName !== "TEXTAREA") {
    e.preventDefault(); $("search").focus();
  }
});
window.addEventListener("focus", load); // picks up a CSV you swapped in while the tab was open
load();
