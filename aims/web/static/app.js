"use strict";
// All data from the server, especially job text and URLs, is untrusted.
// Render with textContent only; never innerHTML.

const LIST_FIELDS = ["preferred_employers", "preferred_locations", "disciplines", "internship_terms"];

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
  // FastAPI validation errors
  return data.detail.map((d) => `${d.loc.slice(1).join(".")}: ${d.msg}`).join("; ");
}

function banner(message, kind = "error") {
  const el = document.getElementById("banner");
  el.textContent = message;
  el.className = `banner ${kind}`;
  el.hidden = false;
  if (kind !== "error") setTimeout(() => (el.hidden = true), 3000);
}

function el(tag, text, attrs = {}) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

function safeLink(url, text) {
  // Only http(s) links are ever clickable.
  let ok = false;
  try { ok = ["http:", "https:"].includes(new URL(url).protocol); } catch { ok = false; }
  if (!ok) return el("span", text);
  return el("a", text, { href: url, target: "_blank", rel: "noopener noreferrer" });
}

const fmtDate = (iso) => (iso ? iso.slice(0, 10) : "");

// ---- Tabs ----
function showTab() {
  const name = (location.hash || "#opportunities").slice(1);
  for (const sec of document.querySelectorAll(".tab")) sec.hidden = sec.id !== `tab-${name}`;
  for (const a of document.querySelectorAll("nav a")) a.classList.toggle("active", a.dataset.tab === name);
  if (name === "opportunities") loadOpportunities();
  if (name === "settings") loadSettings();
}

// ---- Opportunities ----
async function loadOpportunities() {
  try {
    const items = await api("GET", "/api/opportunities");
    const tbody = document.querySelector("#opp-table tbody");
    tbody.replaceChildren();
    for (const o of items) {
      const tr = el("tr");
      tr.append(el("td", o.employer));
      const titleCell = el("td");
      titleCell.append(safeLink(o.url, o.title));
      tr.append(titleCell, el("td", o.location), el("td", o.opportunity_type), el("td", o.deadline || ""), el("td", fmtDate(o.first_seen_at)));
      tr.addEventListener("click", (ev) => { if (ev.target.tagName !== "A") showDetail(o.id); });
      tbody.append(tr);
    }
    document.getElementById("opp-empty").hidden = items.length > 0;
  } catch (err) {
    banner(err.message);
  }
}

async function showDetail(id) {
  try {
    const o = await api("GET", `/api/opportunities/${id}`);
    const box = document.getElementById("opp-detail");
    const head = el("h3");
    head.append(safeLink(o.url, `${o.title} | ${o.employer}`));
    const meta = el("p", `Source: ${o.source} · first seen ${fmtDate(o.first_seen_at)} · last verified ${fmtDate(o.last_seen_at)} · listing ${o.listing_status}`, { class: "muted" });
    box.replaceChildren(head, meta, el("pre", o.description || "(no description)"));
    if (o.user_notes) box.append(el("h4", "Notes"), el("pre", o.user_notes));
    box.hidden = false;
  } catch (err) {
    banner(err.message);
  }
}

async function submitOpportunity(ev) {
  ev.preventDefault();
  const form = ev.target;
  const body = Object.fromEntries(new FormData(form).entries());
  if (!body.deadline) body.deadline = null;
  try {
    await api("POST", "/api/opportunities", body);
    form.reset();
    banner("Opportunity added.", "ok");
    loadOpportunities();
  } catch (err) {
    if (err.status === 409) {
      banner(`Already tracked: ${err.message}`);
      showDetail(err.data.existing_id);
    } else {
      banner(err.message);
    }
  }
}

// ---- Settings ----
async function loadSettings() {
  try {
    const [settings, health] = await Promise.all([api("GET", "/api/settings"), api("GET", "/api/health")]);
    const form = document.getElementById("settings-form");
    for (const f of LIST_FIELDS) form.elements[f].value = settings[f].join("\n");
    form.elements.backup_mirror_dir.value = settings.backup_mirror_dir;
    const dl = document.getElementById("health");
    dl.replaceChildren();
    for (const [k, v] of [["Version", health.version], ["Schema version", health.schema_version], ["Database check", health.database], ["Data folder", health.data_dir]]) {
      dl.append(el("dt", k), el("dd", String(v)));
    }
  } catch (err) {
    banner(err.message);
  }
}

async function submitSettings(ev) {
  ev.preventDefault();
  const form = ev.target;
  const body = { backup_mirror_dir: form.elements.backup_mirror_dir.value };
  for (const f of LIST_FIELDS) {
    body[f] = form.elements[f].value.split("\n").map((s) => s.trim()).filter(Boolean);
  }
  try {
    await api("PUT", "/api/settings", body);
    banner("Preferences saved.", "ok");
  } catch (err) {
    banner(err.message);
  }
}

async function shutdown() {
  if (!confirm("Shut down the Internship Manager server? Reopen it from the desktop shortcut.")) return;
  try {
    await api("POST", "/api/shutdown");
    document.body.replaceChildren(el("p", "The server has shut down. You can close this tab.", { class: "panel" }));
  } catch (err) {
    banner(err.message);
  }
}

window.addEventListener("hashchange", showTab);
document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("opp-form").addEventListener("submit", submitOpportunity);
  document.getElementById("settings-form").addEventListener("submit", submitSettings);
  document.getElementById("shutdown").addEventListener("click", shutdown);
  showTab();
});
