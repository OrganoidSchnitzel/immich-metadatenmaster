"use strict";

const app = document.getElementById("app");
const immichUrl = app.dataset.immich;
const checkForm = document.getElementById("check-form");
const resultsEl = document.getElementById("results");
const KIND_ICON = { clock: "clock", move_track: "route", move_nearby: "map", local_time: "gps", add_track: "locations" };
const SHOWN = 48;  // photo cards per finding before "Show all"
// finding id -> selected asset ids (all at first), and findings showing all their photos.
const chosen = new Map();
const expanded = new Set();
let data = null;

function plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }

function fmtWhen(iso) {
  if (!iso) return "–";
  return iso.replace("T", " ").slice(0, 16);
}

function fmtDay(iso) {
  try {
    return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  } catch (e) { return iso.slice(0, 10); }
}

function fmtKm(m) { return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`; }

function clockWords(c) {
  if (c.offset_s === null) {
    return c.moments ? "can't tell (too few moments shared with phone photos)" : "can't tell (no phone photos of the same moments)";
  }
  if (c.offset_s === 0) return "right";
  const m = Math.floor(Math.abs(c.offset_s) / 60);
  const span = m >= 60 ? `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min` : `${m} min`;
  return `${span} ${c.offset_s > 0 ? "behind" : "ahead"}`;
}

/** What a fix changes on one photo. */
function changeHtml(f, it) {
  const b = it.before, a = it.after;
  if (f.kind === "clock" || f.kind === "local_time") {
    const sameDay = (b.local || "").slice(0, 10) === a.local.slice(0, 10);
    return `<div class="change">${esc(fmtWhen(b.local))} → <strong>${esc(sameDay ? a.local.slice(11, 16) : fmtWhen(a.local))}</strong></div>
      <div class="muted small">${esc(a.time_zone || "")}</div>`;
  }
  if (f.kind === "add_track") return `<div class="change">on <strong>${esc(a.detail || "the track")}</strong></div>`;
  return `<div class="change">moves <strong>${esc(fmtKm(it.distance_m))}</strong></div>
    <div class="muted small">${esc(f.kind === "move_track" ? `onto ${a.detail}` : `to where ${a.detail} was taken`)}</div>`;
}

function cardHtml(f, it) {
  const on = chosen.get(f.id).has(it.asset_id);
  return `<article class="card ${on ? "selected" : ""}" data-finding="${esc(f.id)}" data-id="${esc(it.asset_id)}">
      <img src="/thumb/${esc(it.asset_id)}" alt="" loading="lazy">
      <span class="check"></span>
      ${it.type === "VIDEO" ? `<span class="kind" title="Video">${icon("play")}</span>` : ""}
      <a class="open" href="${esc(immichUrl)}/photos/${esc(it.asset_id)}" target="_blank" rel="noopener" title="Open in Immich" aria-label="Open in Immich">${icon("open")}</a>
      <div class="body"><div class="file" title="${esc(it.file_name)}">${esc(it.file_name)}</div>${changeHtml(f, it)}</div>
    </article>`;
}

function evidenceHtml(f) {
  if (f.kind !== "clock" || !f.examples || !f.examples.length) return "";
  return `<div class="clock-pairs">${f.examples.slice(0, 4).map(e => `
    <figure class="clock-pair" title="${esc(e.photo.file_name)} ↔ ${esc(e.reference.file_name)} (${esc(e.reference.camera)})">
      <img src="/thumb/${esc(e.photo.id)}" alt="" loading="lazy"><img src="/thumb/${esc(e.reference.id)}" alt="" loading="lazy">
      <figcaption>camera ${esc((e.photo.local || "").slice(11, 16))} · phone ${esc((e.reference.local || "").slice(11, 16))}</figcaption>
    </figure>`).join("")}</div>`;
}

function findingHtml(f) {
  if (!chosen.has(f.id)) chosen.set(f.id, new Set(f.items.map(it => it.asset_id)));
  const sel = chosen.get(f.id);
  const all = expanded.has(f.id);
  const items = all ? f.items : f.items.slice(0, SHOWN);
  const verb = { clock: "Correct the times", local_time: "Set the local time", add_track: "Add the locations" }[f.kind] || "Move them";
  return `<section class="panel finding" data-finding="${esc(f.id)}">
      <div class="finding-head">
        <span class="finding-icon">${icon(KIND_ICON[f.kind] || "info")}</span>
        <div class="finding-text">
          <h3>${esc(f.title)}</h3>
          <p>${esc(f.text)}</p>
          ${f.confidence !== "high" ? `<p class="conf-${esc(f.confidence)} small">${esc(f.confidence)} confidence – look at the photos before fixing.</p>` : ""}
        </div>
      </div>
      ${evidenceHtml(f)}
      <div class="row finding-actions">
        <button type="button" class="primary" data-fix ${sel.size ? "" : "disabled"}>${esc(verb)} (${sel.size})</button>
        <button type="button" data-dismiss>Leave ${sel.size === f.items.length ? "all" : "selected"} as is</button>
        <span class="muted small">Select: <button type="button" class="link" data-all>all</button> ·
          <button type="button" class="link" data-none>none</button></span>
      </div>
      <div class="job" data-job hidden></div>
      <div class="grid compact">${items.map(it => cardHtml(f, it)).join("")}</div>
      ${!all && f.items.length > SHOWN ? `<button type="button" class="link" data-more>Show all ${f.items.length} photos</button>` : ""}
    </section>`;
}

function render() {
  if (!data || !data.created_at) {
    resultsEl.innerHTML = "";
    return;
  }
  const byTrip = new Map();
  for (const f of data.findings) {
    if (!byTrip.has(f.trip)) byTrip.set(f.trip, []);
    byTrip.get(f.trip).push(f);
  }
  const total = data.findings.reduce((n, f) => n + f.count, 0);
  const head = `<p class="muted check-summary">Checked ${plural(data.trips.length, "trip")} on ${esc(new Date(data.created_at).toLocaleString())}.
    ${total ? `${plural(data.findings.length, "finding")} to look at.` : "Nothing (left) to fix."}</p>`;
  resultsEl.innerHTML = head + data.trips.map((trip, n) => {
    const findings = byTrip.get(n) || [];
    const cams = trip.cameras.map(c => `<span class="chip clock-${c.offset_s === null ? "unknown" : c.offset_s === 0 ? "ok" : "off"}"
        title="${esc(plural(c.photos, "photo"))} · ${esc(plural(c.moments, "moment"))} shared with phone photos">
        ${icon("camera")}${esc(c.camera)}: clock ${esc(clockWords(c))}</span>`).join("");
    return `<div class="trip">
        <h2>${esc(trip.label)} <span class="muted">${esc(fmtDay(trip.start))}${fmtDay(trip.start) !== fmtDay(trip.end) ? ` – ${esc(fmtDay(trip.end))}` : ""}</span></h2>
        <div class="trip-cams">${cams || `<span class="muted">Only phone photos: their clocks set themselves.</span>`}</div>
        ${findings.length ? findings.map(findingHtml).join("") : `<p class="ok-line">${icon("check-circle")} Nothing to fix here.</p>`}
      </div>`;
  }).join("");
}

async function load() {
  data = await api("GET", "/api/check");
  render();
}

function finding(id) { return data.findings.find(f => f.id === id); }

resultsEl.addEventListener("click", async (ev) => {
  const box = ev.target.closest("[data-finding]");
  if (!box || ev.target.closest("a.open")) return;
  const f = finding(box.closest("section.finding").dataset.finding);
  const sel = chosen.get(f.id);
  const card = ev.target.closest("article.card");
  if (card) {
    sel.has(card.dataset.id) ? sel.delete(card.dataset.id) : sel.add(card.dataset.id);
    render();
    return;
  }
  if (ev.target.closest("[data-all]")) { f.items.forEach(it => sel.add(it.asset_id)); render(); }
  if (ev.target.closest("[data-none]")) { sel.clear(); render(); }
  if (ev.target.closest("[data-more]")) { expanded.add(f.id); render(); }
  if (ev.target.closest("[data-dismiss]")) {
    const ids = sel.size === f.items.length ? null : [...sel];
    if (ids && !ids.length) return;
    await api("POST", "/api/check/dismiss", { finding_id: f.id, asset_ids: ids });
    chosen.delete(f.id);
    await load();
  }
  if (ev.target.closest("[data-fix]")) {
    const section = box.closest("section.finding");
    section.querySelectorAll("button").forEach(b => { b.disabled = true; });
    try {
      const job = await api("POST", "/api/check/fix", { finding_id: f.id, asset_ids: [...sel] });
      await followJob(job, section.querySelector("[data-job]"), f.title);
      chosen.delete(f.id);
      await load();
    } catch (e) {
      if (!e.cancelled) alert(e.message);
      await load();
    }
  }
});

checkForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = checkForm.elements;
  const errEl = document.getElementById("check-error");
  errEl.hidden = true;
  try {
    const job = await api("POST", "/api/check", {
      use_tracks: f.use_tracks.checked, start: f.start.value || null, end: f.end.value || null,
      camera_tz: f.camera_tz.value.trim() || null,
    });
    await followJob(job, document.getElementById("check-job"), "Checking");
    chosen.clear();
    expanded.clear();
    await load();
  } catch (e) {
    if (e.cancelled) return;
    errEl.textContent = e.message;
    errEl.hidden = false;
  }
});

try {
  document.getElementById("timezones").innerHTML =
    Intl.supportedValuesOf("timeZone").map(z => `<option value="${esc(z)}">`).join("");
} catch (e) { /* older browsers: free text only */ }

api("GET", "/api/tracks").then(tracks => {
  const timed = tracks.filter(t => t.start);
  document.getElementById("track-count").textContent = timed.length
    ? `(${plural(timed.length, "track")}, ${fmtDay(timed.map(t => t.start).sort()[0])} – ${fmtDay(timed.map(t => t.end).sort().at(-1))})`
    : "(no tracks with times yet: upload them on the Locations page)";
  if (!timed.length) checkForm.elements.use_tracks.checked = false;
}).catch(() => {});
load().catch(e => { resultsEl.innerHTML = `<div class="notice error">${esc(e.message)}</div>`; });
resumeJob(j => j.kind === "check", document.getElementById("check-job"), load);
resumeJob(j => j.kind === "check-fix", document.getElementById("check-job"), load);
