"use strict";

document.getElementById("calibrate").addEventListener("click", async (ev) => {
  const button = ev.currentTarget;
  const perPerson = Number(document.getElementById("per-person").value) || 60;
  const out = document.getElementById("calibration-report");
  button.disabled = true;
  out.innerHTML = "";
  try {
    const job = await api("POST", "/api/calibrate", { per_person: perPerson });
    const r = await followJob(job, document.getElementById("calibration-job"), "Calibrating");
    const rows = r.bins.filter(b => b.samples >= 1).map(b =>
      `<tr><td>${b.age}</td><td>${b.samples}</td><td>${b.bias >= 0 ? "+" : ""}${b.bias.toFixed(1)}</td><td>±${b.sigma.toFixed(1)}</td></tr>`).join("");
    out.innerHTML = `<p>Used ${r.samples} faces of ${r.people} people. Mean age error: <strong>${r.mae_before} years</strong> raw,
      <strong>${r.mae_after} years</strong> after correction (in-sample).</p>
      <table class="calib"><thead><tr><th>True age</th><th>Faces</th><th>Bias</th><th>Spread</th></tr></thead><tbody>${rows}</tbody></table>
      <p class="muted">Reload the page to see the new calibration status.</p>`;
  } catch (e) {
    out.innerHTML = `<p class="notice error">${esc(e.message)}</p>`;
  } finally {
    button.disabled = false;
  }
});
