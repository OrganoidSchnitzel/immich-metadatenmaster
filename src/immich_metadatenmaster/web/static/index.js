"use strict";

// --- library overview ----------------------------------------------------------------------

const libBody = document.getElementById("lib-body");
const libJob = document.getElementById("lib-job");
const libScanned = document.getElementById("lib-scanned");
const scanBtn = document.getElementById("lib-scan");
const OPEN_KEY = "metadatenmaster.overview.open";

function openYears() {
  try { return new Set(JSON.parse(sessionStorage.getItem(OPEN_KEY) || "[]")); } catch (e) { return new Set(); }
}

function saveOpenYears() {
  const open = [...libBody.querySelectorAll("details[data-year]")].filter(d => d.open).map(d => d.dataset.year);
  try { sessionStorage.setItem(OPEN_KEY, JSON.stringify(open)); } catch (e) { /* not important */ }
}

const fmt = n => Number(n).toLocaleString();

/** "2019-07" or "2019" → the local time range the Locations page should search. */
function locationsUrl(period, label) {
  const year = Number(period.slice(0, 4));
  let from, to;
  if (period.length === 4) {
    from = `${year}-01-01T00:00`;
    to = `${year + 1}-01-01T00:00`;
  } else {
    const month = Number(period.slice(5, 7));
    from = `${period}-01T00:00`;
    to = month === 12 ? `${year + 1}-01-01T00:00` : `${year}-${String(month + 1).padStart(2, "0")}-01T00:00`;
  }
  return `/locations?${new URLSearchParams({ from, to, label })}`;
}

/** One row of numbers; each number links to where the photos can be fixed. */
function cells(row, period, label) {
  const share = row.total ? Math.round((100 * row.no_location) / row.total) : 0;
  const link = (n, href, title) => n
    ? `<a href="${esc(href)}" title="${esc(title)}">${fmt(n)}</a>`
    : `<span class="muted">–</span>`;
  const known = period !== "unknown";
  return `<span class="num">${fmt(row.total)}</span>
    <span class="num share" style="--p:${share}%">${known
      ? link(row.no_location, locationsUrl(period, label), `Find locations for ${label}`)
      : (row.no_location ? fmt(row.no_location) : `<span class="muted">–</span>`)}</span>
    <span class="num">${link(row.no_date, `/album/scan:no-date:${period}`, `Date the photos of ${label}`)}</span>
    <span class="num">${link(row.name_date, `/album/scan:name-date:${period}`, `Dates from file names, ${label}`)}</span>`;
}

function renderLibrary(data) {
  scanBtn.textContent = data.scanned_at ? "Scan again" : "Scan library";
  if (!data.scanned_at) {
    libScanned.textContent = "";
    libBody.innerHTML = `<p class="muted">See per month which photos lack a location or a date. The scan reads the
      metadata of all photos and videos (no images); a large library takes a minute or two. Nothing is changed.</p>`;
    return;
  }
  libScanned.textContent = `Scanned ${new Date(data.scanned_at).toLocaleString()}`;
  const open = openYears();
  const years = data.years.map(y => y.year === "unknown"
    ? `<div class="lib-row"><span class="muted">${esc(y.label)}</span>${cells(y, y.year, y.label)}</div>`
    : `<details data-year="${esc(y.year)}"${open.has(y.year) ? " open" : ""}>
        <summary class="lib-row"><span class="lib-label"><strong>${esc(y.label)}</strong></span>${cells(y, y.year, y.label)}</summary>
        ${y.months.map(m => `<div class="lib-row month"><span>${esc(m.label)}</span>${cells(m, m.month, m.label)}</div>`).join("")}
      </details>`).join("");
  libBody.innerHTML = `
    <div class="lib-summary">
      <span><strong>${fmt(data.total)}</strong> photos and videos</span>
      <span><strong>${fmt(data.no_location)}</strong> without a location</span>
      <span><strong>${fmt(data.no_date)}</strong> without a capture date</span>
      <span><strong>${fmt(data.name_date)}</strong> with another date in the file name</span>
    </div>
    <div class="lib-table">
      <div class="lib-row head"><span></span><span class="num">Photos</span><span class="num">No location</span>
        <span class="num">No capture date</span><span class="num">Other date in name</span></div>
      ${years || `<p class="muted">The library is empty.</p>`}
    </div>
    <p class="muted small">Click a number to work on those photos. Screenshots and photos marked as needing no location
      are not counted. Changes made with this tool update the numbers; changes made in Immich show after the next scan.</p>`;
  libBody.querySelectorAll("details[data-year]").forEach(d => d.addEventListener("toggle", saveOpenYears));
}

async function loadLibrary() {
  try {
    renderLibrary(await api("GET", "/api/library"));
  } catch (e) {
    libBody.innerHTML = `<p class="notice error">${esc(e.message)}</p>`;
  }
}

scanBtn.addEventListener("click", async () => {
  scanBtn.disabled = true;
  try {
    await followJob(await api("POST", "/api/library/scan"), libJob, "Scanning the library");
    await loadLibrary();
  } catch (e) {
    if (!e.cancelled) alert(e.message);
  } finally {
    scanBtn.disabled = false;
  }
});

loadLibrary().then(() => resumeJob(j => j.kind === "library-scan", libJob, loadLibrary));
