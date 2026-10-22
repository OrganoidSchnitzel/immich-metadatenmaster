"use strict";

const app = document.getElementById("app");
const albumId = app.dataset.album;
const immichUrl = app.dataset.immich;
const grid = document.getElementById("grid");
const jobEl = document.getElementById("job");
const selCount = document.getElementById("sel-count");
const applyBtn = document.getElementById("apply");
const discardBtn = document.getElementById("discard");

let assets = [];
const selected = new Set();

const hasSuggestion = a => Boolean(a.name_date) || Boolean(a.estimate && a.estimate.status === "ok");
const hasAnySuggestion = a => Boolean(a.name_date || a.estimate);

function yearOf(iso) { return iso ? iso.slice(0, 10) : "–"; }

function acceptButton(a, buttons) {
  return buttons ? `<button type="button" class="accept" data-accept="${esc(a.id)}">Write this date</button>` : "";
}

function nameDateHtml(a, buttons) {
  const n = a.name_date;
  if (!n) return "";
  return `<div class="badge suggest">
      <div>File name says <span class="big">${esc(n.label)}</span></div>
      <div class="muted">${n.has_time ? "Date and time" : "Day only"} · in Immich: ${esc(yearOf(n.current))}</div>
      ${acceptButton(a, buttons)}
    </div>`;
}

/** The photo's date status and suggestion; `buttons` adds "Write this date" (not in the large view). */
function statusHtml(a, buttons = true) {
  const est = a.estimate;
  if (a.name_date) {
    const other = est && est.status === "ok"
      ? `<div class="muted">Faces suggest ≈ ${esc(est.estimate.label)} (the file name wins when writing)</div>` : "";
    return nameDateHtml(a, buttons) + other;
  }
  if (est) {
    if (est.status !== "ok") {
      return `<div class="badge problem">${esc(est.message || "No estimate")}</div>`;
    }
    const e = est.estimate;
    const faces = e.observations.filter(o => o.asset_id === a.id).map(o =>
      `<li class="${o.outlier ? "outlier" : ""}" title="Born ${esc(o.birth_date)}">${esc(o.person_name)} looks ${o.estimated_age.toFixed(0)}` +
      `${o.outlier ? " – doesn't fit, check face/birthday" : ""}</li>`).join("");
    const joint = est.joint ? `<div class="muted">Same event: ${e.asset_count} photo${e.asset_count === 1 ? "" : "s"} with faces</div>` : "";
    return `<div class="badge suggest">
        <div>Suggestion <span class="big">≈ ${esc(e.label)}</span></div>
        <div>90%: ${esc(e.range_label)} · <span class="conf-${e.confidence}">${e.confidence} confidence</span></div>
        ${joint}${faces ? `<ul class="faces">${faces}</ul>` : ""}
        ${acceptButton(a, buttons)}
      </div>`;
  }
  if (a.dated) {
    if (a.dated.source === "manual") return `<div class="badge manual">Date set: <strong>${esc(a.dated.label)}</strong></div>`;
    if (a.dated.source === "filename") return `<div class="badge manual">From file name: <strong>${esc(a.dated.label)}</strong></div>`;
    return `<div class="badge estimated">Estimated: <strong>${esc(a.dated.label)}</strong></div>`;
  }
  return `<div class="muted">Not dated yet</div>`;
}

function render() {
  if (!assets.length) {
    grid.innerHTML = `<p class="muted">This album is empty. If you are reusing it as a working album, upload the next batch of scans into it in Immich.</p>`;
    updateSelection();
    return;
  }
  grid.innerHTML = assets.map(a => {
    const notes = a.estimate && a.estimate.notes && a.estimate.notes.length
      ? `<div class="notes">${a.estimate.notes.map(esc).join(" · ")}</div>` : "";
    const people = a.people.length ? `<div class="muted">${a.people.map(esc).join(", ")}</div>` : "";
    return `<article class="card ${selected.has(a.id) ? "selected" : ""}" data-id="${esc(a.id)}">
      <img src="/thumb/${esc(a.id)}" alt="" loading="lazy">
      <span class="check"></span>
      ${a.type === "VIDEO" ? `<span class="kind" title="Video">▶</span>` : ""}
      <button type="button" class="zoom" data-zoom title="Large view (V or double-click)">⤢</button>
      <a class="open" href="${esc(immichUrl)}/photos/${esc(a.id)}" target="_blank" rel="noopener" title="Open in Immich">↗</a>
      <div class="body">
        <div class="file" title="${esc(a.file_name)}">${esc(a.file_name)}</div>
        <div class="muted">In Immich: ${yearOf(a.date)}</div>
        ${people}
        ${statusHtml(a)}
        ${notes}
      </div>
    </article>`;
  }).join("");
  updateSelection();
}

function renderPeople(people) {
  const strip = document.getElementById("people-strip");
  if (!people.length) { strip.hidden = true; return; }
  strip.hidden = false;
  strip.innerHTML = `<span class="muted">People in this batch:</span>` + people.map(p => p.birth_date
    ? `<span class="chip" title="${p.photos} photos">${esc(p.name)} · *${esc(p.birth_date.slice(0, 4))}</span>`
    : `<span class="chip missing" data-person="${esc(p.id)}" data-name="${esc(p.name)}" title="Click to set a birthday">${esc(p.name)} · no birthday</span>`
  ).join("");
}

function updateSelection() {
  selCount.textContent = `${selected.size} of ${assets.length} selected`;
  const withSuggestion = assets.some(a => selected.has(a.id) && hasAnySuggestion(a));
  applyBtn.disabled = !assets.some(a => selected.has(a.id) && hasSuggestion(a));
  discardBtn.disabled = !withSuggestion;
  for (const card of grid.querySelectorAll(".card")) {
    card.classList.toggle("selected", selected.has(card.dataset.id));
  }
}

function selectedIds() {
  return assets.filter(a => selected.has(a.id)).map(a => a.id);  // in display (file name) order
}

async function load() {
  try {
    const data = await api("GET", `/api/albums/${albumId}/assets`);
    assets = data.assets;
    const ids = new Set(assets.map(a => a.id));
    for (const id of [...selected]) if (!ids.has(id)) selected.delete(id);
    renderPeople(data.people);
    render();
  } catch (e) {
    grid.innerHTML = `<p class="notice error">${esc(e.message)}</p>`;
  }
}

const resultEl = document.getElementById("job-result");
const removeBox = document.getElementById("remove-from-album");

/** Fields for requests that write dates: the album to clean up, if the user wants that. */
function writeOptions() {
  return { album_id: albumId, remove_from_album: removeBox.checked };
}

function showResult(result) {
  if (result && result.suggested !== undefined) {
    resultEl.textContent = `Checked ${result.checked} file name${result.checked === 1 ? "" : "s"}: ` +
      `${result.with_date} contain${result.with_date === 1 ? "s" : ""} a date, ` +
      `${result.suggested} differ${result.suggested === 1 ? "s" : ""} from the date in Immich.`;
    resultEl.classList.remove("warn");
    resultEl.hidden = false;
    return;
  }
  if (!result || result.written === undefined) { resultEl.hidden = true; return; }
  const parts = [`Wrote ${result.written} date${result.written === 1 ? "" : "s"}`];
  if (result.skipped) parts.push(`${result.skipped} skipped (no suggestion)`);
  if (result.removed !== undefined) parts.push(`${result.removed} removed from this album`);
  if (result.remove_error) parts.push(`removing from the album failed: ${result.remove_error}`);
  resultEl.textContent = parts.join(" · ");
  resultEl.classList.toggle("warn", Boolean(result.remove_error));
  resultEl.hidden = false;
}

async function runJob(label, url, body) {
  resultEl.hidden = true;
  try {
    const job = await api("POST", url, body);
    const result = await followJob(job, jobEl, label);
    await load();
    showResult(result);
    return result;
  } catch (e) {
    if (e.cancelled) { await load(); return null; }  // followJob shows the message
    jobEl.hidden = false;
    jobEl.classList.add("failed");
    jobEl.textContent = e.message;
    return null;
  }
}

// --- selection ---------------------------------------------------------------------------

const select = makeSelector(selected, () => assets.map(a => a.id), updateSelection);

function showLarge(id) {
  const ids = assets.map(a => a.id);
  openViewer({
    ids, index: Math.max(0, ids.indexOf(id)), immichUrl,
    isSelected: x => selected.has(x),
    toggle: x => select(x),
    describe: x => {
      const a = assets.find(y => y.id === x);
      return a && {
        title: a.file_name, video: a.type === "VIDEO",
        html: `<div class="muted">In Immich: ${esc(yearOf(a.date))}${a.people.length ? ` · ${a.people.map(esc).join(", ")}` : ""}</div>
          ${statusHtml(a, false)}`,
      };
    },
  });
}

grid.addEventListener("click", async (ev) => {
  const accept = ev.target.closest("[data-accept]");
  if (accept) {
    await runJob("Writing date", "/api/apply-estimates", { asset_ids: [accept.dataset.accept], ...writeOptions() });
    return;
  }
  if (ev.target.closest("a")) return;
  const card = ev.target.closest(".card");
  if (!card) return;
  if (ev.target.closest("[data-zoom]")) { showLarge(card.dataset.id); return; }
  select(card.dataset.id, ev);
});

grid.addEventListener("dblclick", (ev) => {
  const card = ev.target.closest(".card");
  if (card && !ev.target.closest("a, button")) showLarge(card.dataset.id);
});

onShortcuts({
  a: () => { assets.forEach(a => selected.add(a.id)); updateSelection(); },
  Escape: () => { selected.clear(); updateSelection(); },
  v: () => { const first = assets.find(a => selected.has(a.id)) || assets[0]; if (first) showLarge(first.id); },
});

document.querySelectorAll("[data-select]").forEach(btn => btn.addEventListener("click", () => {
  const mode = btn.dataset.select;
  selected.clear();
  for (const a of assets) {
    if (mode === "all" || (mode === "undated" && !a.dated) || (mode === "suggested" && hasSuggestion(a))) {
      selected.add(a.id);
    }
  }
  updateSelection();
}));

document.getElementById("people-strip").addEventListener("click", async (ev) => {
  const chip = ev.target.closest("[data-person]");
  if (!chip) return;
  const value = prompt(`Birthday of ${chip.dataset.name} (YYYY-MM-DD):`);
  if (!value) return;
  try {
    await api("PUT", `/api/people/${chip.dataset.person}/birthdate`, { birth_date: value.trim() });
    await load();
  } catch (e) {
    alert(e.message);
  }
});

// --- known date --------------------------------------------------------------------------

const knownInput = document.getElementById("known-date");
const knownHint = document.getElementById("known-hint");
let hintTimer = null;

knownInput.addEventListener("input", () => {
  clearTimeout(hintTimer);
  hintTimer = setTimeout(async () => {
    const text = knownInput.value.trim();
    knownHint.classList.remove("error");
    if (!text) { knownHint.textContent = "Year or month is enough."; return; }
    const r = await api("GET", `/api/parse-date?text=${encodeURIComponent(text)}`);
    if (r.ok) {
      knownHint.textContent = `${r.label} (${r.precision}) → written as ${r.written}`;
    } else {
      knownHint.classList.add("error");
      knownHint.textContent = r.error;
    }
  }, 250);
});

document.getElementById("known-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const ids = selectedIds();
  if (!ids.length) { alert("Select photos first."); return; }
  const already = assets.filter(a => selected.has(a.id) && a.dated).length;
  if (already && !confirm(`${already} of the selected photos were already dated. Overwrite them?`)) return;
  await runJob("Writing dates", "/api/known-date", {
    asset_ids: ids, date: knownInput.value, keep_order: document.getElementById("keep-order").checked,
    ...writeOptions(),
  });
});

// --- estimation --------------------------------------------------------------------------

document.getElementById("estimate-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const ids = selectedIds();
  if (!ids.length) { alert("Select photos first."); return; }
  await runJob("Analysing faces", "/api/estimate", {
    asset_ids: ids,
    album_id: albumId,
    joint: document.getElementById("joint").checked,
    not_before: document.getElementById("not-before").value || null,
    not_after: document.getElementById("not-after").value || null,
  });
});

applyBtn.addEventListener("click", async () => {
  const ids = assets.filter(a => selected.has(a.id) && hasSuggestion(a)).map(a => a.id);
  if (!confirm(`Write the suggested dates of ${ids.length} photos to Immich?`)) return;
  await runJob("Writing dates", "/api/apply-estimates", { asset_ids: ids, ...writeOptions() });
});

document.getElementById("name-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const ids = selectedIds();
  if (!ids.length) { alert("Select photos first."); return; }
  await runJob("Reading file names", "/api/name-dates", { album_id: albumId, asset_ids: ids });
});

discardBtn.addEventListener("click", async () => {
  const ids = assets.filter(a => selected.has(a.id) && hasAnySuggestion(a)).map(a => a.id);
  await api("POST", "/api/discard-estimates", { asset_ids: ids });
  await load();
});

load().then(() => resumeJob(j => j.meta.album_id === albumId, jobEl, async (result) => {
  await load();
  showResult(result);
}));
