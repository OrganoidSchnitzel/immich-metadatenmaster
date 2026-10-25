"use strict";

const loginForm = document.getElementById("login-form");
const loginStatus = document.getElementById("login-status");
const kindLink = document.getElementById("login-kind");  // only with several users
let withKey = false;

kindLink?.addEventListener("click", (ev) => {
  ev.preventDefault();
  withKey = !withKey;
  document.getElementById("with-password").hidden = withKey;
  document.getElementById("with-key").hidden = !withKey;
  kindLink.textContent = withKey ? "Log in with email and password instead" : "Log in with an API key instead";
  loginForm.elements[withKey ? "api_key" : "email"].focus();
  loginStatus.textContent = "";
});

function credentials() {
  const f = loginForm.elements;
  if (!kindLink) return { password: f.password.value };
  return withKey ? { api_key: f.api_key.value.trim() } : { email: f.email.value.trim(), password: f.password.value };
}

loginForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const button = loginForm.querySelector("button");
  button.disabled = true;
  loginStatus.classList.remove("error");
  loginStatus.textContent = "Checking…";
  try {
    await api("POST", "/api/login", credentials());
    location.href = loginForm.dataset.next || "/";
  } catch (e) {
    loginStatus.textContent = e.message;
    loginStatus.classList.add("error");
    const field = loginForm.elements[withKey ? "api_key" : "password"];
    field.select();
  } finally {
    button.disabled = false;
  }
});
