"use strict";

const app = document.getElementById("app");
const immichUrl = app.dataset.immich;
const form = document.getElementById("find-form");
const grid = document.getElementById("geo-grid");
const review = document.getElementById("review");
const selCount = document.getElementById("sel-count");
const applyBtn = document.getElementById("geo-apply");
const placeBtn = document.getElementById("geo-place");
const discardBtn = document.getElementById("geo-discard");
const cameraFilter = document.getElementById("camera-filter");
const noLocationBtn = document.getElementById("geo-no-location");

const SOURCE_LABEL = { gpx: "GPX track", sibling: "Same-name file", nearby: "Nearby photo", map: "Placed by you" };
const STORE_KEY = "metadatenmaster.locations";

let tracks = [];
let run = null;
let items = [];
const selected = new Set();
let placing = false;

// --- small helpers -------------------------------------------------------------------------

function loadPrefs() {
  try { return JSON.parse(localStorage.getItem(STORE_KEY) || "{}"); } catch (e) { return {}; }
}

function savePrefs(update) {
  try { localStorage.setItem(STORE_KEY, JSON.stringify({ ...loadPrefs(), ...update })); } catch (e) { /* private mode */ }
}

/** "+1:02:03", "-0:30", "1:00" or a number of minutes → seconds; NaN if invalid. */
function parseOffset(text) {
  const s = String(text || "").trim().replace(/\s+/g, "");
  if (!s) return 0;
  let m = s.match(/^([+-]?)(\d+):([0-5]?\d)(?::([0-5]?\d))?$/);
  if (m) return (m[1] === "-" ? -1 : 1) * (Number(m[2]) * 3600 + Number(m[3]) * 60 + Number(m[4] || 0));
  m = s.match(/^([+-]?\d+(?:\.\d+)?)(m|min)?$/i);
  if (m) return Math.round(Number(m[1]) * 60);
  return NaN;
}

function formatOffset(seconds) {
  const sign = seconds < 0 ? "-" : "+";
  const s = Math.abs(seconds);
  return `${sign}${Math.floor(s / 3600)}:${String(Math.floor(s % 3600 / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function cameraTz() {
  return form.elements.camera_tz.value.trim() || app.dataset.tz || "UTC";
}

function fmtInstant(iso) {
  if (!iso) return "–";
  try {
    return new Date(iso).toLocaleString(undefined, { timeZone: cameraTz(), dateStyle: "medium", timeStyle: "short" });
  } catch (e) {
    return iso.replace("T", " ").slice(0, 16);
  }
}

function fmtLocal(iso) { return iso ? iso.replace("T", " ").slice(0, 19) : "–"; }

/** For photos without a time zone: when they were taken, as the clock time at the suggested place. */
function placeTimeHtml(it) {
  if (!it.place_tz || !it.instant) return "";
  let t;
  try {
    const p = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
      timeZone: it.place_tz, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    }).formatToParts(new Date(it.instant)).map(x => [x.type, x.value]));
    t = `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`;
  } catch (e) {
    return "";
  }
  if (t === fmtLocal(it.corrected || it.local)) return "";
  return `<div class="muted" title="Written with “Also set the local time of the place”">Local time there: ${esc(t)} (${esc(it.place_tz)})</div>`;
}

function fmtDiff(seconds) {
  if (seconds < 60) return `${Math.round(seconds)} s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

/** A datetime-local value ("2024-07-01T10:00") for an instant, in the camera time zone. */
function toLocalInput(iso) {
  try {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: cameraTz(), year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).formatToParts(new Date(iso));
    const p = Object.fromEntries(parts.map(x => [x.type, x.value]));
    return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
  } catch (e) {
    return "";
  }
}

// --- map -----------------------------------------------------------------------------------

// Canvas instead of SVG: stays fast with thousands of photo markers.
const map = L.map("map", { zoomControl: true, preferCanvas: true }).setView([48, 10], 5);
L.tileLayer(app.dataset.tiles, { maxZoom: 19, attribution: app.dataset.attribution }).addTo(map);
const trackLayer = L.layerGroup().addTo(map);
const locatedLayer = L.layerGroup().addTo(map);
const suggestionLayer = L.layerGroup().addTo(map);
const markers = new Map();
const TRACK_COLORS = ["#4250af", "#c2410c", "#0f766e", "#a21caf", "#b45309", "#15803d", "#be123c", "#0369a1",
  "#7c3aed", "#4d7c0f", "#db2777", "#0e7490"];
const css = getComputedStyle(document.documentElement);
const SOURCE_COLOR = {
  gpx: css.getPropertyValue("--src-gpx").trim() || "#2563eb",
  sibling: css.getPropertyValue("--src-sibling").trim() || "#9333ea",
  nearby: css.getPropertyValue("--src-nearby").trim() || "#ea580c",
  map: css.getPropertyValue("--src-map").trim() || "#16a34a",
};

/** A colour per track that stays the same when other tracks are added or deleted. */
function trackColor(id) {
  let h = 2166136261;  // FNV-1a
  for (const c of id) h = Math.imul(h ^ c.charCodeAt(0), 16777619) >>> 0;
  return TRACK_COLORS[h % TRACK_COLORS.length];
}

function drawTracks() {
  trackLayer.clearLayers();
  tracks.forEach(t => {
    if (!t.line.length) return;
    const on = isTicked(t.id);
    L.polyline(t.line, { color: trackColor(t.id), weight: on ? 4 : 2, opacity: on ? 0.85 : 0.35 })
      .bindTooltip(esc(t.name)).addTo(trackLayer);
  });
}

function drawSuggestions() {
  locatedLayer.clearLayers();
  suggestionLayer.clearLayers();
  markers.clear();
  for (const [lat, lon] of (run && run.located) || []) {
    L.circleMarker([lat, lon], { radius: 3, color: "#888", weight: 1, fillOpacity: 0.5 }).addTo(locatedLayer);
  }
  for (const it of visibleItems()) {
    if (it.status !== "ok") continue;
    const sel = selected.has(it.id);
    const m = L.circleMarker([it.lat, it.lon], {
      radius: sel ? 9 : 6, color: sel ? "#000" : "#fff", weight: 2,
      fillColor: SOURCE_COLOR[it.source] || "#2563eb", fillOpacity: 0.9,
    }).bindTooltip(`${esc(it.file_name)}<br>${esc(fmtLocal(it.local))}<br><span class="muted">Drag to move</span>`)
      .addTo(suggestionLayer);
    m.on("click", (ev) => {
      if (placing || Date.now() - lastDragEnd < 400) return;
      select(it.id, ev.originalEvent);
      const card = grid.querySelector(`[data-id="${CSS.escape(it.id)}"]`);
      if (card) card.scrollIntoView({ behavior: "smooth", block: "center" });
    });
    m.on("mousedown", (ev) => {
      if (placing) return;
      dragging = { id: it.id, marker: m, start: m.getLatLng(), moved: false };
      map.dragging.disable();
      L.DomEvent.stop(ev);
    });
    markers.set(it.id, m);
  }
}

// --- dragging a dot moves that photo ---------------------------------------------------------

let dragging = null;
let lastDragEnd = 0;

// Document-level events: with the canvas renderer the map does not report moves over the canvas.
document.addEventListener("mousemove", (ev) => {
  if (!dragging) return;
  ev.preventDefault();  // no text selection while dragging
  dragging.moved = true;
  dragging.marker.setLatLng(map.mouseEventToLatLng(ev));
});

async function endDrag() {
  if (!dragging) return;
  const d = dragging;
  dragging = null;
  map.dragging.enable();
  if (!d.moved) return;
  lastDragEnd = Date.now();
  const p = d.marker.getLatLng();
  try {
    await api("POST", "/api/geo/place", { asset_ids: [d.id], lat: p.lat, lon: p.lng, label: "Moved on the map" });
    await loadSuggestions();
  } catch (e) {
    d.marker.setLatLng(d.start);
    alert(e.message);
  }
}

document.addEventListener("mouseup", endDrag);

function fitMap() {
  const bounds = L.latLngBounds([]);
  tracks.filter(t => isTicked(t.id)).forEach(t => t.line.forEach(p => bounds.extend(p)));
  items.filter(it => it.status === "ok").forEach(it => bounds.extend([it.lat, it.lon]));
  if (bounds.isValid()) map.fitBounds(bounds, { padding: [30, 30], maxZoom: 15 });
}

map.on("click", async (ev) => {
  if (!placing) return;
  const ids = visibleSelectedIds();
  setPlacing(false);
  if (!ids.length) return;
  try {
    await api("POST", "/api/geo/place", { asset_ids: ids, lat: ev.latlng.lat, lon: ev.latlng.lng, label: "" });
    await loadSuggestions();
  } catch (e) {
    alert(e.message);
  }
});

// --- address search on the map ----------------------------------------------------------------

let searchMarker = null;

const SearchControl = L.Control.extend({
  options: { position: "topright" },
  onAdd() {
    const box = L.DomUtil.create("div", "map-search leaflet-bar");
    box.innerHTML = `<input type="search" placeholder="${app.dataset.geocoder ? "Search a place or address…" : "Coordinates, e.g. 47.42, 10.98"}"
        aria-label="Search a place" autocomplete="off"><ul class="map-search-results" hidden></ul>`;
    L.DomEvent.disableClickPropagation(box);
    L.DomEvent.disableScrollPropagation(box);
    return box;
  },
});
const searchBox = new SearchControl().addTo(map).getContainer();
const searchInput = searchBox.querySelector("input");
const searchList = searchBox.querySelector("ul");
let searchResults = [];
let searchTimer = null;
let searchSeq = 0;

async function runSearch(text) {
  const seq = ++searchSeq;
  const c = map.getCenter();
  const params = new URLSearchParams({ q: text, lang: navigator.language || "en", lat: c.lat, lon: c.lng });
  try {
    const data = await api("GET", `/api/geocode?${params}`);
    if (seq !== searchSeq) return;
    searchResults = data.results;
    searchList.innerHTML = searchResults.length
      ? searchResults.map((r, i) => `<li data-i="${i}"><strong>${esc(r.name)}</strong><span>${esc(r.detail)}</span></li>`).join("")
      : `<li class="muted">Nothing found.</li>`;
  } catch (e) {
    if (seq !== searchSeq) return;
    searchResults = [];
    searchList.innerHTML = `<li class="err">${esc(e.message)}</li>`;
  }
  searchList.hidden = false;
}

function placeLabel(r) { return r.detail && r.kind !== "coordinates" ? `${r.name}, ${r.detail}` : r.name; }

function showResult(r) {
  searchList.hidden = true;
  if (r.bounds) map.fitBounds(r.bounds, { maxZoom: 17 }); else map.setView([r.lat, r.lon], 16);
  if (searchMarker) searchMarker.remove();
  searchMarker = L.marker([r.lat, r.lon]).addTo(map);
  const div = L.DomUtil.create("div", "search-popup");
  div.innerHTML = `<strong>${esc(r.name)}</strong><div class="muted">${esc(r.detail)}</div><button type="button" class="primary"></button>`;
  const btn = div.querySelector("button");
  const refresh = () => {
    const n = visibleSelectedIds().length;
    btn.disabled = !n;
    btn.textContent = n ? `Place ${n} selected photo${n === 1 ? "" : "s"} here` : "Select photos below to place them here";
  };
  btn.addEventListener("click", async () => {
    const ids = visibleSelectedIds();
    if (!ids.length) return;
    try {
      await api("POST", "/api/geo/place", { asset_ids: ids, lat: r.lat, lon: r.lon, label: placeLabel(r) });
      searchMarker.closePopup();
      await loadSuggestions();
    } catch (e) {
      alert(e.message);
    }
  });
  searchMarker.bindPopup(div, { minWidth: 220 }).on("popupopen", refresh).openPopup();
}

searchInput.addEventListener("input", () => {
  clearTimeout(searchTimer);
  const text = searchInput.value.trim();
  if (text.length < 3) { searchList.hidden = true; return; }
  searchTimer = setTimeout(() => runSearch(text), 350);
});
searchInput.addEventListener("keydown", async (ev) => {
  if (ev.key === "Escape") { searchList.hidden = true; return; }
  if (ev.key !== "Enter") return;
  ev.preventDefault();
  clearTimeout(searchTimer);
  const text = searchInput.value.trim();
  if (!text) return;
  await runSearch(text);
  if (searchResults.length) showResult(searchResults[0]);
});
searchList.addEventListener("click", (ev) => {
  const li = ev.target.closest("[data-i]");
  if (li) showResult(searchResults[Number(li.dataset.i)]);
});

function setPlacing(on) {
  placing = on;
  document.getElementById("map-mode").hidden = !on;
  document.getElementById("map").classList.toggle("placing", on);
  if (on) {
    const n = visibleSelectedIds().length;
    document.getElementById("place-count").textContent = `${n} selected photo${n === 1 ? "" : "s"}`;
    document.getElementById("map").scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

document.getElementById("place-cancel").addEventListener("click", () => setPlacing(false));

// --- tracks --------------------------------------------------------------------------------

// Opened from the library overview: the tracks of that time, ticked for this visit only.
let tickedNow = null;

function isTicked(id) {
  if (tickedNow) return tickedNow.has(id);
  return !(loadPrefs().unticked || []).includes(id);
}

function setTicked(id, on) {
  if (tickedNow) { on ? tickedNow.add(id) : tickedNow.delete(id); return; }
  const unticked = new Set(loadPrefs().unticked || []);
  on ? unticked.delete(id) : unticked.add(id);
  savePrefs({ unticked: [...unticked] });
}

function renderTracks() {
  const list = document.getElementById("tracks");
  if (!tracks.length) {
    list.innerHTML = `<li class="muted">No tracks yet. Upload a GPX file to start.</li>`;
    return;
  }
  list.innerHTML = tracks.map(t => {
    const span = t.start ? `${fmtInstant(t.start)} – ${fmtInstant(t.end)}` : "";
    const times = t.needs_times ? `
      <div class="needs-times">
        <div>This file has no times (a planned route). When did you walk it? A constant pace is assumed.</div>
        <div class="row">
          <input type="datetime-local" data-role="start" step="60" title="Start">
          <input type="datetime-local" data-role="end" step="60" title="End">
          <button type="button" data-action="times">Save times</button>
        </div>
      </div>` : (!t.has_times ? `<div class="muted">Times entered by you (assumed constant pace). <a href="#" data-action="retime">Change</a></div>` : "");
    return `<li data-id="${esc(t.id)}">
      <label class="check"><input type="checkbox" data-action="tick" ${isTicked(t.id) ? "checked" : ""}>
        <i class="swatch" style="background:${trackColor(t.id)}"></i>
        <span class="track-name" title="${esc(t.file_name)}">${esc(t.name)}</span></label>
      <div class="muted">${esc(span)}${span ? " · " : ""}${t.distance_km} km · ${t.points} points</div>
      ${times}
      <div class="track-actions">
        <a href="#" data-action="zoom">Show</a> · <a href="#" data-action="use">Use time span</a> ·
        <a href="#" data-action="rename">Rename</a> · <a href="#" data-action="delete" class="danger">Delete</a>
      </div>
    </li>`;
  }).join("");
}

async function loadTracks() {
  tracks = await api("GET", "/api/tracks");
  // Routes still waiting for their times first, then the newest tracks.
  tracks.sort((a, b) => (b.start || "9999").localeCompare(a.start || "9999"));
  renderTracks();
  drawTracks();
}

async function upload(files) {
  const status = document.getElementById("upload-status");
  status.classList.remove("error");
  const errors = [];
  let added = 0;
  for (const file of files) {
    status.textContent = `Uploading ${file.name}…`;
    try {
      const resp = await fetch(`/api/tracks?file_name=${encodeURIComponent(file.name)}`, {
        method: "POST", headers: { "X-Metadatenmaster": "1", "Content-Type": "application/gpx+xml" }, body: file,
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      setTicked(data.id, true);
      added++;
    } catch (e) {
      errors.push(`${file.name}: ${e.message}`);
    }
  }
  status.textContent = [added ? `Added ${added} track${added === 1 ? "" : "s"}.` : "", ...errors].filter(Boolean).join(" ");
  status.classList.toggle("error", errors.length > 0);
  await loadTracks();
  fitMap();
}

const drop = document.getElementById("drop");
const fileInput = document.getElementById("gpx-file");
fileInput.addEventListener("change", () => { upload([...fileInput.files]); fileInput.value = ""; });
["dragenter", "dragover"].forEach(t => drop.addEventListener(t, ev => { ev.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach(t => drop.addEventListener(t, ev => { ev.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", ev => upload([...ev.dataTransfer.files].filter(f => /\.gpx$/i.test(f.name) || f.type.includes("gpx"))));

document.getElementById("tracks").addEventListener("click", async (ev) => {
  const el = ev.target.closest("[data-action]");
  if (!el) return;
  const li = el.closest("li");
  const t = tracks.find(x => x.id === li.dataset.id);
  const action = el.dataset.action;
  if (action === "tick") { setTicked(t.id, el.checked); drawTracks(); return; }
  ev.preventDefault();
  try {
    if (action === "zoom" && t.line.length) map.fitBounds(L.latLngBounds(t.line), { padding: [30, 30] });
    if (action === "use" && t.start) {
      form.elements.start.value = toLocalInput(t.start);
      form.elements.end.value = toLocalInput(t.end);
    }
    if (action === "rename") {
      const name = prompt("Name of the track:", t.name);
      if (name) { await api("PUT", `/api/tracks/${t.id}`, { name }); await loadTracks(); }
    }
    if (action === "delete" && confirm(`Delete the track “${t.name}”? Photos are not affected.`)) {
      await api("DELETE", `/api/tracks/${t.id}`);
      await loadTracks();
    }
    if (action === "retime") { t.needs_times = true; renderTracks(); }
    if (action === "times") {
      const start = li.querySelector("[data-role=start]").value;
      const end = li.querySelector("[data-role=end]").value;
      await api("PUT", `/api/tracks/${t.id}`, { start, end, timezone: cameraTz() });
      await loadTracks();
    }
  } catch (e) {
    alert(e.message);
  }
});

// --- finding -------------------------------------------------------------------------------

const PREF_FIELDS = ["camera_tz", "offset", "tolerance_min", "max_gap_min", "nearby_min", "use_photo_tz",
  "use_nearby", "use_siblings", "use_tracks", "include_located"];

function restoreForm() {
  const prefs = loadPrefs();
  for (const name of PREF_FIELDS) {
    const el = form.elements[name];
    if (!el || prefs[name] === undefined) continue;
    if (el.type === "checkbox") el.checked = Boolean(prefs[name]); else el.value = prefs[name];
  }
  if (!form.elements.camera_tz.value) form.elements.camera_tz.value = app.dataset.tz || "UTC";
}

function formRequest() {
  const f = form.elements;
  const offset = parseOffset(f.offset.value);
  if (Number.isNaN(offset)) throw new Error("Write the clock offset like +1:00:00, -0:30 or 90 (minutes).");
  savePrefs(Object.fromEntries(PREF_FIELDS.map(n => [n, f[n].type === "checkbox" ? f[n].checked : f[n].value])));
  const cameraOffsets = Object.fromEntries(Object.entries(savedOffsets()).filter(([k, v]) => k && Number.isFinite(v)));
  return {
    camera_offsets: cameraOffsets,
    track_ids: tracks.filter(t => isTicked(t.id)).map(t => t.id),
    use_tracks: f.use_tracks.checked,
    start: f.start.value || null,
    end: f.end.value || null,
    album_id: f.album_id.value || null,
    camera_tz: cameraTz(),
    use_photo_tz: f.use_photo_tz.checked,
    offset_s: offset,
    tolerance_min: Number(f.tolerance_min.value || 0),
    max_gap_min: Number(f.max_gap_min.value || 0),
    use_nearby: f.use_nearby.checked,
    nearby_min: Number(f.nearby_min.value || 0),
    use_siblings: f.use_siblings.checked,
    include_located: f.include_located.checked,
  };
}

async function find() {
  const errEl = document.getElementById("find-error");
  errEl.hidden = true;
  try {
    const body = formRequest();
    const pending = items.filter(it => it.status === "ok").length;
    if (pending && !confirm(`${pending} suggestions have not been written yet. Replace them with a new search?`)) return;
    const job = await api("POST", "/api/geo/find", body);
    await followJob(job, document.getElementById("find-job"), "Searching photos");
    selected.clear();
    await loadSuggestions();
    items.filter(it => it.status === "ok").forEach(it => selected.add(it.id));
    render();
    fitMap();
    review.scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    if (e.cancelled) return;  // the previous suggestions stay; followJob shows the message
    errEl.textContent = e.message;
    errEl.hidden = false;
  }
}

form.addEventListener("submit", ev => { ev.preventDefault(); find(); });

// --- review --------------------------------------------------------------------------------

function visibleItems() {
  const cam = cameraFilter.value;
  return items.filter(it => !cam || it.camera === cam);
}

function visibleSelectedIds() {
  return visibleItems().filter(it => selected.has(it.id)).map(it => it.id);
}

function suggestionHtml(it) {
  if (it.status === "located") {
    return `<div class="badge estimated">Has a location: ${Number(it.current_lat).toFixed(5)}, ${Number(it.current_lon).toFixed(5)}
      ${it.keep_reason ? `<div class="muted">Kept: ${esc(it.keep_reason)}.</div>` : ""}
      <div class="muted">Place it on the map to change it.</div></div>`;
  }
  if (it.status !== "ok") return `<div class="badge problem">No suggestion: ${esc(it.reason || "")}</div>`;
  let detail = "";
  if (it.source === "gpx") {
    detail = `${esc(it.detail)} · ${it.method === "interpolated" ? "on the track"
      : `${fmtDiff(it.time_diff_s)} from the nearest track point`}`;
  } else if (it.source === "sibling") {
    detail = `copied from ${esc(it.detail)}`;
  } else if (it.source === "nearby") {
    detail = `${esc(it.detail)}, ${fmtDiff(it.time_diff_s)} apart` +
      (it.same_camera ? " · same camera" : " · another camera") + (it.method === "interpolated" ? " · in between" : "");
  } else {
    detail = it.detail ? esc(it.detail) : "your choice";
  }
  const replaces = it.current_lat !== undefined && it.current_lat !== null
    ? `<div class="muted">replaces ${Number(it.current_lat).toFixed(5)}, ${Number(it.current_lon).toFixed(5)}</div>` : "";
  const ele = it.ele !== null && it.ele !== undefined ? ` · ${Math.round(it.ele)} m` : "";
  return `<div class="badge suggest src-${esc(it.source)}">
      <div><strong>${esc(SOURCE_LABEL[it.source] || it.source)}</strong> · <span class="conf-${esc(it.confidence)}">${esc(it.confidence)} confidence</span></div>
      <div>${detail}</div>
      ${placeHtml(it)}
      <div class="muted">${it.lat.toFixed(5)}, ${it.lon.toFixed(5)}${ele}</div>${replaces}
    </div>`;
}

/** Photos without a location still in the list (not counting listed photos that already have one). */
// Photos without a location still in the list (photos that have one are only listed on request).
function pendingCount() { return items.filter(it => it.current_lat === undefined || it.current_lat === null).length; }

function render() {
  review.hidden = !run;
  if (!run) { grid.innerHTML = ""; return; }

  const opts = run.options || {};
  const src = Object.entries(run.sources || {}).map(([k, v]) => `${v} from ${SOURCE_LABEL[k] || k}`).join(", ");
  document.getElementById("run-summary").innerHTML =
    `Search of ${esc(new Date(run.created_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }))}: ${run.searched} photos looked at, ${run.with_location} already had a location, ` +
    `${run.candidates} without one ${run.whole_album ? "in the album" : "in the time span"}, <strong>${run.suggested} with a suggestion</strong>${src ? ` (${esc(src)})` : ""}.` +
    (run.no_location_needed ? ` ${run.no_location_needed} skipped as “no location needed”.` : "") +
    (run.relocated ? ` <strong>${run.relocated} photo${run.relocated === 1 ? "" : "s"} that already had a location ` +
      `${run.relocated === 1 ? "is" : "are"} more than 100 m off the track</strong> and get${run.relocated === 1 ? "s" : ""} its position.` : "") +
    offsetsSummary(opts) +
    (pendingCount() < run.candidates ? ` ${run.candidates - pendingCount()} already written, discarded or marked.` : "");

  renderCameras();
  document.getElementById("fix-time-opt").hidden = !items.some(it => it.corrected);
  document.getElementById("local-time-opt").hidden = !items.some(it => it.place_tz);

  const cams = [...new Set(items.map(it => it.camera).filter(Boolean))].sort();
  const current = cameraFilter.value;
  cameraFilter.innerHTML = `<option value="">All cameras</option>` +
    cams.map(c => `<option value="${esc(c)}"${c === current ? " selected" : ""}>${esc(c)}</option>`).join("");

  const shown = visibleItems();
  if (!shown.length) {
    grid.innerHTML = `<p class="muted">${items.length ? "No photos of this camera."
      : `No photos without a location ${run.whole_album ? "in this album" : "in this time span"}.`}</p>`;
  } else {
    // One section per day, in time order.
    const days = new Map();
    for (const it of shown) {
      const day = dayOf(it);
      if (!days.has(day)) days.set(day, []);
      days.get(day).push(it);
    }
    grid.innerHTML = [...days].map(([day, list]) => `
      <section class="day">
        <div class="day-head">
          <h3>${esc(dayLabel(day))}</h3>
          <span class="muted">${list.length} photo${list.length === 1 ? "" : "s"} ·
            ${list.filter(it => it.status === "ok").length} with a suggestion</span>
          <button type="button" class="link" data-select-day="${esc(day)}">Select day</button>
        </div>
        <div class="grid">${list.map(cardHtml).join("")}</div>
      </section>`).join("");
  }
  drawSuggestions();
  updateSelection();
  loadPlaceNames();
}

function dayOf(it) { return (it.corrected || it.local || it.instant || "").slice(0, 10) || "unknown"; }

function dayLabel(day) {
  if (day === "unknown") return "Unknown date";
  const d = new Date(`${day}T12:00:00`);
  return Number.isNaN(d.getTime()) ? day
    : d.toLocaleDateString(undefined, { weekday: "short", year: "numeric", month: "short", day: "numeric" });
}

function cardHtml(it) {
  return `
    <article class="card ${selected.has(it.id) ? "selected" : ""}" data-id="${esc(it.id)}">
      <img src="/thumb/${esc(it.id)}" alt="" loading="lazy">
      <span class="check"></span>
      ${it.type === "VIDEO" ? `<span class="kind" title="Video">${icon("play")}</span>` : ""}
      <button type="button" class="zoom" data-zoom title="Large view (V or double-click)" aria-label="Large view">${icon("expand")}</button>
      <a class="open" href="${esc(immichUrl)}/photos/${esc(it.id)}" target="_blank" rel="noopener" title="Open in Immich" aria-label="Open in Immich">${icon("open")}</a>
      <div class="body">
        <div class="file" title="${esc(it.file_name)}">${esc(it.file_name)}</div>
        <div class="muted">${esc(fmtLocal(it.local))}${it.camera ? ` · ${esc(it.camera)}` : ""}</div>
        ${it.corrected ? `<div class="muted">Real time: ${esc(fmtLocal(it.corrected))}</div>` : ""}
        ${placeTimeHtml(it)}
        ${suggestionHtml(it)}
      </div>
    </article>`;
}

function showLarge(id) {
  const ids = visibleItems().map(it => it.id);
  openViewer({
    ids, index: Math.max(0, ids.indexOf(id)), immichUrl,
    isSelected: x => selected.has(x),
    toggle: x => select(x),
    describe: x => {
      const it = items.find(y => y.id === x);
      return it && {
        title: it.file_name, video: it.type === "VIDEO",
        html: `<div class="muted">${esc(fmtLocal(it.local))}${it.camera ? ` · ${esc(it.camera)}` : ""}</div>
          ${it.corrected ? `<div class="muted">Real time: ${esc(fmtLocal(it.corrected))}</div>` : ""}
          ${placeTimeHtml(it)}
          ${suggestionHtml(it)}`,
      };
    },
  });
}

function updateSelection() {
  const ids = visibleSelectedIds();
  const ok = items.filter(it => ids.includes(it.id) && it.status === "ok").length;
  selCount.textContent = `${ids.length} of ${visibleItems().length} selected`;
  applyBtn.disabled = !ok;
  applyBtn.textContent = ok ? `Write ${ok} location${ok === 1 ? "" : "s"}` : "Write locations";
  placeBtn.disabled = !ids.length;
  discardBtn.disabled = !ids.length;
  noLocationBtn.disabled = !ids.length;
  for (const card of grid.querySelectorAll(".card")) card.classList.toggle("selected", selected.has(card.dataset.id));
  for (const btn of grid.querySelectorAll("[data-select-day]")) {
    const ids = visibleItems().filter(it => dayOf(it) === btn.dataset.selectDay).map(it => it.id);
    btn.textContent = ids.length && ids.every(id => selected.has(id)) ? "Unselect day" : "Select day";
  }
  for (const [id, m] of markers) {
    const sel = selected.has(id);
    m.setStyle({ radius: sel ? 9 : 6, color: sel ? "#000" : "#fff" });
  }
}

const select = makeSelector(selected, () => visibleItems().map(it => it.id), updateSelection);

async function loadSuggestions() {
  const data = await api("GET", "/api/geo/suggestions");
  run = data.run;
  items = data.items;
  const ids = new Set(items.map(it => it.id));
  for (const id of [...selected]) if (!ids.has(id)) selected.delete(id);
  render();
}

grid.addEventListener("click", (ev) => {
  const dayBtn = ev.target.closest("[data-select-day]");
  if (dayBtn) {
    const ids = visibleItems().filter(it => dayOf(it) === dayBtn.dataset.selectDay).map(it => it.id);
    const all = ids.every(id => selected.has(id));
    ids.forEach(id => (all ? selected.delete(id) : selected.add(id)));
    updateSelection();
    return;
  }
  if (ev.target.closest("a")) return;
  const card = ev.target.closest(".card");
  if (!card) return;
  if (ev.target.closest("[data-zoom]")) { showLarge(card.dataset.id); return; }
  select(card.dataset.id, ev);
  const m = markers.get(card.dataset.id);
  if (m) map.panTo(m.getLatLng());
});

grid.addEventListener("dblclick", (ev) => {
  const card = ev.target.closest(".card");
  if (card && !ev.target.closest("a, button")) showLarge(card.dataset.id);
});

onShortcuts({
  a: () => { visibleItems().forEach(it => selected.add(it.id)); updateSelection(); },
  Escape: () => { if (placing) setPlacing(false); else { selected.clear(); updateSelection(); } },
  v: () => {
    const list = visibleItems();
    const first = list.find(it => selected.has(it.id)) || list[0];
    if (first) showLarge(first.id);
  },
});

document.querySelectorAll("[data-select]").forEach(btn => btn.addEventListener("click", () => {
  const mode = btn.dataset.select;
  selected.clear();
  for (const it of visibleItems()) {
    if (mode === "all" || (mode === "ok" && it.status === "ok") || (mode === "no_match" && it.status === "no_match")
        || (mode === "high" && it.status === "ok" && it.confidence === "high") || (mode === "screenshot" && it.screenshot)) {
      selected.add(it.id);
    }
  }
  updateSelection();
}));

cameraFilter.addEventListener("change", render);

// --- camera clocks -------------------------------------------------------------------------

function savedOffsets() { return loadPrefs().cameraOffsets || {}; }

function saveOffset(camera, seconds) {
  const all = savedOffsets();
  if (seconds === null) delete all[camera]; else all[camera] = seconds;
  savePrefs({ cameraOffsets: all });
}

function offsetsSummary(opts) {
  const parts = Object.entries(opts.camera_offsets || {}).filter(([, v]) => v).map(([k, v]) => `${k} ${formatOffset(v)}`);
  if (opts.offset_s) parts.push(`${parts.length ? "other cameras" : "all cameras"} ${formatOffset(opts.offset_s)}`);
  return parts.length ? ` Clock offsets: ${esc(parts.join(", "))}.` : "";
}

function renderCameras() {
  const box = document.getElementById("cameras");
  const cams = (run && run.cameras) || [];
  box.hidden = !cams.length;
  if (!cams.length) return;
  const hints = Object.fromEntries((run.hints || []).map(h => [h.camera, h]));
  const saved = savedOffsets();
  const nHints = Object.keys(hints).length;
  document.getElementById("cameras-note").textContent =
    nHints ? `· ${nHints} camera${nHints === 1 ? "" : "s"} may have a wrong clock` : `· ${cams.length} camera${cams.length === 1 ? "" : "s"}`;
  if (nHints) box.open = true;
  document.getElementById("camera-rows").innerHTML = cams.map(c => {
    const named = Boolean(c.camera);
    const value = named && saved[c.camera] !== undefined ? saved[c.camera] : c.offset_s;
    const h = hints[c.camera];
    const shift = h ? `${h.hours > 0 ? "+" : ""}${h.hours} h` : "";
    return `<tr data-camera="${esc(c.camera)}">
      <td><strong>${esc(c.camera || "Unknown camera")}</strong>
        <div class="muted">${c.without_location} without a location nearby · ${c.in_scope} in the time span ·
          ${c.suggested} with a suggestion</div>
        ${h ? `<div class="camera-hint">⚠ ${h.matches} of its photos would fit the tracks with the clock shifted by
          <strong>${shift}</strong> (now ${h.current}). Wrong time zone or summer time?
          <button type="button" class="link" data-shift="${h.hours}">Shift by ${shift} and search again</button></div>` : ""}</td>
      <td>${named ? `<input class="cam-offset" value="${esc(formatOffset(value))}" size="9" aria-label="Clock offset">`
        : `<span class="muted" title="Photos without a camera name use the clock offset in the form">${esc(formatOffset(value))}</span>`}</td>
      <td>${named && c.photos.length ? `<button type="button" data-sync>Sync with another camera…</button>` : ""}</td>
    </tr>`;
  }).join("");
}

const cameraRows = document.getElementById("camera-rows");

cameraRows.addEventListener("change", (ev) => {
  const input = ev.target.closest(".cam-offset");
  if (!input) return;
  const camera = input.closest("tr").dataset.camera;
  const text = input.value.trim();
  const seconds = text ? parseOffset(text) : null;
  input.classList.toggle("invalid", Number.isNaN(seconds));
  if (!Number.isNaN(seconds)) saveOffset(camera, seconds);
});

cameraRows.addEventListener("click", (ev) => {
  const row = ev.target.closest("tr");
  if (!row) return;
  const camera = row.dataset.camera;
  if (ev.target.closest("[data-shift]")) {
    const input = row.querySelector(".cam-offset");
    const current = parseOffset(input.value) || 0;
    saveOffset(camera, current + Number(ev.target.closest("[data-shift]").dataset.shift) * 3600);
    find();
  } else if (ev.target.closest("[data-sync]")) {
    openSync(camera);
  }
});

document.getElementById("search-again").addEventListener("click", () => find());

// --- working out a clock offset from two photos of the same moment ----------------------------

const syncDialog = document.getElementById("sync");
const syncBody = document.getElementById("sync-body");
let syncCamera = null;

function syncStep(title, text, body) {
  document.getElementById("sync-title").textContent = title;
  document.getElementById("sync-text").innerHTML = text;
  syncBody.innerHTML = body;
}

function photoButton(p, caption) {
  return `<button type="button" class="sync-photo" data-pick="${esc(p.id)}" title="${esc(p.file_name)}">
    <img src="/thumb/${esc(p.id)}" alt="" loading="lazy"><span>${caption}</span></button>`;
}

function openSync(camera) {
  const cam = run.cameras.find(c => c.camera === camera);
  syncCamera = { name: camera, photo: null };
  syncStep(`Sync the clock of ${camera}`,
    `<strong>1.</strong> Pick a photo of this camera that shows a moment you also photographed with another camera,
     e.g. your phone (the phone's clock is set automatically and is right).`,
    `<div class="sync-grid">${cam.photos.map(p => photoButton(p, esc(fmtLocal(p.local)))).join("")}</div>`);
  syncDialog.showModal();
}

function describeDelta(seconds) {
  const abs = Math.abs(seconds);
  const h = Math.floor(abs / 3600), m = Math.floor(abs % 3600 / 60), s = abs % 60;
  const text = [h ? `${h} h` : "", m ? `${m} min` : "", !h && s ? `${s} s` : ""].filter(Boolean).join(" ") || "0 s";
  return seconds === 0 ? "the same time" : `${text} ${seconds > 0 ? "later" : "earlier"}`;
}

async function pickCameraPhoto(id) {
  const cam = run.cameras.find(c => c.camera === syncCamera.name);
  const photo = cam.photos.find(p => p.id === id);
  syncCamera.photo = photo;
  syncStep(`Sync the clock of ${syncCamera.name}`, "Looking for photos of other cameras taken around then…", "");
  try {
    const params = new URLSearchParams({ asset_id: id, camera_tz: cameraTz(), use_photo_tz: form.elements.use_photo_tz.checked });
    const r = await api("GET", `/api/geo/clock-candidates?${params}`);
    syncCamera.others = r.others;
    renderReferences(id, photo, r.others.some(o => o.has_time_zone));
  } catch (e) {
    syncStep(`Sync the clock of ${syncCamera.name}`, `<span class="err">${esc(e.message)}</span>`, `<button type="button" data-back>← Back</button>`);
  }
}

/** Step 2: the photos of other cameras around then, in time order; phones (clock set automatically) first. */
function renderReferences(id, photo, phonesOnly) {
  const others = syncCamera.others.filter(o => !phonesOnly || o.has_time_zone);
  const anyPhones = syncCamera.others.some(o => o.has_time_zone);
  syncStep(`Sync the clock of ${syncCamera.name}`,
    `<strong>2.</strong> Which of these photos shows the same moment? Under each: how much later or earlier it was
     taken, by the clocks as they were.`,
    `<div class="sync-pair">
      <figure><img src="/thumb/${esc(id)}?size=preview" alt=""><figcaption>${esc(photo.file_name)} ·
        ${esc(fmtLocal(photo.local))}</figcaption></figure>
      <div>
        ${anyPhones ? `<label class="check"><input type="checkbox" data-phones ${phonesOnly ? "checked" : ""}>
          Only photos with an automatically set clock (phones)</label>` : ""}
        <div class="sync-grid">${others.map(o => photoButton(o,
          `${esc(o.camera || "?")}${o.has_time_zone ? " 📱" : ""}<br>${esc(fmtLocal(o.local))}<br>
           <strong>${esc(describeDelta(o.delta_s))}</strong>`)).join("")
          || `<p class="muted">No photos of other cameras within 6 hours.</p>`}</div>
      </div>
    </div>
    <button type="button" data-back>← Pick another photo</button>`);
  syncBody.querySelector("[data-phones]")?.addEventListener("change", (ev) => renderReferences(id, photo, ev.target.checked));
}

function pickReference(id) {
  const ref = syncCamera.others.find(o => o.id === id);
  const offset = ref.delta_s;
  syncStep(`Sync the clock of ${syncCamera.name}`,
    `The other camera's photo was taken <strong>${esc(describeDelta(offset))}</strong>, so the clock of
     ${esc(syncCamera.name)} is off by <strong>${esc(formatOffset(offset))}</strong>.`,
    `<div class="row"><button type="button" class="primary" data-use="${offset}">Use ${esc(formatOffset(offset))} for ${esc(syncCamera.name)}</button>
      <button type="button" data-back>← Pick another pair</button></div>`);
}

syncBody.addEventListener("click", (ev) => {
  const pick = ev.target.closest("[data-pick]");
  if (pick) {
    if (!syncCamera.photo) pickCameraPhoto(pick.dataset.pick); else pickReference(pick.dataset.pick);
    return;
  }
  if (ev.target.closest("[data-back]")) { openSync(syncCamera.name); return; }
  const use = ev.target.closest("[data-use]");
  if (use) {
    saveOffset(syncCamera.name, Number(use.dataset.use));
    syncDialog.close();
    renderCameras();
    const btn = document.getElementById("search-again");
    btn.classList.add("pulse");
    btn.scrollIntoView({ behavior: "smooth", block: "center" });
  }
});

// --- place names ---------------------------------------------------------------------------

const placeNamesOn = Boolean(app.dataset.placeNames);
const placeNames = new Map();
let placeLoading = false;

function placeKey(lat, lon) { return `${lat.toFixed(3)},${lon.toFixed(3)}`; }

function placeHtml(it) {
  if (!placeNamesOn || it.status !== "ok") return "";
  const key = placeKey(it.lat, it.lon);
  const name = placeNames.get(key);
  return `<div class="place" data-place="${key}">${name === undefined ? "…" : name ? `${icon("locations")} ${esc(name)}` : ""}</div>`;
}

/** Fill in the names of the places of the shown suggestions, a few at a time (they are cached on the server). */
async function loadPlaceNames() {
  if (!placeNamesOn || placeLoading) return;
  placeLoading = true;
  try {
    for (let round = 0; round < 100; round++) {
      const keys = [...new Set(visibleItems().filter(it => it.status === "ok").map(it => placeKey(it.lat, it.lon)))]
        .filter(k => !placeNames.has(k));
      if (!keys.length) break;
      const r = await api("POST", "/api/place-names", { keys, lang: navigator.language || "en" });
      for (const [k, v] of Object.entries(r.names)) placeNames.set(k, v);
      if (!r.pending || r.error) keys.forEach(k => { if (!placeNames.has(k)) placeNames.set(k, ""); });
      for (const el of document.querySelectorAll("[data-place]")) {
        const name = placeNames.get(el.dataset.place);
        if (name !== undefined) el.innerHTML = name ? `${icon("locations")} ${esc(name)}` : "";
      }
      if (r.error || !r.enabled) break;
    }
  } catch (e) {
    /* place names are a nicety */
  } finally {
    placeLoading = false;
  }
}

placeBtn.addEventListener("click", () => setPlacing(!placing));

discardBtn.addEventListener("click", async () => {
  const ids = visibleSelectedIds();
  if (!confirm(`Remove ${ids.length} photo${ids.length === 1 ? "" : "s"} from the suggestions? Nothing changes in Immich.`)) return;
  await api("POST", "/api/geo/discard", { asset_ids: ids });
  await loadSuggestions();
});

noLocationBtn.addEventListener("click", async () => {
  const ids = visibleSelectedIds();
  if (!confirm(`Mark ${ids.length} photo${ids.length === 1 ? "" : "s"} as “no location needed”? Later searches skip them. ` +
    "Nothing changes in Immich, and you can undo this at the bottom of the page.")) return;
  try {
    await api("POST", "/api/geo/no-location", { asset_ids: ids });
    await Promise.all([loadSuggestions(), loadNoLocation()]);
  } catch (e) {
    alert(e.message);
  }
});

async function loadNoLocation() {
  const list = await api("GET", "/api/geo/no-location");
  document.getElementById("no-location-count").textContent = list.length;
  document.getElementById("no-location-list").innerHTML = list.map(x => `<li>
      <a href="${esc(immichUrl)}/photos/${esc(x.id)}" target="_blank" rel="noopener">${esc(x.file_name || x.id)}</a>
      <button type="button" data-unmark="${esc(x.id)}">Unmark</button></li>`).join("") || `<li class="muted">None.</li>`;
}

document.getElementById("no-location-list").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("[data-unmark]");
  if (!btn) return;
  await api("POST", "/api/geo/no-location/remove", { asset_ids: [btn.dataset.unmark] });
  await loadNoLocation();
});

applyBtn.addEventListener("click", async () => {
  const chosen = visibleSelectedIds().map(id => items.find(it => it.id === id)).filter(it => it.status === "ok");
  const ids = chosen.map(it => it.id);
  const replacing = chosen.filter(it => it.current_lat !== undefined && it.current_lat !== null).length;
  const fixTime = !document.getElementById("fix-time-opt").hidden && document.getElementById("fix-time").checked;
  const localTime = !document.getElementById("local-time-opt").hidden && document.getElementById("local-time").checked;
  const zoneless = chosen.filter(it => it.place_tz).length;
  const msg = `Write the location of ${ids.length} photo${ids.length === 1 ? "" : "s"} to Immich?` +
    (replacing ? ` ${replacing} of them already have a location, which is replaced.` : "") +
    (fixTime ? "\n\nTheir capture times are corrected by their camera's clock offset as well." : "") +
    (localTime && zoneless ? `\n\n${zoneless} of them store no time zone: their capture time is written as the local time of the place.` : "") +
    "\n\nYou can undo this in History.";
  if (!confirm(msg)) return;
  const resultEl = document.getElementById("apply-result");
  resultEl.hidden = true;
  try {
    const job = await api("POST", "/api/geo/apply", { asset_ids: ids, fix_time: fixTime, local_time: localTime });
    const r = await followJob(job, document.getElementById("apply-job"), "Writing locations");
    resultEl.textContent = `Wrote ${r.written} location${r.written === 1 ? "" : "s"}` +
      (r.times_fixed ? `, corrected ${r.times_fixed} time${r.times_fixed === 1 ? "" : "s"}` : "") +
      (r.local_times ? `, set ${r.local_times} time${r.local_times === 1 ? "" : "s"} to the local time` : "") + ". Immich updates the place names in the background.";
    resultEl.hidden = false;
    await loadSuggestions();
  } catch (e) {
    if (e.cancelled) { await loadSuggestions(); return; }
    alert(e.message);
  }
});

// --- start ---------------------------------------------------------------------------------

/** /locations?from=2019-07-01T00:00&to=2019-08-01T00:00&label=July 2019 (from the library overview):
 * search that time with the tracks recorded then. */
async function searchFromOverview() {
  const q = new URLSearchParams(location.search);
  const from = q.get("from"), to = q.get("to");
  if (!from || !to || Number.isNaN(Date.parse(from)) || Number.isNaN(Date.parse(to))) return;
  history.replaceState(null, "", location.pathname);  // a reload should not search again
  const day = 86400e3;  // the camera's zone may differ from the browser's
  const lo = Date.parse(from) - day, hi = Date.parse(to) + day;
  tickedNow = new Set(tracks.filter(t => t.start && t.end && Date.parse(t.start) <= hi && Date.parse(t.end) >= lo)
    .map(t => t.id));
  renderTracks();
  drawTracks();
  form.elements.start.value = from;
  form.elements.end.value = to;
  const note = document.getElementById("preset-note");
  const n = tickedNow.size;
  note.textContent = `${q.get("label") || "This time"} from the library overview` +
    (n ? ` · ${n} track${n === 1 ? "" : "s"} from that time ticked.` : " · no tracks from that time.");
  note.hidden = false;
  await find();
}

try {
  document.getElementById("timezones").innerHTML =
    Intl.supportedValuesOf("timeZone").map(z => `<option value="${esc(z)}">`).join("");
} catch (e) { /* older browsers: free text only */ }

restoreForm();
form.elements.camera_tz.addEventListener("change", renderTracks);
Promise.all([loadTracks(), loadSuggestions(), loadNoLocation()]).then(fitMap).then(searchFromOverview).catch(e => {
  document.getElementById("tracks").innerHTML = `<li class="notice error">${esc(e.message)}</li>`;
}).then(() => Promise.all([
  // A search or write still running from before a reload: show it and refresh when it is done.
  resumeJob(j => j.kind === "geo-find", document.getElementById("find-job"), async () => {
    selected.clear();
    await loadSuggestions();
    fitMap();
  }),
  resumeJob(j => j.kind === "geo-apply", document.getElementById("apply-job"), loadSuggestions),
]));
