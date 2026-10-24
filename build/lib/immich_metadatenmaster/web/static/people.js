"use strict";

const tbody = document.getElementById("people");
const filter = document.getElementById("filter");
const missingOnly = document.getElementById("missing-only");
let people = [];

function render() {
  const q = filter.value.trim().toLowerCase();
  const rows = people.filter(p => (!q || p.name.toLowerCase().includes(q)) && (!missingOnly.checked || !p.birth_date));
  tbody.innerHTML = rows.map(p => `
    <tr data-id="${esc(p.id)}">
      <td><img src="/api/people/${esc(p.id)}/thumbnail" alt="" loading="lazy" onerror="this.style.visibility='hidden'"></td>
      <td>${esc(p.name)}${p.hidden ? ' <span class="muted">(hidden)</span>' : ""}</td>
      <td><input type="date" value="${esc(p.birth_date || "")}" max="${new Date().toISOString().slice(0, 10)}"></td>
      <td class="status"></td>
    </tr>`).join("") || `<tr><td colspan="4" class="muted">Nobody to show.</td></tr>`;
}

tbody.addEventListener("change", async (ev) => {
  const input = ev.target.closest("input[type=date]");
  if (!input) return;
  const row = input.closest("tr");
  const status = row.querySelector(".status");
  try {
    const p = await api("PUT", `/api/people/${row.dataset.id}/birthdate`, { birth_date: input.value || null });
    people.find(x => x.id === p.id).birth_date = p.birth_date;
    status.innerHTML = '<span class="saved">saved</span>';
  } catch (e) {
    status.innerHTML = `<span class="err">${esc(e.message)}</span>`;
  }
});
filter.addEventListener("input", render);
missingOnly.addEventListener("change", render);

api("GET", "/api/people").then(p => { people = p; render(); })
  .catch(e => { tbody.innerHTML = `<tr><td colspan="4" class="err">${esc(e.message)}</td></tr>`; });
