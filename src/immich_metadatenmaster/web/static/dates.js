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
