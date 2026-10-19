"use strict";

async function api(method, url, body) {
  const resp = await fetch(url, {
    method,
    headers: body === undefined ? { "X-Metadatenmaster": "1" } : { "Content-Type": "application/json", "X-Metadatenmaster": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = resp.headers.get("content-type")?.includes("json") ? await resp.json() : await resp.text();
  if (!resp.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail || data));
  }
  return data;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/** Poll a background job, rendering progress into `el`. Resolves with the job's result. */
async function followJob(job, el, label) {
  el.hidden = false;
  el.classList.remove("failed");
  for (;;) {
    const pct = job.total ? Math.round((100 * job.done) / job.total) : 0;
    el.innerHTML = `${esc(label)}: ${job.status === "queued" ? "waiting…" : `${job.done} / ${job.total || "?"}`}
      <div class="bar"><span style="width:${pct}%"></span></div>`;
    if (job.status === "done") { el.hidden = true; return job.result; }
    if (job.status === "failed") {
      el.classList.add("failed");
      el.textContent = `${label} failed: ${job.error}`;
      throw new Error(job.error);
    }
    await new Promise(r => setTimeout(r, 800));
    job = await api("GET", `/api/jobs/${job.id}`);
  }
}
