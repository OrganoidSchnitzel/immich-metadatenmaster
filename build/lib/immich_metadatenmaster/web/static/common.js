"use strict";

async function api(method, url, body) {
  const resp = await fetch(url, {
    method,
    headers: body === undefined ? { "X-Metadatenmaster": "1" } : { "Content-Type": "application/json", "X-Metadatenmaster": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = resp.headers.get("content-type")?.includes("json") ? await resp.json() : await resp.text();
  if (resp.status === 401 && data && data.login && location.pathname !== "/login") {
    // The session ran out (or the password was changed elsewhere).
    location.href = `/login?next=${encodeURIComponent(location.pathname + location.search)}`;
  }
  if (!resp.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail || data));
  }
  return data;
}

/** An icon from the sprite in the page (Material Design Icons). */
function icon(name) {
  return `<svg class="i" aria-hidden="true"><use href="#i-${name}"/></svg>`;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

/** Thrown by followJob when the user cancelled the job. */
class JobCancelled extends Error {
  constructor() { super("Cancelled."); this.cancelled = true; }
}

// --- background jobs -----------------------------------------------------------------------

const cancelling = new Set();

/** Poll a background job, rendering its progress (with a Cancel button) into `el`. Resolves with its result. */
async function followJob(job, el, label) {
  label = label || job.label;
  el.hidden = false;
  el.classList.remove("failed");
  refreshJobPill();
  for (;;) {
    const pct = job.total ? Math.round((100 * job.done) / job.total) : 0;
    const stopping = cancelling.has(job.id);
    el.innerHTML = `<div class="job-line">${esc(label)}: ${job.status === "queued" ? "waiting for another job…" : `${job.done} / ${job.total || "?"}`}
        ${job.cancellable ? `<button type="button" class="link" data-cancel-job="${esc(job.id)}"${stopping ? " disabled" : ""}>${stopping ? "Cancelling…" : "Cancel"}</button>` : ""}</div>
      <div class="bar"><span style="width:${pct}%"></span></div>`;
    if (job.status === "done") { el.hidden = true; refreshJobPill(); return job.result; }
    if (job.status === "cancelled") {
      el.textContent = `${label}: cancelled. What was done until then is kept (see History).`;
      refreshJobPill();
      throw new JobCancelled();
    }
    if (job.status === "failed") {
      el.classList.add("failed");
      el.textContent = `${label} failed: ${job.error}`;
      refreshJobPill();
      throw new Error(job.error);
    }
    await sleep(800);
    job = await api("GET", `/api/jobs/${job.id}`);
  }
}

document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("[data-cancel-job]");
  if (!btn) return;
  cancelling.add(btn.dataset.cancelJob);
  btn.disabled = true;
  btn.textContent = "Cancelling…";
  try {
    await api("POST", `/api/jobs/${btn.dataset.cancelJob}/cancel`);
  } catch (e) {
    alert(e.message);
  }
});

/** If a job matching `match` is still running (e.g. after a page reload), show it again and call `onDone`. */
async function resumeJob(match, el, onDone) {
  let list;
  try { list = await api("GET", "/api/jobs"); } catch (e) { return; }
  const job = list.find(match);
  if (!job) return;
  let result = null;
  try {
    result = await followJob(job, el, job.label);
  } catch (e) {
    if (!e.cancelled) return;
  }
  await onDone(result, job);
}

let pillTimer = null;

/** The running job in the top bar, on every page. */
async function refreshJobPill() {
  const pill = document.getElementById("job-pill");
  if (!pill) return;
  clearTimeout(pillTimer);
  let list = [];
  try { list = await api("GET", "/api/jobs"); } catch (e) { return; }
  if (!list.length) { pill.hidden = true; return; }
  const j = list.find(x => x.status === "running") || list[0];
  const href = j.meta.href || (j.meta.album_id ? `/album/${encodeURIComponent(j.meta.album_id)}`
    : j.meta.page ? `/${j.meta.page}` : "");
  const text = `${icon("progress")} ${esc(j.label)} ${j.status === "queued" ? "(waiting)" : `${j.done}/${j.total || "?"}`}`;
  pill.innerHTML = (href ? `<a href="${esc(href)}">${text}</a>` : text) +
    (list.length > 1 ? ` <span class="muted">+${list.length - 1} waiting</span>` : "");
  pill.hidden = false;
  pillTimer = setTimeout(refreshJobPill, 2000);
}

// --- selecting photos ----------------------------------------------------------------------

/** Click toggles a photo; shift-click selects all photos from the last clicked one to this one. */
function makeSelector(selected, orderedIds, onChange) {
  let anchor = null;
  return function select(id, ev) {
    if (ev && ev.shiftKey && anchor && anchor !== id) {
      const ids = orderedIds();
      const a = ids.indexOf(anchor), b = ids.indexOf(id);
      if (a >= 0 && b >= 0) {
        const [lo, hi] = a < b ? [a, b] : [b, a];
        ids.slice(lo, hi + 1).forEach(x => selected.add(x));
        anchor = id;
        onChange();
        return;
      }
    }
    selected.has(id) ? selected.delete(id) : selected.add(id);
    anchor = id;
    onChange();
  };
}

/** Single-key shortcuts (a, Escape, v …), ignored while typing in a field or when the viewer is open. */
function onShortcuts(handlers) {
  document.addEventListener("keydown", (ev) => {
    if (ev.ctrlKey || ev.metaKey || ev.altKey || viewerState.open) return;
    if (ev.target.closest("input, textarea, select, [contenteditable]")) return;
    const fn = handlers[ev.key] || handlers[ev.key.toLowerCase()];
    if (fn) {
      ev.preventDefault();
      fn(ev);
    }
  });
}

// --- large view ----------------------------------------------------------------------------

const viewerState = { open: false, ids: [], index: 0, opts: null, el: null };

/**
 * Show photos large: ← → to browse, Space to select, Esc to close.
 * opts: { ids, index, describe(id) → {title, html, video}, isSelected(id), toggle(id), immichUrl }
 */
function openViewer(opts) {
  if (!opts.ids.length) return;
  if (!viewerState.el) {
    const el = document.createElement("div");
    el.className = "viewer";
    el.hidden = true;
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-modal", "true");
    el.innerHTML = `
      <button type="button" class="viewer-close" data-v="close" aria-label="Close (Esc)">${icon("close")}</button>
      <button type="button" class="viewer-nav prev" data-v="prev" aria-label="Previous (←)">${icon("left")}</button>
      <figure><img alt=""><span class="viewer-video" hidden>${icon("play")} Video: open it in Immich to play</span></figure>
      <button type="button" class="viewer-nav next" data-v="next" aria-label="Next (→)">${icon("right")}</button>
      <div class="viewer-info"></div>`;
    document.body.appendChild(el);
    el.addEventListener("click", (ev) => {
      const action = ev.target.closest("[data-v]")?.dataset.v;
      if (action === "close" || ev.target === el) closeViewer();
      else if (action === "prev") moveViewer(-1);
      else if (action === "next") moveViewer(1);
      else if (action === "select") { viewerState.opts.toggle(currentId()); renderViewer(); }
    });
    document.addEventListener("keydown", (ev) => {
      if (!viewerState.open) return;
      if (ev.key === "Escape") closeViewer();
      else if (ev.key === "ArrowLeft") moveViewer(-1);
      else if (ev.key === "ArrowRight") moveViewer(1);
      else if (ev.key === " ") { viewerState.opts.toggle(currentId()); renderViewer(); }
      else return;
      ev.preventDefault();
    });
    viewerState.el = el;
  }
  viewerState.opts = opts;
  viewerState.ids = opts.ids;
  viewerState.index = Math.max(0, Math.min(opts.index || 0, opts.ids.length - 1));
  viewerState.open = true;
  viewerState.el.hidden = false;
  document.body.classList.add("no-scroll");
  renderViewer();
}

function currentId() { return viewerState.ids[viewerState.index]; }

function closeViewer() {
  viewerState.open = false;
  if (viewerState.el) viewerState.el.hidden = true;
  document.body.classList.remove("no-scroll");
}

function moveViewer(step) {
  const n = viewerState.ids.length;
  viewerState.index = (viewerState.index + step + n) % n;
  renderViewer();
}

function renderViewer() {
  const { el, opts } = viewerState;
  const id = currentId();
  const d = opts.describe(id) || {};
  el.querySelector("img").src = `/thumb/${encodeURIComponent(id)}?size=preview`;
  el.querySelector(".viewer-video").hidden = !d.video;
  const sel = opts.isSelected(id);
  el.querySelector(".viewer-info").innerHTML = `
    <div class="viewer-title"><strong>${esc(d.title || "")}</strong>
      <span class="muted">${viewerState.index + 1} / ${viewerState.ids.length}</span></div>
    ${d.html || ""}
    <div class="row">
      <button type="button" data-v="select" class="${sel ? "primary" : ""}">${sel ? "✓ Selected" : "Select"} <span class="muted">(Space)</span></button>
      ${opts.immichUrl ? `<a href="${esc(opts.immichUrl)}/photos/${esc(id)}" target="_blank" rel="noopener">Open in Immich ${icon("open")}</a>` : ""}
    </div>`;
  // Load the neighbours in the background so that browsing is quick.
  for (const k of [-1, 1]) {
    const n = viewerState.ids[(viewerState.index + k + viewerState.ids.length) % viewerState.ids.length];
    new Image().src = `/thumb/${encodeURIComponent(n)}?size=preview`;
  }
}

document.getElementById("logout")?.addEventListener("click", async (ev) => {
  ev.preventDefault();
  try { await api("POST", "/api/logout"); } catch (e) { /* logged out either way */ }
  location.href = "/login";
});

// --- theme and menu ------------------------------------------------------------------------

/** Light or dark: follows the system until the user picks one, which is remembered in this browser. */
document.getElementById("theme-toggle")?.addEventListener("click", () => {
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("metadatenmaster.theme", root.dataset.theme); } catch (e) { /* not remembered */ }
});

/** The sidebar slides in on narrow screens. */
function setNav(open) {
  document.body.classList.toggle("nav-open", open);
  document.getElementById("nav-toggle")?.setAttribute("aria-expanded", String(open));
  const scrim = document.getElementById("scrim");
  if (scrim) scrim.hidden = !open;
}
document.getElementById("nav-toggle")?.addEventListener("click", () => setNav(!document.body.classList.contains("nav-open")));
document.getElementById("scrim")?.addEventListener("click", () => setNav(false));
document.addEventListener("keydown", ev => { if (ev.key === "Escape" && document.body.classList.contains("nav-open")) setNav(false); });

refreshJobPill();
