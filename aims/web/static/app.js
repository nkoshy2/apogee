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
    throw new Error("Can't reach Apogee. Is it still running? Reopen it from the desktop shortcut.");
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
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

// Replace a node's children; accepts nested arrays and skips null/false (unlike replaceChildren).
function fill(node, ...children) {
  node.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false)
    .map((c) => (c instanceof Node ? c : document.createTextNode(String(c)))));
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
  if (!deadline) return h("td", { class: "deadline" }, "");
  const days = Math.round((new Date(deadline + "T00:00") - new Date(today() + "T00:00")) / 86400000);
  const cls = done ? "" : days < 0 ? "overdue" : days <= 7 ? "due-soon" : "";
  const hint = done ? "" : days < 0 ? " (passed)" : days <= 7 ? ` (${days}d)` : "";
  return h("td", { class: `deadline ${cls}` }, deadline + hint);
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
  if (name === "resume") loadResumeStudio();
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
      items.length ? `${items.length} shown` : "Nothing matches. Add an opportunity, or widen the filters above.";
    loadSummary();
  } catch (err) {
    banner(err.message);
  }
}

// ---------------------------------------------------------------- summaries (sentences + nav counts)

function setFilter(id, value) {
  const el = document.getElementById(id);
  el.value = value;
  el.dispatchEvent(new Event("change"));
}

function linkBtn(text, onclick, attention) {
  return h("button", { type: "button", class: attention ? "inline-link attention" : "inline-link", onclick }, text);
}

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

async function loadSummary() {
  let s;
  try {
    s = await api("GET", "/api/summary");
  } catch {
    return; // the list request already reports connection problems
  }
  const o = s.opportunities, a = s.applications;
  document.getElementById("nav-count-opportunities").textContent = o.active || "";
  document.getElementById("nav-count-applications").textContent = a.active || "";

  const oppLede = document.getElementById("opp-lede");
  if (!o.active) {
    fill(oppLede, "Nothing tracked yet. Add an opportunity or import a list to get started.");
  } else {
    const parts = [`You're tracking ${plural(o.active, "open opportunity", "open opportunities")}: `,
      linkBtn(`${o.eligible} eligible`, () => setFilter("f-elig", "eligible")), " and ",
      linkBtn(`${o.uncertain} to check`, () => setFilter("f-elig", "uncertain"))];
    if (o.ineligible) parts.push(", plus ", linkBtn(`${o.ineligible} that ${o.ineligible === 1 ? "fails" : "fail"} a requirement`, () => setFilter("f-elig", "ineligible")));
    parts.push(". ");
    if (o.overdue) parts.push(linkBtn(`${plural(o.overdue, "deadline has", "deadlines have")} passed`, () => setFilter("f-sort", "deadline"), true), ", and ");
    const dueText = `${plural(o.due_soon, "deadline falls", "deadlines fall")} in the next week`;
    parts.push(o.due_soon ? linkBtn(dueText, () => setFilter("f-sort", "deadline"), true)
                          : (o.overdue ? "none fall" : "No deadlines fall") + " in the next week", ".");
    fill(oppLede, ...parts);
  }

  const appLede = document.getElementById("app-lede");
  if (!a.active) {
    fill(appLede, "No active applications. Start one from an opportunity you've saved.");
  } else {
    fill(appLede, 
      "You have ", linkBtn(plural(a.active, "active application", "active applications"), () => setFilter("a-status", "active")), ": ",
      linkBtn(`${a.preparing} in preparation`, () => setFilter("a-status", "preparing")), " and ",
      linkBtn(`${a.waiting} waiting to hear back`, () => setFilter("a-status", "submitted")),
      a.offers ? [", with ", linkBtn(plural(a.offers, "offer", "offers"), () => setFilter("a-status", "offer"))] : "",
      ". ", a.due_soon ? linkBtn(`${plural(a.due_soon, "deadline", "deadlines")} in the next week`, () => setFilter("a-status", "preparing"), true)
                       : "No deadlines in the next week", ".");
  }
}

function oppRow(o) {
  const check = h("input", {
    type: "checkbox", checked: state.selected.has(o.id), "aria-label": "Select for comparison",
    onclick: (ev) => ev.stopPropagation(),
    onchange: (ev) => toggleCompare(o.id, ev.target),
  });
  const status = [
    o.application_status
      ? h("span", { class: `status status-${o.application_status}` }, cap(o.application_status))
      : h("span", { class: o.user_state === "saved" ? "tag ok" : "tag" }, cap(o.user_state)),
    o.listing_status === "not_seen" ? h("span", { class: "tag attention", title: "Missing from the employer's latest complete listing" }, "Not seen") : null,
  ];
  return h("tr", { class: [o.id === state.detailId ? "selected" : "", o.user_state === "dismissed" ? "dim" : ""].join(" "),
                   onclick: (ev) => { if (ev.target.tagName !== "A" && ev.target.tagName !== "INPUT") showDetail(o.id); } },
    h("td", {}, check),
    h("td", { title: `Relevance ${o.relevance}, preference ${o.preference}` }, eligBadge(o.eligibility, o.score)),
    h("td", {}, h("strong", {}, o.title), h("span", { class: "sub" }, o.employer)),
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
      h("span", { class: "icon-mark" }, FINDING_ICON[f.status]), " ", f.reason,
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
    h("button", { class: "secondary", onclick: () => tailorResume(o.id) }, "Tailor resume"),
    o.source === "manual" ? h("button", { class: "secondary", onclick: () => openForm(o) }, "Edit") : null,
  );

  const box = document.getElementById("opp-detail");
  fill(box, 
    h("h3", {}, o.title),
    h("p", {}, h("strong", {}, o.employer), o.location ? ` · ${o.location}` : "", ` · ${o.opportunity_type}`,
      o.deadline ? ` · deadline ${o.deadline}` : ""),
    h("p", { class: "muted small" },
      o.source === "manual" ? `Added by you on ${fmtDate(o.first_seen_at)} · ${s}`
        : `From ${cap(o.source)} (${o.source_key}) · first seen ${fmtDate(o.first_seen_at)} · last seen on the board ${fmtDate(o.last_seen_at)} · ${s}`),
    o.listing_status === "not_seen"
      ? h("p", { class: "notice" }, `Not in ${o.employer}'s complete listing since ${fmtDate(o.missing_since)}. It may have closed or been renamed; check the posting before applying. Nothing has been deleted.`)
      : null,
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
  fill(out, h("p", { class: "muted" }, "Importing…"));
  try {
    const format = file.name.toLowerCase().endsWith(".json") ? "json" : "csv";
    const res = await api("POST", "/api/opportunities/import", { format, content: await file.text() });
    const c = res.counts;
    const problems = res.rows.filter((r) => r.result !== "created");
    fill(out, 
      h("p", {}, `${c.created} added, ${c.duplicate} already tracked, ${c.invalid} invalid.`),
      problems.length ? h("ul", { class: "small" }, problems.map((r) =>
        h("li", {}, `Row ${r.row}: ${r.result}`, r.detail ? ` (${r.detail})` : r.id ? ` (existing id ${r.id})` : ""))) : null);
    loadOpportunities();
  } catch (err) {
    fill(out, h("p", { class: "error-text" }, err.message));
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
    fill(box, 
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
        h("td", {}, h("strong", {}, a.title), h("span", { class: "sub" }, a.employer)),
        h("td", {}, h("span", { class: `status status-${a.status}` }, cap(a.status))),
        deadlineCell(a.deadline, a.status !== "preparing"),
        h("td", {}, a.submitted_on || ""),
        h("td", {}, fmtDate(a.updated_at)))));
    document.getElementById("app-count").textContent = items.length ? `${items.length} shown` : "No applications in this view.";
    loadSummary();
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
  let docs = [];
  try { docs = await api("GET", `/api/resumes?opportunity_id=${a.opportunity_id}`); } catch { /* shown as none */ }
  const resumeSel = h("select", { name: "resume_document_id" },
    h("option", { value: "" }, docs.length ? "None attached" : "None yet (tailor one in Resume Studio)"),
    docs.map((d) => h("option", { value: d.id, selected: d.id === a.resume_document_id },
      `Resume #${d.id}, ${fmtDate(d.created_at)}, ${plural(d.page_count, "page", "pages")}`)));
  status.addEventListener("change", () => {
    if (status.value === "submitted" && !submitted.value) submitted.value = today();
  });

  const form = h("form", { onsubmit: async (ev) => {
      ev.preventDefault();
      try {
        await api("PATCH", `/api/applications/${id}`, {
          status: status.value, submitted_on: submitted.value || null, deadline: deadline.value || null,
          notes: notes.value, event_note: eventNote.value,
          resume_document_id: resumeSel.value ? Number(resumeSel.value) : null,
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
    h("label", {}, "Resume used", resumeSel),
    a.resume_document_id ? h("p", { class: "meta" }, h("a", { href: `/api/resumes/${a.resume_document_id}/pdf`, target: "_blank", rel: "noopener" }, `Open resume #${a.resume_document_id} ↗`)) : null,
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
  fill(box, 
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

// ---------------------------------------------------------------- resume studio

const KIND_LABEL = { education: "Education", employment: "Work experience", research: "Research", project: "Project",
                     leadership: "Leadership", achievement: "Honor or award" };
const STATEMENT_TAG = { draft: "tag", verified: "tag attention", approved: "tag ok" };
const CHECK_MARK = { pass: ["✓", "met"], warn: ["!", "unknown"], fail: ["✕", "unmet"] };
const rs = { sub: "generate", oppId: "", include: new Set(), exclude: new Set() };

function loadResumeStudio() {
  showSub(rs.sub);
  loadResumeLede();
}

function showSub(name) {
  rs.sub = name;
  for (const v of document.querySelectorAll("#tab-resume .subview")) v.hidden = v.id !== `rs-${name}`;
  for (const b of document.querySelectorAll(".subviews button")) b.classList.toggle("active", b.dataset.sub === name);
  if (name === "generate") loadGenerate();
  if (name === "experience") loadExperiences();
  if (name === "skills") loadSkills();
  if (name === "header") loadHeader();
  if (name === "documents") loadDocuments();
}

function tailorResume(opportunityId) {
  rs.oppId = String(opportunityId);
  rs.include.clear();
  rs.exclude.clear();
  rs.sub = "generate";
  if (location.hash === "#resume") loadResumeStudio(); else location.hash = "#resume";
}

async function loadResumeLede() {
  try {
    const [exps, skills] = await Promise.all([api("GET", "/api/experiences"), api("GET", "/api/skills")]);
    const statements = exps.flatMap((e) => e.statements);
    const approved = statements.filter((s) => s.status === "approved").length;
    const pending = statements.length - approved;
    const lede = document.getElementById("resume-lede");
    if (!exps.length) {
      fill(lede, "Start by adding your ", linkBtn("experience", () => showSub("experience")),
        " and writing a few statements for each. Apogee only ever uses wording you've approved.");
      return;
    }
    fill(lede, 
      `${plural(approved, "approved statement", "approved statements")} across ${plural(exps.length, "experience", "experiences")}`,
      pending ? [", with ", linkBtn(`${pending} still to review`, () => showSub("experience"), true)] : "",
      ", and ", linkBtn(plural(skills.length, "skill", "skills"), () => showSub("skills")), ".");
  } catch (err) {
    banner(err.message);
  }
}

// ---- tailor

async function loadGenerate() {
  const sel = document.getElementById("rs-opp");
  try {
    const opps = await api("GET", "/api/opportunities?state=all&sort=rank");
    fill(sel, h("option", { value: "" }, "a general audience (no specific job)"),
      opps.map((o) => h("option", { value: o.id }, `${o.title} at ${o.employer}`)));
    sel.value = rs.oppId;
    if (sel.value !== rs.oppId) rs.oppId = sel.value;
  } catch (err) {
    banner(err.message);
  }
  loadPlan();
}

async function loadPlan() {
  const box = document.getElementById("rs-plan");
  try {
    const p = await api("POST", "/api/resumes/plan", {
      opportunity_id: rs.oppId ? Number(rs.oppId) : null, include_ids: [...rs.include], exclude_ids: [...rs.exclude] });
    renderPlan(p, box);
  } catch (err) {
    fill(box, h("p", { class: "error-text" }, err.message));
  }
}

function toggleStatement(id, checked) {
  if (checked) { rs.exclude.delete(id); rs.include.add(id); } else { rs.include.delete(id); rs.exclude.add(id); }
  loadPlan();
}

function renderPlan(p, box) {
  const intro = p.opportunity
    ? h("p", { class: "meta" }, "Matched against this posting's keywords: ",
        p.job_tags.length ? p.job_tags.join(", ") : "none recognized, so statements keep your order", ".")
    : h("p", { class: "meta" }, "No job selected: statements keep your own order.");
  const sections = p.sections.map((sec) => h("div", { class: "plan-section" },
    h("p", { class: "kicker" }, sec.title),
    sec.entries.map((e) => h("div", { class: e.included ? "plan-entry" : "plan-entry dim" },
      h("div", { class: "row spread" },
        h("strong", {}, e.title, e.organization ? h("span", { class: "muted" }, ` · ${e.organization}`) : ""),
        h("span", { class: "meta" }, e.dates)),
      e.included ? null : h("p", { class: "meta" }, "Left out: no approved statements selected."),
      e.statements.map((s) => h("label", { class: "plan-stmt" },
        h("input", { type: "checkbox", checked: s.included, disabled: s.protected,
                     onchange: (ev) => toggleStatement(s.id, ev.target.checked) }),
        h("span", {}, s.text,
          h("span", { class: "meta" }, ` ${s.protected ? "🔒 protected" : s.reason}`,
            s.matched.length ? ` · matches ${s.matched.join(", ")}` : "")))),
    ))));
  const skills = p.skills.length
    ? h("div", { class: "plan-section" }, h("p", { class: "kicker" }, "Skills"),
        p.skills.map((l) => h("p", { class: "small" }, h("strong", {}, `${l.category}: `),
          l.items.map((i, n) => [n ? ", " : "", i.relevant ? h("mark", {}, i.name) : i.name]))))
    : h("p", { class: "meta" }, "No skills yet. Add them under Skills.");
  fill(box, 
    h("h2", {}, "Content"),
    intro,
    p.warnings.length ? h("ul", { class: "warnings" }, p.warnings.map((w) => h("li", {}, w))) : null,
    p.sections.length ? sections : h("p", { class: "muted" }, "Nothing approved yet. Approve statements under Experience."),
    skills,
    h("p", { class: "meta" }, "Untick a statement to leave it out, or tick one to force it in. If the resume runs long, Apogee drops the lowest-scoring optional statements. It never shrinks fonts or margins."));
}

async function generateResume() {
  const btn = document.getElementById("rs-generate-btn");
  const box = document.getElementById("rs-result");
  btn.disabled = true;
  btn.textContent = "Generating…";
  box.hidden = false;
  fill(box, h("p", { class: "muted" }, "Compiling with LaTeX…"));
  try {
    const doc = await api("POST", "/api/resumes", {
      opportunity_id: rs.oppId ? Number(rs.oppId) : null, page_limit: Number(document.getElementById("rs-pages").value),
      include_ids: [...rs.include], exclude_ids: [...rs.exclude] });
    renderDocument(doc, box);
    banner(`Resume #${doc.id} saved.`, "ok");
    loadResumeLede();
  } catch (err) {
    fill(box, h("h2", {}, "Not generated"), h("pre", { class: "error-text" }, err.message),
      err.data && err.data.checks ? checkList(err.data.checks) : null,
      h("p", { class: "meta" }, "Nothing was saved, and earlier resumes are untouched."));
  } finally {
    btn.disabled = false;
    btn.textContent = "Generate PDF";
  }
}

function checkList(checks) {
  return h("ul", { class: "findings" }, checks.map((c) => h("li", { class: `finding ${CHECK_MARK[c.status][1]}` },
    h("span", { class: "icon-mark" }, CHECK_MARK[c.status][0]), " ", c.name,
    c.detail ? h("span", { class: "meta" }, ` · ${c.detail}`) : "")));
}

function renderDocument(doc, box) {
  const target = doc.opportunity_title ? `${doc.opportunity_title} at ${doc.opportunity_employer}` : "General resume";
  fill(box, 
    h("p", { class: "kicker" }, `Resume #${doc.id}`),
    h("h2", {}, target),
    h("p", { class: "meta" }, `${plural(doc.page_count, "page", "pages")} · template ${doc.template_version} · ${fmtDate(doc.created_at)}`),
    h("div", { class: "row wrap" },
      h("a", { class: "button-link", href: `/api/resumes/${doc.id}/pdf?download=true` }, "Download PDF"),
      h("a", { class: "button-link secondary-link", href: `/api/resumes/${doc.id}/tex` }, "Download .tex"),
      doc.opportunity_url ? safeLink(doc.opportunity_url, "Open posting ↗", "button-link secondary-link") : null),
    doc.dropped && doc.dropped.length
      ? h("details", {}, h("summary", {}, `${plural(doc.dropped.length, "statement was", "statements were")} left out to fit the page`),
          h("ul", { class: "small" }, doc.dropped.map((d) => h("li", {}, d.text))))
      : null,
    h("h4", {}, "Checks"), checkList(doc.validation.checks),
    h("iframe", { class: "pdf-preview", src: `/api/resumes/${doc.id}/pdf#navpanes=0&view=FitH`, title: `Preview of resume #${doc.id}` }));
}

// ---- experience

async function loadExperiences() {
  try {
    const exps = await api("GET", "/api/experiences");
    const list = document.getElementById("rs-exp-list");
    list.replaceChildren(...(exps.length ? exps.map(experienceCard)
      : [h("div", { class: "panel" }, h("p", { class: "muted" }, "No experience yet. Add your education first, then jobs, research, and projects."))]));
  } catch (err) {
    banner(err.message);
  }
}

async function statementAction(id, body, okMessage) {
  try {
    await api("PATCH", `/api/statements/${id}`, body);
    if (okMessage) banner(okMessage, "ok");
    loadExperiences();
    loadResumeLede();
  } catch (err) {
    banner(err.message);
  }
}

function statementRow(s) {
  const actions = [];
  if (s.status === "draft") actions.push(h("button", { class: "ghost", onclick: () => statementAction(s.id, { status: "verified" }, "Marked as verified.") }, "Mark verified"));
  if (s.status === "verified") actions.push(h("button", { class: "ghost", onclick: () => statementAction(s.id, { status: "approved" }, "Approved for resumes.") }, "Approve"));
  if (s.status !== "draft") actions.push(h("button", { class: "ghost", onclick: () => statementAction(s.id, { status: "draft" }) }, "Back to draft"));
  actions.push(h("button", { class: "ghost", onclick: () => statementAction(s.id, { protected: !s.protected }) }, s.protected ? "Unprotect" : "Protect"));
  const row = h("li", { class: "stmt" });
  const view = () => fill(row, 
    h("div", { class: "row spread" },
      h("span", {}, h("span", { class: STATEMENT_TAG[s.status] }, cap(s.status)), s.protected ? h("span", { class: "tag" }, "🔒 Protected") : null),
      h("span", { class: "row" },
        h("button", { class: "ghost", onclick: edit }, "Edit"),
        s.used_in_resume ? null : h("button", { class: "ghost", onclick: () => removeStatement(s.id) }, "Delete"))),
    h("p", { class: "stmt-text" }, s.text),
    h("div", { class: "row wrap" }, actions));
  const edit = () => {
    const ta = h("textarea", { rows: 3, maxlength: 600 });
    ta.value = s.text;
    fill(row, ta,
      s.status !== "draft" ? h("p", { class: "meta" }, "Changing the wording sends this statement back to draft for re-approval.") : null,
      h("div", { class: "row wrap" },
        h("button", { onclick: () => statementAction(s.id, { text: ta.value }, "Statement saved.") }, "Save"),
        h("button", { class: "secondary", onclick: view }, "Cancel")));
    ta.focus();
  };
  view();
  return row;
}

async function removeStatement(id) {
  if (!confirm("Delete this statement? This can't be undone.")) return;
  try {
    await api("DELETE", `/api/statements/${id}`);
    loadExperiences();
    loadResumeLede();
  } catch (err) {
    banner(err.message);
  }
}

function experienceCard(e) {
  const input = h("textarea", { rows: 2, maxlength: 600, placeholder: "Add a statement: what you did, how, and the result (with real numbers)." });
  const addForm = h("form", { class: "add-stmt", onsubmit: async (ev) => {
      ev.preventDefault();
      if (!input.value.trim()) return;
      try {
        await api("POST", `/api/experiences/${e.id}/statements`, { text: input.value });
        loadExperiences();
        loadResumeLede();
      } catch (err) { banner(err.message); }
    } }, input, h("button", { type: "submit", class: "secondary" }, "Add statement"));
  const dates = formatMonths(e);
  return h("div", { class: "panel" },
    h("div", { class: "row spread" },
      h("p", { class: "kicker" }, KIND_LABEL[e.kind], e.protected ? " · always included" : ""),
      h("span", { class: "row" },
        h("button", { class: "ghost", onclick: () => editExperience(e) }, "Edit"),
        e.statements.length ? null : h("button", { class: "ghost", onclick: () => removeExperience(e.id) }, "Delete"))),
    h("h3", {}, e.title),
    h("p", { class: "meta" }, [e.organization, e.location, dates].filter(Boolean).join(" · ")),
    h("ul", { class: "stmts" }, e.statements.map(statementRow)),
    addForm);
}

function formatMonths(e) {
  const m = (v) => (v ? new Date(`${v}-01T00:00`).toLocaleDateString("en-US", { month: "short", year: "numeric" }) : "");
  if (e.is_current) return e.kind === "education" && e.end_date ? `Expected ${m(e.end_date)}` : `${m(e.start_date)} – Present`;
  return [m(e.start_date), m(e.end_date)].filter(Boolean).join(" – ");
}

async function removeExperience(id) {
  if (!confirm("Delete this experience?")) return;
  try {
    await api("DELETE", `/api/experiences/${id}`);
    loadExperiences();
  } catch (err) {
    banner(err.message);
  }
}

function editExperience(e) {
  const form = document.getElementById("rs-exp-form");
  form.elements.id.value = e.id;
  for (const f of ["kind", "title", "organization", "location"]) form.elements[f].value = e[f];
  form.elements.start_date.value = e.start_date || "";
  form.elements.end_date.value = e.end_date || "";
  form.elements.is_current.checked = e.is_current;
  form.elements.protected.checked = e.protected;
  document.getElementById("rs-exp-form-title").textContent = "Edit experience";
  document.getElementById("rs-exp-cancel").hidden = false;
  form.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function resetExperienceForm() {
  const form = document.getElementById("rs-exp-form");
  form.reset();
  form.elements.id.value = "";
  document.getElementById("rs-exp-form-title").textContent = "Add experience";
  document.getElementById("rs-exp-cancel").hidden = true;
}

async function submitExperience(ev) {
  ev.preventDefault();
  const f = ev.target.elements;
  const body = {
    kind: f.kind.value, title: f.title.value, organization: f.organization.value, location: f.location.value,
    start_date: f.start_date.value || null, end_date: f.end_date.value || null,
    is_current: f.is_current.checked, protected: f.protected.checked,
  };
  try {
    if (f.id.value) await api("PATCH", `/api/experiences/${f.id.value}`, body);
    else await api("POST", "/api/experiences", body);
    banner(f.id.value ? "Experience updated." : "Experience added. Now write its statements.", "ok");
    resetExperienceForm();
    loadExperiences();
    loadResumeLede();
  } catch (err) {
    banner(err.message);
  }
}

// ---- skills

async function loadSkills() {
  try {
    const skills = await api("GET", "/api/skills");
    renderSkills(skills);
  } catch (err) {
    banner(err.message);
  }
}

function renderSkills(skills) {
  const categories = [...new Set(skills.map((s) => s.category))];
  document.getElementById("rs-categories").replaceChildren(...categories.map((c) => h("option", { value: c })));
  document.querySelector("#rs-skill-table tbody").replaceChildren(...skills.map((s) => {
    const cat = h("input", { value: s.category, maxlength: 60, "aria-label": `Category for ${s.name}`, list: "rs-categories",
      onchange: async (ev) => {
        try { renderSkills(await api("PATCH", `/api/skills/${s.id}`, { category: ev.target.value })); }
        catch (err) { banner(err.message); }
      } });
    return h("tr", {},
      h("td", {}, h("strong", {}, s.name)),
      h("td", {}, cat),
      h("td", {}, h("button", { class: "ghost", onclick: async () => {
        try { await api("DELETE", `/api/skills/${s.id}`); loadSkills(); loadResumeLede(); }
        catch (err) { banner(err.message); }
      } }, "Remove")));
  }));
  document.getElementById("rs-skill-count").textContent = skills.length ? plural(skills.length, "skill", "skills") : "No skills yet.";
}

async function submitSkill(ev) {
  ev.preventDefault();
  const f = ev.target.elements;
  try {
    renderSkills(await api("POST", "/api/skills", { name: f.name.value, category: f.category.value || "Technical" }));
    f.name.value = "";
    f.name.focus();
    loadResumeLede();
  } catch (err) {
    banner(err.message);
  }
}

// ---- header

async function loadHeader() {
  try {
    const hdr = (await api("GET", "/api/settings")).resume_header;
    const f = document.getElementById("rs-header-form").elements;
    for (const k of ["name", "email", "phone", "location"]) f[k].value = hdr[k];
    f.links.value = hdr.links.join("\n");
  } catch (err) {
    banner(err.message);
  }
}

async function submitHeader(ev) {
  ev.preventDefault();
  const f = ev.target.elements;
  const resume_header = {
    name: f.name.value, email: f.email.value, phone: f.phone.value, location: f.location.value,
    links: f.links.value.split("\n").map((x) => x.trim()).filter(Boolean),
  };
  try {
    await api("PUT", "/api/settings", { resume_header }); // only this section is replaced
    banner("Contact header saved.", "ok");
  } catch (err) {
    banner(err.message);
  }
}

// ---- generated resumes

async function loadDocuments() {
  try {
    const docs = await api("GET", "/api/resumes");
    document.querySelector("#rs-doc-table tbody").replaceChildren(...docs.map((d) =>
      h("tr", { onclick: () => showDocument(d.id) },
        h("td", {}, h("strong", {}, d.opportunity_title || "General resume"),
          h("span", { class: "sub" }, d.opportunity_employer ? `${d.opportunity_employer} · #${d.id}` : `#${d.id}`)),
        h("td", {}, String(d.page_count)),
        h("td", { class: "deadline" }, fmtDate(d.created_at)))));
    document.getElementById("rs-doc-count").textContent = docs.length
      ? `${plural(docs.length, "resume", "resumes")}. Each one is kept exactly as generated.` : "No resumes generated yet.";
  } catch (err) {
    banner(err.message);
  }
}

async function showDocument(id) {
  const box = document.getElementById("rs-doc-detail");
  try {
    renderDocument(await api("GET", `/api/resumes/${id}`), box);
    box.hidden = false;
  } catch (err) {
    banner(err.message);
  }
}

// ---------------------------------------------------------------- discovery

const HEALTH_TAG = { ok: ["tag ok", "Healthy"], partial: ["tag attention", "Partial list"], stale: ["tag attention", "Stale"],
                     failing: ["tag critical", "Failing"], "never checked": ["tag", "Not checked yet"] };

function ago(iso) {
  if (!iso) return "never";
  const mins = Math.round((Date.now() - new Date(iso)) / 60000);
  if (mins < 2) return "just now";
  if (mins < 120) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  return hours < 48 ? `${hours} h ago` : `${Math.round(hours / 24)} days ago`;
}

async function loadDiscovery() {
  try {
    const [hl, s] = await Promise.all([api("GET", "/api/discovery/health"), api("GET", "/api/settings")]);
    document.getElementById("disc-sync").disabled = !s.discovery_feed_url;
    const counts = {};
    for (const src of hl.sources) counts[src.state] = (counts[src.state] || 0) + 1;
    const trouble = (counts.failing || 0) + (counts.partial || 0) + (counts.stale || 0);
    fill(document.getElementById("disc-lede"),
      `${plural(hl.sources.length, "source", "sources")}: `,
      `${counts.ok || 0} healthy`, trouble ? [", ", h("strong", { class: "attention-text" }, `${trouble} need attention`)] : "",
      counts["never checked"] ? `, ${counts["never checked"]} not checked yet` : "", ". ",
      s.discovery_feed_url ? `Last cloud run: ${ago(hl.last_cloud_run_at)}.` : "No cloud feed set up yet, so discovery runs only when you click Run discovery here.");
    document.querySelector("#disc-table tbody").replaceChildren(...hl.sources.map((src) => {
      const [cls, label] = HEALTH_TAG[src.state];
      return h("tr", {},
        h("td", {}, h("strong", {}, src.employer), h("span", { class: "sub" }, `${src.source} · ${src.source_key}${src.configured ? "" : " · removed from sources.json"}`)),
        h("td", {}, h("span", { class: cls }, label),
          src.last_error ? h("span", { class: "sub", title: src.last_error }, src.last_error.slice(0, 90)) : null),
        h("td", { class: "deadline" }, ago(src.last_checked_at)),
        h("td", {}, `${src.active} open`, src.not_seen ? h("span", { class: "sub" }, `${src.not_seen} not seen`) : null));
    }));
  } catch (err) {
    banner(err.message);
  }
}

function describeImport(r) {
  const parts = [`${plural(r.new, "new opportunity", "new opportunities")}`];
  if (r.updated) parts.push(`${r.updated} updated`);
  if (r.not_seen) parts.push(`${r.not_seen} no longer listed`);
  if (r.failed_checks) parts.push(`${plural(r.failed_checks, "source", "sources")} failed`);
  return parts.join(", ") + ".";
}

async function busy(button, label, work) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = label;
  try { return await work(); } finally { button.disabled = false; button.textContent = original; }
}

async function runDiscovery() {
  await busy(document.getElementById("disc-run"), "Checking boards…", async () => {
    try {
      const r = await api("POST", "/api/discovery/run");
      banner(`Discovery finished: ${describeImport(r)}`, r.failed_checks ? "error" : "ok");
    } catch (err) {
      banner(err.message);
    }
    loadDiscovery();
    loadSummary();
  });
}

async function syncCloud(quiet) {
  try {
    const r = await api("POST", "/api/discovery/sync");
    if (r.errors.length) banner(`Cloud feed: ${r.errors.length} run(s) could not be imported. ${r.errors[0]}`);
    else if (r.new || !quiet) banner(`Cloud feed: ${plural(r.imported.length, "run", "runs")} imported, ${plural(r.new, "new opportunity", "new opportunities")}.`, "ok");
    loadSummary();
    if (location.hash === "#opportunities" || !location.hash) loadOpportunities();
  } catch (err) {
    if (!quiet || err.status !== 409) banner(err.message);
  }
  if (!quiet) loadDiscovery();
}

async function importFeedFile(ev) {
  const file = ev.target.files[0];
  if (!file) return;
  try {
    const r = await api("POST", "/api/discovery/import", { content: await file.text() });
    banner(r.skipped ? "That feed run was already imported." : `Feed imported: ${describeImport(r)}`, "ok");
    loadDiscovery();
    loadSummary();
  } catch (err) {
    banner(err.message);
  } finally {
    ev.target.value = "";
  }
}

// When Apogee opens, pick up anything the cloud found while this computer was off (once per browser session).
async function syncOnOpen() {
  try {
    if (sessionStorage.getItem("apogee-synced")) return;
    sessionStorage.setItem("apogee-synced", "1");
  } catch { /* storage unavailable: sync anyway */ }
  try {
    if ((await api("GET", "/api/settings")).discovery_feed_url) syncCloud(true);
  } catch { /* the page's own requests report connection problems */ }
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
    form.elements.discovery_feed_url.value = s.discovery_feed_url;
    loadDiscovery();
    const p = s.profile;
    for (const f of TRISTATE_FIELDS) form.elements[f].value = p[f] === null ? "" : String(p[f]);
    form.elements.degree_level.value = p.degree_level || "";
    form.elements.graduation.value = p.graduation || "";
    form.elements.gpa.value = p.gpa ?? "";
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
  const body = { backup_mirror_dir: form.elements.backup_mirror_dir.value,
    discovery_feed_url: form.elements.discovery_feed_url.value.trim(), profile: {
    us_citizen: tri("us_citizen"), us_person: tri("us_person"), needs_sponsorship: tri("needs_sponsorship"),
    degree_level: form.elements.degree_level.value || null,
    graduation: form.elements.graduation.value || null,
    gpa: form.elements.gpa.value === "" ? null : Number(form.elements.gpa.value),
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
  if (!confirm("Shut down Apogee? Reopen it any time from the desktop shortcut.")) return;
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
  // The header search always searches opportunities, from any view.
  document.getElementById("f-q").addEventListener("input", () => {
    clearTimeout(searchTimer);
    if (location.hash && location.hash !== "#opportunities") { location.hash = "#opportunities"; return; }
    searchTimer = setTimeout(loadOpportunities, 250);
  });
  for (const id of ["f-state", "f-type", "f-elig", "f-sort"]) document.getElementById(id).addEventListener("change", loadOpportunities);
  document.getElementById("compare-btn").addEventListener("click", showCompare);
  document.getElementById("add-btn").addEventListener("click", () => {
    if (location.hash && location.hash !== "#opportunities") location.hash = "#opportunities";
    openForm(null);
  });
  document.getElementById("opp-form").addEventListener("submit", submitOpportunity);
  document.getElementById("opp-form-cancel").addEventListener("click", closeForm);
  document.getElementById("import-file").addEventListener("change", importFile);
  document.getElementById("a-status").addEventListener("change", loadApplications);
  for (const b of document.querySelectorAll(".subviews button")) b.addEventListener("click", () => showSub(b.dataset.sub));
  document.getElementById("rs-opp").addEventListener("change", (ev) => {
    rs.oppId = ev.target.value; rs.include.clear(); rs.exclude.clear(); loadPlan();
  });
  document.getElementById("rs-generate-btn").addEventListener("click", generateResume);
  document.getElementById("rs-exp-form").addEventListener("submit", submitExperience);
  document.getElementById("rs-exp-cancel").addEventListener("click", resetExperienceForm);
  document.getElementById("rs-skill-form").addEventListener("submit", submitSkill);
  document.getElementById("rs-header-form").addEventListener("submit", submitHeader);
  for (const a of document.querySelectorAll("[data-open-sub]")) a.addEventListener("click", () => { rs.sub = a.dataset.openSub; });
  document.getElementById("disc-run").addEventListener("click", runDiscovery);
  document.getElementById("disc-sync").addEventListener("click", () => syncCloud(false));
  document.getElementById("disc-file").addEventListener("change", importFeedFile);
  setupSettingsForm();
  document.getElementById("settings-form").addEventListener("submit", submitSettings);
  document.getElementById("shutdown").addEventListener("click", shutdown);
  showTab();
  syncOnOpen();
});
