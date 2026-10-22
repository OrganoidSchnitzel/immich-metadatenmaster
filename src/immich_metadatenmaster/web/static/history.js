"use strict";

const immichUrl = document.getElementById("app").dataset.immich;
const tbody = document.getElementById("batches");

function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso.replace("T", " ").slice(0, 16)
    : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function value(kind, v) {
  if (!v) return "–";
  if (kind === "date") {
    if (v.date) return esc(v.date.replace("T", " ").slice(0, 19));
    return v.local ? esc(v.local.replace("T", " ").slice(0, 19)) : "–";
  }
  return v.latitude !== null && v.latitude !== undefined
    ? `${Number(v.latitude).toFixed(5)}, ${Number(v.longitude).toFixed(5)}` : "no location";
}

function status(b) {
  if (b.undone_at) return `<span class="muted">undone ${esc(when(b.undone_at))}</span>`;
  if (b.undoable) return `<button type="button" data-undo="${b.id}">Undo</button>`;
  return `<span class="muted" title="The previous date is unknown">cannot be undone</span>`;
}

async function load() {
  const batches = await api("GET", "/api/history");
  tbody.innerHTML = batches.map(b => `
    <tr data-id="${b.id}">
      <td>${esc(when(b.created_at))}</td>
      <td><a href="#" data-details="${b.id}">${esc(b.label)}</a>${b.tag ? ` <span class="muted">· tag ${esc(b.tag)}</span>` : ""}</td>
      <td>${b.count}</td>
      <td>${status(b)}</td>
    </tr>
    <tr class="details" data-for="${b.id}" hidden><td colspan="4"></td></tr>`).join("")
    || `<tr><td colspan="4" class="muted">Nothing written yet.</td></tr>`;
}

tbody.addEventListener("click", async (ev) => {
  const details = ev.target.closest("[data-details]");
  if (details) {
    ev.preventDefault();
    const row = tbody.querySelector(`tr[data-for="${details.dataset.details}"]`);
    if (!row.hidden) { row.hidden = true; return; }
    const b = await api("GET", `/api/history/${details.dataset.details}`);
    row.querySelector("td").innerHTML = `<table class="changes"><thead><tr><th>Photo</th><th>Before</th><th>Written</th></tr></thead><tbody>` +
      b.changes.map(c => `<tr>
        <td><a href="${esc(immichUrl)}/photos/${esc(c.asset_id)}" target="_blank" rel="noopener">${esc(c.file_name || c.asset_id)}</a></td>
        <td>${value(b.kind, b.kind === "date" ? { local: c.old.local } : c.old)}</td>
        <td>${value(b.kind, c.new)}</td></tr>`).join("") + `</tbody></table>`;
    row.hidden = false;
    return;
  }
  const undo = ev.target.closest("[data-undo]");
  if (undo) {
    const row = undo.closest("tr");
    if (!confirm(`Undo “${row.querySelector("[data-details]").textContent}”? The photos get their previous values back.`)) return;
    const resultEl = document.getElementById("result");
    resultEl.hidden = true;
    try {
      const job = await api("POST", `/api/history/${undo.dataset.undo}/undo`);
      const r = await followJob(job, document.getElementById("job"), "Undoing");
      const parts = [`Restored ${r.restored} photo${r.restored === 1 ? "" : "s"}`];
      if (r.changed) parts.push(`${r.changed} left alone because they were changed in Immich since`);
      if (r.failed) parts.push(`${r.failed} could not be cleared by Immich and keep the written location`);
      if (r.skipped) parts.push(`${r.skipped} skipped (previous value unknown)`);
      resultEl.textContent = parts.join(" · ") + ".";
      resultEl.classList.toggle("warn", Boolean(r.changed || r.failed));
      resultEl.hidden = false;
      await load();
    } catch (e) {
      alert(e.message);
    }
  }
});

load().catch(e => { tbody.innerHTML = `<tr><td colspan="4" class="err">${esc(e.message)}</td></tr>`; });
