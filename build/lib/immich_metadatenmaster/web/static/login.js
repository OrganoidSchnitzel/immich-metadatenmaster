"use strict";

const loginForm = document.getElementById("login-form");
const loginStatus = document.getElementById("login-status");

loginForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const button = loginForm.querySelector("button");
  button.disabled = true;
  loginStatus.classList.remove("error");
  loginStatus.textContent = "Checking…";
  try {
    await api("POST", "/api/login", { password: loginForm.elements.password.value });
    location.href = loginForm.dataset.next || "/";
  } catch (e) {
    loginStatus.textContent = e.message;
    loginStatus.classList.add("error");
    loginForm.elements.password.select();
  } finally {
    button.disabled = false;
  }
});
