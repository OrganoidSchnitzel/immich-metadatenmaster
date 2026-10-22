"use strict";

const calibrateBtn = document.getElementById("calibrate");
const calibrationOut = document.getElementById("calibration-report");
const calibrationJob = document.getElementById("calibration-job");

function showReport(r) {
  if (!r) return;
  const rows = r.bins.filter(b => b.samples >= 1).map(b =>
    `<tr><td>${b.age}</td><td>${b.samples}</td><td>${b.bias >= 0 ? "+" : ""}${b.bias.toFixed(1)}</td><td>±${b.sigma.toFixed(1)}</td></tr>`).join("");
  calibrationOut.innerHTML = `<p>Used ${r.samples} faces of ${r.people} people. Mean age error: <strong>${r.mae_before} years</strong> raw,
    <strong>${r.mae_after} years</strong> after correction (in-sample).</p>
    <table class="calib"><thead><tr><th>True age</th><th>Faces</th><th>Bias</th><th>Spread</th></tr></thead><tbody>${rows}</tbody></table>
    <p class="muted">Reload the page to see the new calibration status.</p>`;
}

calibrateBtn.addEventListener("click", async () => {
  const perPerson = Number(document.getElementById("per-person").value) || 60;
  calibrateBtn.disabled = true;
  calibrationOut.innerHTML = "";
  try {
    const job = await api("POST", "/api/calibrate", { per_person: perPerson });
    showReport(await followJob(job, calibrationJob, "Calibrating"));
  } catch (e) {
    if (!e.cancelled) calibrationOut.innerHTML = `<p class="notice error">${esc(e.message)}</p>`;
  } finally {
    calibrateBtn.disabled = false;
  }
});

// A calibration still running from before a reload.
resumeJob(j => j.kind === "calibrate", calibrationJob, async (r) => showReport(r));

// --- versions of a photo (HDR, panoramas, edits) -------------------------------------------

const vBox = document.getElementById("versions");
const vGrid = document.getElementById("versions-grid");
const vJob = document.getElementById("versions-job");
const vResult = document.getElementById("versions-result");
const vApply = document.getElementById("versions-apply");
const vDiscard = document.getElementById("versions-discard");
const vImmich = vBox.dataset.immich;
let versions = [];
const vSelected = new Set();

function fmtTime(iso) { return iso ? iso.replace("T", " ").slice(0, 16) : "–"; }

function versionHtml(v) {
  return `<div class="muted">In Immich: ${esc(fmtTime(v.current))}${v.has_date ? "" : " (no capture date in the file)"}</div>
    <div class="badge suggest">
      <div>Date of <strong>${esc(v.original_name)}</strong>: <span class="big">${esc(fmtTime(v.new))}</span></div>
      ${v.latitude !== undefined && v.latitude !== null
        ? `<div class="muted">+ its location (${Number(v.latitude).toFixed(4)}, ${Number(v.longitude).toFixed(4)})</div>` : ""}
    </div>`;
}

function renderVersions() {
  document.getElementById("versions-review").hidden = !versions.length;
  vGrid.innerHTML = versions.map(v => `
    <article class="card ${vSelected.has(v.id) ? "selected" : ""}" data-id="${esc(v.id)}">
      <img src="/thumb/${esc(v.id)}" alt="" loading="lazy">
      <span class="check"></span>
      ${v.type === "VIDEO" ? `<span class="kind" title="Video">▶</span>` : ""}
      <button type="button" class="zoom" data-zoom title="Large view (V or double-click)">⤢</button>
      <a class="open" href="${esc(vImmich)}/photos/${esc(v.id)}" target="_blank" rel="noopener" title="Open in Immich">↗</a>
      <div class="body">
        <div class="file" title="${esc(v.file_name)}">${esc(v.file_name)}</div>
        ${versionHtml(v)}
      </div>
    </article>`).join("");
  updateVersionSelection();
}

function updateVersionSelection() {
  document.getElementById("versions-count").textContent = `${vSelected.size} of ${versions.length} selected`;
  vApply.disabled = vDiscard.disabled = !vSelected.size;
  vApply.textContent = vSelected.size ? `Copy ${vSelected.size} date${vSelected.size === 1 ? "" : "s"}` : "Copy dates";
  for (const card of vGrid.querySelectorAll(".card")) card.classList.toggle("selected", vSelected.has(card.dataset.id));
}

async function loadVersions() {
  versions = await api("GET", "/api/versions");
  const ids = new Set(versions.map(v => v.id));
  for (const id of [...vSelected]) if (!ids.has(id)) vSelected.delete(id);
  renderVersions();
}

const vSelect = makeSelector(vSelected, () => versions.map(v => v.id), updateVersionSelection);

function showVersion(id) {
  const ids = versions.map(v => v.id);
  openViewer({
    ids, index: Math.max(0, ids.indexOf(id)), immichUrl: vImmich,
    isSelected: x => vSelected.has(x),
    toggle: x => vSelect(x),
    describe: x => {
      const v = versions.find(y => y.id === x);
      return v && { title: v.file_name, video: v.type === "VIDEO", html: versionHtml(v) };
    },
  });
}

vGrid.addEventListener("click", (ev) => {
  if (ev.target.closest("a")) return;
  const card = ev.target.closest(".card");
  if (!card) return;
  if (ev.target.closest("[data-zoom]")) { showVersion(card.dataset.id); return; }
  vSelect(card.dataset.id, ev);
});
vGrid.addEventListener("dblclick", (ev) => {
  const card = ev.target.closest(".card");
  if (card && !ev.target.closest("a, button")) showVersion(card.dataset.id);
});
document.querySelectorAll("[data-vselect]").forEach(btn => btn.addEventListener("click", () => {
  vSelected.clear();
  if (btn.dataset.vselect === "all") versions.forEach(v => vSelected.add(v.id));
  updateVersionSelection();
}));
onShortcuts({
  a: () => { if (versions.length) { versions.forEach(v => vSelected.add(v.id)); updateVersionSelection(); } },
  Escape: () => { vSelected.clear(); updateVersionSelection(); },
  v: () => { const first = versions.find(v => vSelected.has(v.id)) || versions[0]; if (first) showVersion(first.id); },
});

function showVersionsFound(r) {
  if (!r) return;
  const parts = [`Found ${r.versions} version${r.versions === 1 ? "" : "s"}; <strong>${r.suggested} with a different date</strong>`];
  if (r.ambiguous) parts.push(`${r.ambiguous} skipped: several originals with that name from different times`);
  if (r.without_original) parts.push(`${r.without_original} without an original with a date`);
  vResult.innerHTML = parts.join(" · ") + ".";
  vResult.hidden = false;
}

document.getElementById("versions-find").addEventListener("click", async (ev) => {
  const btn = ev.currentTarget;
  btn.disabled = true;
  vResult.hidden = true;
  try {
    const job = await api("POST", "/api/versions/find", { album_id: document.getElementById("versions-album").value || null });
    showVersionsFound(await followJob(job, vJob));
    vSelected.clear();
    await loadVersions();
    versions.forEach(v => vSelected.add(v.id));
    updateVersionSelection();
  } catch (e) {
    if (!e.cancelled) { vResult.textContent = e.message; vResult.hidden = false; }
  } finally {
    btn.disabled = false;
  }
});

vApply.addEventListener("click", async () => {
  const ids = versions.filter(v => vSelected.has(v.id)).map(v => v.id);
  const withPlace = versions.filter(v => vSelected.has(v.id) && v.latitude !== undefined && v.latitude !== null).length;
  if (!confirm(`Write the original's date to ${ids.length} version${ids.length === 1 ? "" : "s"}` +
      (withPlace ? ` (and the location to ${withPlace})` : "") + "? You can undo this in History.")) return;
  try {
    const job = await api("POST", "/api/versions/apply", { asset_ids: ids });
    const r = await followJob(job, vJob);
    vResult.textContent = `Wrote ${r.written} date${r.written === 1 ? "" : "s"}` +
      (r.locations ? ` and ${r.locations} location${r.locations === 1 ? "" : "s"}` : "") + ".";
    vResult.hidden = false;
  } catch (e) {
    if (!e.cancelled) alert(e.message);
  }
  await loadVersions();
});

vDiscard.addEventListener("click", async () => {
  await api("POST", "/api/versions/discard", { asset_ids: [...vSelected] });
  await loadVersions();
});

loadVersions().then(() => Promise.all([
  resumeJob(j => j.kind === "versions-find", vJob, async (r) => { showVersionsFound(r); await loadVersions(); }),
  resumeJob(j => j.kind === "versions-apply", vJob, loadVersions),
]));
