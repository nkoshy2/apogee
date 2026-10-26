"use strict";
// All data from the server, especially job text and URLs, is untrusted.
// Render with textContent only (via h()); never assign innerHTML.

const LIST_FIELDS = ["preferred_employers", "preferred_locations", "disciplines", "internship_terms"];
const TRISTATE_FIELDS = ["us_citizen", "us_person", "needs_sponsorship"];
const APP_STATUSES = ["preparing", "submitted", "assessment", "interviewing", "offer", "rejected", "withdrawn"];
const ELIG_LABEL = { eligible: "Eligible", uncertain: "Uncertain", ineligible: "Ineligible" };
const ELIG_ICON = { eligible: "✓", uncertain: "?", ineligible: "✕" };
const FINDING_ICON = { met: "✓", unknown: "?", unmet: "✕" };
const MAX_COMPARE = 4;

const state = { selected: new Set(), detailId: null, appDetailId: null, pendingAppId: null };

// ---------------------------------------------------------------- helpers

async function api(method, path, body) {
  const opts = { method, headers: { "X-AIMS-Request": "1" } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (err) {
    throw new Error("Cannot reach the AIMS server. Is it still running?");
  }
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) {
    const err = new Error(describeError(data) || `Request failed (${res.status})`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

function describeError(data) {
  if (!data || data.detail === undefined) return null;
  if (typeof data.detail === "string") return data.detail;
  return data.detail.map((d) => `${d.loc.slice(1).join(".")}: ${d.msg}`).join("; ");
}

function banner(message, kind = "error") {
  const box = document.getElementById("banner");
  box.textContent = message;
  box.className = `banner ${kind}`;
  box.hidden = false;
  clearTimeout(banner.timer);
  if (kind !== "error") banner.timer = setTimeout(() => (box.hidden = true), 3000);
}

// h("tag", {attr: value, onclick: fn}, child, "text", ...): children are nodes or text.
function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "class") node.className = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function safeLink(url, text, cls) {
  let ok = false;
  try { ok = ["http:", "https:"].includes(new URL(url).protocol); } catch { ok = false; }
  if (!ok) return h("span", {}, text);
  return h("a", { href: url, target: "_blank", rel: "noopener noreferrer", class: cls }, text);
}

const fmtDate = (iso) => (iso ? iso.slice(0, 10) : "");
const today = () => new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD, local time
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : "");

function deadlineCell(deadline, done) {
  if (!deadline) return h("td", {}, "");
  const days = Math.round((new Date(deadline + "T00:00") - new Date(today() + "T00:00")) / 86400000);
  const cls = done ? "" : days < 0 ? "overdue" : days <= 7 ? "due-soon" : "";
  const hint = done ? "" : days < 0 ? " (passed)" : days <= 7 ? ` (${days}d)` : "";
  return h("td", { class: cls }, deadline + hint);
}

function eligBadge(status, score) {
  return h("span", { class: `badge elig-${status}`, title: ELIG_LABEL[status] },
    ELIG_ICON[status], score !== undefined ? ` ${score}` : "");
}

// ---------------------------------------------------------------- tabs

function showTab() {
  const name = (location.hash || "#opportunities").slice(1);
  for (const sec of document.querySelectorAll(".tab")) sec.hidden = sec.id !== `tab-${name}`;
  for (const a of document.querySelectorAll("nav a")) a.classList.toggle("active", a.dataset.tab === name);
  if (name === "opportunities") loadOpportunities();
  if (name === "applications") loadApplications();
  if (name === "settings") loadSettings();
}

// ---------------------------------------------------------------- opportunities

function filterQuery() {
  const p = new URLSearchParams();
  const val = (id) => document.getElementById(id).value;
  if (val("f-q").trim()) p.set("q", val("f-q").trim());
  p.set("state", val("f-state"));
  if (val("f-type")) p.set("opportunity_type", val("f-type"));
  if (val("f-elig")) p.set("eligibility", val("f-elig"));
  p.set("sort", val("f-sort"));
  return p.toString();
}

async function loadOpportunities() {
  try {
    const items = await api("GET", `/api/opportunities?${filterQuery()}`);
    const tbody = document.querySelector("#opp-table tbody");
    tbody.replaceChildren(...items.map(oppRow));
    document.getElementById("opp-count").textContent =
      items.length ? `${items.length} shown` : "No opportunities match. Add one with “+ Add / import”, or change the filters.";
  } catch (err) {
    banner(err.message);
  }
}

function oppRow(o) {
  const check = h("input", {
    type: "checkbox", checked: state.selected.has(o.id), "aria-label": "Select for comparison",
    onclick: (ev) => ev.stopPropagation(),
    onchange: (ev) => toggleCompare(o.id, ev.target),
  });
  const status = o.application_status ? `Application: ${o.application_status}` : cap(o.user_state);
  return h("tr", { class: [o.id === state.detailId ? "selected" : "", o.user_state === "dismissed" ? "dim" : ""].join(" "),
                   onclick: (ev) => { if (ev.target.tagName !== "A") showDetail(o.id); } },
    h("td", {}, check),
    h("td", { title: `Relevance ${o.relevance}, preference ${o.preference}` }, eligBadge(o.eligibility, o.score)),
    h("td", {}, o.employer),
    h("td", {}, safeLink(o.url, o.title)),
    h("td", {}, o.location),
    deadlineCell(o.deadline, !!o.application_status && o.application_status !== "preparing"),
    h("td", {}, status),
  );
}

function toggleCompare(id, box) {
  if (box.checked && state.selected.size >= MAX_COMPARE) {
    box.checked = false;
    banner(`Compare up to ${MAX_COMPARE} opportunities at a time.`);
    return;
  }
  box.checked ? state.selected.add(id) : state.selected.delete(id);
  const btn = document.getElementById("compare-btn");
  btn.textContent = `Compare (${state.selected.size})`;
  btn.disabled = state.selected.size < 2;
}

async function patchOpportunity(id, body, okMessage) {
  try {
    await api("PATCH", `/api/opportunities/${id}`, body);
    if (okMessage) banner(okMessage, "ok");
    await loadOpportunities();
    await showDetail(id);
  } catch (err) {
    banner(err.message);
  }
}

function rankingSection(r) {
  const e = r.eligibility;
  const findings = e.findings.map((f) =>
    h("li", { class: `finding ${f.status}` },
      h("span", { class: "icon" }, FINDING_ICON[f.status]), " ", f.reason,
      f.mandatory ? "" : h("span", { class: "muted" }, " (preferred, not required)"),
      h("blockquote", {}, f.evidence)));
  const reasons = (list) => (list.length ? h("ul", {}, list.map((x) => h("li", {}, x))) : h("p", { class: "muted" }, "Nothing to note."));
  return h("div", { class: "ranking" },
    h("h4", {}, "Eligibility: ", eligBadge(e.status), " ", ELIG_LABEL[e.status]),
    h("p", { class: "muted" }, e.summary),
    findings.length ? h("ul", { class: "findings" }, findings) : null,
    h("h4", {}, `Relevance: ${r.relevance.score}/100`), reasons(r.relevance.reasons),
    h("h4", {}, `Preference: ${r.preference.score}/100`), reasons(r.preference.reasons),
    h("p", { class: "muted" }, `Overall fit ${r.score} = 0.6 × relevance + 0.4 × preference. Sorted by eligibility first.`));
}

async function showDetail(id) {
  let o;
  try {
    o = await api("GET", `/api/opportunities/${id}`);
  } catch (err) {
    banner(err.message);
    return;
  }
  state.detailId = id;
  for (const tr of document.querySelectorAll("#opp-table tbody tr")) tr.classList.remove("selected");

  const notes = h("textarea", { rows: 3 });
  notes.value = o.user_notes;
  const s = o.user_state;
  const actions = h("div", { class: "row wrap" },
    safeLink(o.url, "Open posting ↗", "button-link"),
    s === "saved"
      ? h("button", { class: "secondary", onclick: () => patchOpportunity(id, { user_state: "new" }, "Removed from saved.") }, "Unsave")
      : h("button", { onclick: () => patchOpportunity(id, { user_state: "saved" }, "Saved.") }, "Save"),
    s === "dismissed"
      ? h("button", { class: "secondary", onclick: () => patchOpportunity(id, { user_state: "new" }, "Restored.") }, "Restore")
      : h("button", { class: "secondary", onclick: () => patchOpportunity(id, { user_state: "dismissed" }, "Dismissed. Find it under the Dismissed filter.") }, "Dismiss"),
    o.application_id
      ? h("button", { class: "secondary", onclick: () => openApplication(o.application_id) }, `Application: ${o.application_status}`)
      : h("button", { class: "secondary", onclick: () => startApplication(id) }, "Start application"),
    o.source === "manual" ? h("button", { class: "secondary", onclick: () => openForm(o) }, "Edit") : null,
  );

  const box = document.getElementById("opp-detail");
  box.replaceChildren(
    h("h3", {}, o.title),
    h("p", {}, h("strong", {}, o.employer), o.location ? ` · ${o.location}` : "", ` · ${o.opportunity_type}`,
      o.deadline ? ` · deadline ${o.deadline}` : ""),
    h("p", { class: "muted small" },
      `Source: ${o.source} · first seen ${fmtDate(o.first_seen_at)} · last verified ${fmtDate(o.last_seen_at)} · listing ${o.listing_status} · ${s}`),
    actions,
    rankingSection(o.ranking),
    h("h4", {}, "Notes"), notes,
    h("p", {}, h("button", { class: "secondary", onclick: () => patchOpportunity(id, { user_notes: notes.value }, "Notes saved.") }, "Save notes")),
    h("h4", {}, "Description"),
    h("pre", {}, o.description || "(no description)"),
  );
  box.hidden = false;
  loadOpportunities();
}

async function startApplication(opportunityId) {
  try {
    const a = await api("POST", "/api/applications", { opportunity_id: opportunityId });
    banner("Application started. Status: preparing.", "ok");
    openApplication(a.id);
  } catch (err) {
    if (err.status === 409) openApplication(err.data.existing_id);
    else banner(err.message);
  }
}

function openApplication(appId) {
  state.pendingAppId = appId;
  document.getElementById("a-status").value = "all";
  location.hash = "#applications";
}

function openForm(o) {
  const form = document.getElementById("opp-form");
  form.reset();
  const editing = !!o;
  document.getElementById("opp-form-title").textContent = editing ? "Edit opportunity" : "Add opportunity";
  form.querySelector("[data-create-only]").hidden = editing;
  form.elements.id.value = editing ? o.id : "";
  if (editing) {
    for (const f of ["employer", "title", "url", "location", "opportunity_type", "description"]) form.elements[f].value = o[f] || "";
    form.elements.deadline.value = o.deadline || "";
  }
  form.hidden = false;
  document.getElementById("import-panel").hidden = editing;
  form.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function closeForm() {
  document.getElementById("opp-form").hidden = true;
  document.getElementById("import-panel").hidden = true;
}

async function submitOpportunity(ev) {
  ev.preventDefault();
  const form = ev.target;
  const body = Object.fromEntries(new FormData(form).entries());
  const id = body.id;
  delete body.id;
  body.deadline = body.deadline || null;
  try {
    let saved;
    if (id) {
      delete body.user_notes;
      saved = await api("PATCH", `/api/opportunities/${id}`, body);
      banner("Opportunity updated.", "ok");
    } else {
      saved = await api("POST", "/api/opportunities", body);
      banner("Opportunity added.", "ok");
    }
    closeForm();
    await loadOpportunities();
    showDetail(saved.id);
  } catch (err) {
    if (err.status === 409 && err.data && err.data.existing_id) {
      banner(`Already tracked: ${err.message}`);
      showDetail(err.data.existing_id);
    } else {
      banner(err.message);
    }
  }
}

async function importFile(ev) {
  const file = ev.target.files[0];
  if (!file) return;
  const out = document.getElementById("import-result");
  out.replaceChildren(h("p", { class: "muted" }, "Importing…"));
  try {
    const format = file.name.toLowerCase().endsWith(".json") ? "json" : "csv";
    const res = await api("POST", "/api/opportunities/import", { format, content: await file.text() });
    const c = res.counts;
    const problems = res.rows.filter((r) => r.result !== "created");
    out.replaceChildren(
      h("p", {}, `${c.created} added, ${c.duplicate} already tracked, ${c.invalid} invalid.`),
      problems.length ? h("ul", { class: "small" }, problems.map((r) =>
        h("li", {}, `Row ${r.row}: ${r.result}`, r.detail ? ` (${r.detail})` : r.id ? ` (existing id ${r.id})` : ""))) : null);
    loadOpportunities();
  } catch (err) {
    out.replaceChildren(h("p", { class: "error-text" }, err.message));
  } finally {
    ev.target.value = "";
  }
}

async function showCompare() {
  const box = document.getElementById("compare");
  try {
    const items = await Promise.all([...state.selected].map((id) => api("GET", `/api/opportunities/${id}`)));
    const rows = [
      ["Employer", (o) => o.employer],
      ["Title", (o) => safeLink(o.url, o.title)],
      ["Location", (o) => o.location || "—"],
      ["Type", (o) => o.opportunity_type],
      ["Deadline", (o) => o.deadline || "—"],
      ["Eligibility", (o) => h("span", {}, eligBadge(o.ranking.eligibility.status), " ", ELIG_LABEL[o.ranking.eligibility.status])],
      ["Requirements", (o) => h("ul", { class: "small" }, o.ranking.eligibility.findings.map((f) => h("li", {}, FINDING_ICON[f.status], " ", f.reason)))],
      ["Overall fit", (o) => String(o.ranking.score)],
      ["Relevance", (o) => String(o.ranking.relevance.score)],
      ["Preference", (o) => String(o.ranking.preference.score)],
      ["Matching skills", (o) => o.ranking.relevance.matched_skills.join(", ") || "—"],
      ["Missing skills", (o) => o.ranking.relevance.missing_skills.join(", ") || "—"],
      ["Status", (o) => (o.application_status ? `Application: ${o.application_status}` : o.user_state)],
      ["Last verified", (o) => fmtDate(o.last_seen_at)],
    ];
    box.replaceChildren(
      h("div", { class: "row spread" }, h("h2", {}, "Compare"),
        h("button", { class: "secondary", onclick: () => (box.hidden = true) }, "Close")),
      h("div", { class: "scroll-x" }, h("table", { class: "compare" },
        h("tbody", {}, rows.map(([label, fn]) => h("tr", {}, h("th", {}, label), items.map((o) => h("td", {}, fn(o)))))))));
    box.hidden = false;
    box.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    banner(err.message);
  }
}

// ---------------------------------------------------------------- applications

async function loadApplications() {
  try {
    const items = await api("GET", `/api/applications?status=${document.getElementById("a-status").value}`);
    document.querySelector("#app-table tbody").replaceChildren(...items.map((a) =>
      h("tr", { class: a.id === state.appDetailId ? "selected" : "", onclick: (ev) => { if (ev.target.tagName !== "A") showApplication(a.id); } },
        h("td", {}, a.employer),
        h("td", {}, safeLink(a.url, a.title)),
        h("td", {}, h("span", { class: `status status-${a.status}` }, a.status)),
        deadlineCell(a.deadline, a.status !== "preparing"),
        h("td", {}, a.submitted_on || ""),
        h("td", {}, fmtDate(a.updated_at)))));
    document.getElementById("app-count").textContent = items.length ? `${items.length} shown` : "No applications in this view.";
    if (state.pendingAppId) {
      const id = state.pendingAppId;
      state.pendingAppId = null;
      showApplication(id);
    }
  } catch (err) {
    banner(err.message);
  }
}

async function showApplication(id) {
  let a;
  try {
    a = await api("GET", `/api/applications/${id}`);
  } catch (err) {
    banner(err.message);
    return;
  }
  state.appDetailId = id;
  const status = h("select", { name: "status" }, APP_STATUSES.map((s) => h("option", { value: s, selected: s === a.status }, cap(s))));
  const submitted = h("input", { type: "date", name: "submitted_on", value: a.submitted_on || "" });
  const deadline = h("input", { type: "date", name: "deadline", value: a.deadline || "" });
  const notes = h("textarea", { name: "notes", rows: 4 });
  notes.value = a.notes;
  const eventNote = h("input", { name: "event_note", maxlength: 2000, placeholder: "Optional: what happened (kept in history)" });
  status.addEventListener("change", () => {
    if (status.value === "submitted" && !submitted.value) submitted.value = today();
  });

  const form = h("form", { onsubmit: async (ev) => {
      ev.preventDefault();
      try {
        await api("PATCH", `/api/applications/${id}`, {
          status: status.value, submitted_on: submitted.value || null, deadline: deadline.value || null,
          notes: notes.value, event_note: eventNote.value,
        });
        banner("Application updated.", "ok");
        loadApplications();
        showApplication(id);
      } catch (err) { banner(err.message); }
    } },
    h("div", { class: "grid2" },
      h("label", {}, "Status", status),
      h("label", {}, "Submitted on", submitted),
      h("label", {}, "Deadline", deadline)),
    h("label", {}, "What changed", eventNote),
    h("label", {}, "Notes", notes),
    h("button", { type: "submit" }, "Save changes"));

  const quickNote = h("input", { maxlength: 2000, placeholder: "e.g. Recruiter emailed about OA", required: true });
  const noteForm = h("form", { class: "row", onsubmit: async (ev) => {
      ev.preventDefault();
      try {
        await api("POST", `/api/applications/${id}/events`, { note: quickNote.value });
        showApplication(id);
      } catch (err) { banner(err.message); }
    } }, quickNote, h("button", { type: "submit", class: "secondary" }, "Add note"));

  const history = h("ol", { class: "history" }, [...a.events].reverse().map((e) =>
    h("li", {},
      h("span", { class: "muted small" }, e.at.replace("T", " ").replace("Z", " UTC")), " ",
      e.from_status === null ? `Created as ${e.to_status}` : e.from_status === e.to_status ? "Note" : `${e.from_status} → ${e.to_status}`,
      e.note ? h("div", {}, e.note) : null)));

  const box = document.getElementById("app-detail");
  box.replaceChildren(
    h("h3", {}, a.title),
    h("p", {}, h("strong", {}, a.employer)),
    h("div", { class: "row wrap" },
      safeLink(a.url, "Open posting ↗", "button-link"),
      h("button", { class: "secondary", onclick: () => { location.hash = "#opportunities"; showDetail(a.opportunity_id); } }, "Show opportunity")),
    form,
    h("h4", {}, "History"), noteForm, history);
  box.hidden = false;
  for (const tr of document.querySelectorAll("#app-table tbody tr")) tr.classList.remove("selected");
}

// ---------------------------------------------------------------- settings

function setupSettingsForm() {
  const form = document.getElementById("settings-form");
  for (const f of TRISTATE_FIELDS) {
    form.elements[f].replaceChildren(
      h("option", { value: "" }, "Not specified"), h("option", { value: "true" }, "Yes"), h("option", { value: "false" }, "No"));
  }
}

async function loadSettings() {
  try {
    const [s, health] = await Promise.all([api("GET", "/api/settings"), api("GET", "/api/health")]);
    const form = document.getElementById("settings-form");
    for (const f of LIST_FIELDS) form.elements[f].value = s[f].join("\n");
    form.elements.backup_mirror_dir.value = s.backup_mirror_dir;
    const p = s.profile;
    for (const f of TRISTATE_FIELDS) form.elements[f].value = p[f] === null ? "" : String(p[f]);
    form.elements.degree_level.value = p.degree_level || "";
    form.elements.graduation.value = p.graduation || "";
    form.elements.gpa.value = p.gpa ?? "";
    form.elements.skills.value = p.skills.join("\n");
    document.getElementById("health").replaceChildren(...[
      ["Version", health.version], ["Schema version", health.schema_version],
      ["Database check", health.database], ["Data folder", health.data_dir],
    ].flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, String(v))]));
  } catch (err) {
    banner(err.message);
  }
}

async function submitSettings(ev) {
  ev.preventDefault();
  const form = ev.target;
  const lines = (name) => form.elements[name].value.split("\n").map((x) => x.trim()).filter(Boolean);
  const tri = (name) => (form.elements[name].value === "" ? null : form.elements[name].value === "true");
  const body = { backup_mirror_dir: form.elements.backup_mirror_dir.value, profile: {
    us_citizen: tri("us_citizen"), us_person: tri("us_person"), needs_sponsorship: tri("needs_sponsorship"),
    degree_level: form.elements.degree_level.value || null,
    graduation: form.elements.graduation.value || null,
    gpa: form.elements.gpa.value === "" ? null : Number(form.elements.gpa.value),
    skills: lines("skills"),
  } };
  for (const f of LIST_FIELDS) body[f] = lines(f);
  try {
    await api("PUT", "/api/settings", body);
    banner("Settings saved. Rankings now use them.", "ok");
  } catch (err) {
    banner(err.message);
  }
}

async function shutdown() {
  if (!confirm("Shut down the Internship Manager server? Reopen it from the desktop shortcut.")) return;
  try {
    await api("POST", "/api/shutdown");
    document.body.replaceChildren(h("p", { class: "panel" }, "The server has shut down. You can close this tab."));
  } catch (err) {
    banner(err.message);
  }
}

// ---------------------------------------------------------------- wiring

window.addEventListener("hashchange", showTab);
document.addEventListener("DOMContentLoaded", () => {
  let searchTimer;
  document.getElementById("f-q").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadOpportunities, 250);
  });
  for (const id of ["f-state", "f-type", "f-elig", "f-sort"]) document.getElementById(id).addEventListener("change", loadOpportunities);
  document.getElementById("compare-btn").addEventListener("click", showCompare);
  document.getElementById("add-btn").addEventListener("click", () => openForm(null));
  document.getElementById("opp-form").addEventListener("submit", submitOpportunity);
  document.getElementById("opp-form-cancel").addEventListener("click", closeForm);
  document.getElementById("import-file").addEventListener("change", importFile);
  document.getElementById("a-status").addEventListener("change", loadApplications);
  setupSettingsForm();
  document.getElementById("settings-form").addEventListener("submit", submitSettings);
  document.getElementById("shutdown").addEventListener("click", shutdown);
  showTab();
});
