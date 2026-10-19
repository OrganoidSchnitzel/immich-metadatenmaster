"use strict";

const form = document.getElementById("settings");
const checks = document.getElementById("checks");
const saveStatus = document.getElementById("save-status");
let configured = false;

function fieldValues() {
  const values = {};
  for (const el of form.elements) {
    if (!el.name || el.disabled) continue;
    values[el.name] = el.type === "checkbox" ? el.checked : el.value.trim();
  }
  return values;
}

function showOllama() {
  const ollama = form.elements.estimator.value === "ollama";
  form.querySelectorAll("[data-show=ollama]").forEach(el => { el.hidden = !ollama; });
}

function renderChecks(result) {
  checks.innerHTML = result.checks.map(c =>
    `<li class="${c.ok ? "ok" : "bad"}">${c.ok ? "✓" : "✗"} ${esc(c.label)}${c.detail ? ` – ${esc(c.detail)}` : ""}</li>`).join("");
}

async function load() {
  const data = await api("GET", "/api/settings");
  configured = data.configured;
  for (const [name, value] of Object.entries(data.values)) {
    const el = form.elements[name];
    if (!el) continue;
    if (el.type === "checkbox") el.checked = Boolean(value); else el.value = value ?? "";
  }
  for (const name of data.locked) {
    const el = form.elements[name];
    if (el) { el.disabled = true; el.title = "Set by an environment variable"; }
  }
  document.getElementById("locked-note").hidden = !data.locked.length;
  document.getElementById("data-dir").textContent = data.data_dir;
  document.getElementById("key-hint").textContent = data.locked.includes("immich_api_key")
    ? "Set by an environment variable."
    : data.has_api_key ? "A key is saved. Leave empty to keep it." : "";
  // Suggest the browser's time zone on first setup.
  const tz = form.elements.timezone;
  if (!configured && !tz.disabled && (!tz.value || tz.value === "UTC")) {
    tz.value = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  }
  showOllama();
}

try {
  document.getElementById("timezones").innerHTML =
    Intl.supportedValuesOf("timeZone").map(z => `<option value="${esc(z)}">`).join("");
} catch (e) { /* older browsers: free text only */ }

form.elements.estimator.addEventListener("change", showOllama);

document.getElementById("test").addEventListener("click", async (ev) => {
  const v = fieldValues();
  checks.innerHTML = "<li>Testing…</li>";
  try {
    renderChecks(await api("POST", "/api/settings/test", {
      immich_url: v.immich_url ?? "", immich_api_key: v.immich_api_key ?? "",
    }));
  } catch (e) {
    checks.innerHTML = `<li class="bad">✗ ${esc(e.message)}</li>`;
  }
});

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  saveStatus.textContent = "Saving…";
  try {
    await api("PUT", "/api/settings", fieldValues());
    saveStatus.textContent = "Saved.";
    if (!configured) { window.location.href = "/"; return; }
    form.elements.immich_api_key.value = "";
    await load();
  } catch (e) {
    saveStatus.textContent = e.message;
  }
});

load().catch(e => { saveStatus.textContent = e.message; });
