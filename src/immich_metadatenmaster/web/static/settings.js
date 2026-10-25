"use strict";

const form = document.getElementById("settings");
const checks = document.getElementById("checks");
const saveStatus = document.getElementById("save-status");
let configured = false;
let keySaved = false;

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

function showUsers() {
  const multi = form.elements.multi_user.value === "true";
  document.getElementById("users-hint").textContent = multi
    ? "Everyone logs in with their Immich email and password (or an API key). Only Immich administrators " +
      "can change these settings. The API key below is then optional: it only tells whose data the " +
      "single-user setup was."
    : "No login with Immich: everyone who can open this tool works on the photos of the API key's owner " +
      "(protect it with a password below).";
  form.elements.immich_api_key.placeholder = multi && !keySaved ? "optional" : "paste the key here";
}

function renderChecks(result) {
  checks.innerHTML = result.checks.map(c =>
    `<li class="${c.ok ? "ok" : "bad"}">${c.ok ? "✓" : "✗"} ${esc(c.label)}${c.detail ? ` – ${esc(c.detail)}` : ""}</li>`).join("");
}

async function load() {
  const data = await api("GET", "/api/settings");
  configured = data.configured;
  keySaved = data.has_api_key;
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
  renderPassword(data.password, data.password_min_length);
  // With several users the password is not used; only Immich admins change settings.
  pwForm.hidden = data.values.multi_user;
  if (!data.can_edit) {
    form.querySelectorAll("input, select, button").forEach(el => { el.disabled = true; });
    document.getElementById("admin-only").hidden = false;
  }
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
  showUsers();
}

try {
  document.getElementById("timezones").innerHTML =
    Intl.supportedValuesOf("timeZone").map(z => `<option value="${esc(z)}">`).join("");
} catch (e) { /* older browsers: free text only */ }

form.elements.estimator.addEventListener("change", showOllama);
form.elements.multi_user.addEventListener("change", showUsers);
document.getElementById("account-logout")?.addEventListener("click", () => document.getElementById("logout").click());

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
    const r = await api("PUT", "/api/settings", fieldValues());
    saveStatus.textContent = "Saved.";
    if (r.login) { window.location.href = "/login"; return; }
    if (!configured) { window.location.href = "/"; return; }
    form.elements.immich_api_key.value = "";
    await load();
  } catch (e) {
    saveStatus.textContent = e.message;
  }
});

// --- password ------------------------------------------------------------------------------

const pwForm = document.getElementById("password-form");
const pwStatus = document.getElementById("pw-status");
let minLength = 8;

function renderPassword(state, min) {
  minLength = min || minLength;
  const text = {
    none: "No password: everyone who can open this page can change your photos in Immich. Set one if others " +
      "can reach the tool (on your network or from the internet).",
    set: "A password is set. Browsers stay logged in for 30 days; a new password logs out all others.",
    env: "The password is set by the UI_PASSWORD environment variable and can only be changed there.",
  }[state];
  document.getElementById("pw-state").textContent = text;
  document.getElementById("pw-fields").hidden = state === "env";
  document.getElementById("pw-current").hidden = state !== "set";
  document.getElementById("pw-remove").hidden = state !== "set";
  document.getElementById("pw-save").textContent = state === "set" ? "Change password" : "Set password";
  const logout = document.getElementById("logout");
  if (logout && !document.getElementById("account")) logout.hidden = state === "none";
}

async function savePassword(newPassword) {
  pwStatus.textContent = "Saving…";
  try {
    const r = await api("PUT", "/api/settings/password", { current: pwForm.elements.current.value, new: newPassword });
    pwForm.reset();
    renderPassword(r.password);
    pwStatus.textContent = r.password === "none" ? "Password removed." : "Password saved.";
  } catch (e) {
    pwStatus.textContent = e.message;
  }
}

pwForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = pwForm.elements;
  if (f.new.value.length < minLength) { pwStatus.textContent = `Use at least ${minLength} characters.`; return; }
  if (f.new.value !== f.repeat.value) { pwStatus.textContent = "The two new passwords differ."; return; }
  await savePassword(f.new.value);
});

document.getElementById("pw-remove").addEventListener("click", async () => {
  if (!pwForm.elements.current.value) { pwStatus.textContent = "Enter the current password first."; return; }
  if (!confirm("Remove the password? Everyone who can reach this page can then use it.")) return;
  await savePassword("");
});

load().catch(e => { saveStatus.textContent = e.message; });
