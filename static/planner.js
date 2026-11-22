/* Planner UI: the draft/accepted plan inside the Week view, plus the School (read-only)
   and Work schedule screens. Uses app.js / assistant.js helpers. */

const plannerState = { plan: null };
const KIND_LABEL = { study: "Study", review: "Review", workout: "Train" };

async function renderWeekPlan(startIso) {
  const today = state.today.today;
  const start = startIso < today ? today : startIso;
  const end = new Date(asDate(startIso).getTime() + 6 * 86400000).toISOString().slice(0, 10);
  const summary = $("#plan-summary");
  if (start > end) { summary.innerHTML = ""; return; }
  const days = Math.round((asDate(end) - asDate(start)) / 86400000) + 1;
  const plan = await api(`/api/assistant/plan?start=${start}&days=${days}`);
  plannerState.plan = plan;
  const drafts = plan.days.flatMap(day => day.blocks.filter(block => block.status === "draft"));
  const school = plan.school;
  summary.innerHTML = `<article class="card plan-card">
      <div class="plan-top"><div><b>Plan for ${start === today ? "the rest of this week" : "this week"}</b>
        <small>${school.connected ? `${school.open} open school tasks · ${Math.round(school.open_minutes / 60)} h estimated` : esc(school.message || "School To Do not connected")}</small></div>
        ${drafts.length ? `<button class="btn btn-primary small" type="button" id="accept-week">Accept ${drafts.length} block${drafts.length === 1 ? "" : "s"}</button>` : `<span class="faint">All set</span>`}</div>
      ${plan.warnings.length ? `<ul class="plan-warnings">${plan.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
      <p class="faint">Dashed blocks are suggestions. Accepting makes them part of your schedule; nothing changes in School To Do or your calendars.</p></article>`;
  const acceptAll = $("#accept-week");
  if (acceptAll) acceptAll.onclick = () => acceptBlocks(drafts, acceptAll);
  for (const day of plan.days) {
    const card = $(`#week-days .week-day-head[data-day="${day.date}"]`)?.closest(".week-day");
    if (!card) continue;
    card.querySelector(".plan-list")?.remove();
    const rows = [
      ...day.work.map(shift => ({ sort: shift.start, html: `<li class="fixed"><time>${esc(shift.start)}</time><span>${esc(shift.title)} · until ${esc(shift.end)}</span></li>` })),
      ...day.blocks.map(block => ({ sort: block.start, html: `<li class="${esc(block.kind)} ${esc(block.status)}" data-block="${esc(block.id)}"><time>${esc(block.start_label)}</time><span><em>${KIND_LABEL[block.kind]}</em>${esc(block.title)} <small>${block.end - block.start} min</small></span><button type="button" aria-label="${block.status === "draft" ? "Accept" : "Remove"}">${block.status === "draft" ? "✓" : "×"}</button></li>` })),
    ];
    if (!rows.length) continue;
    const toMinutes = label => { const [, h, m, ap] = String(label).match(/(\d+):(\d+)\s*(AM|PM)/) || [0, 0, 0, "AM"]; return (Number(h) % 12 + (ap === "PM" ? 12 : 0)) * 60 + Number(m); };
    rows.sort((a, b) => (typeof a.sort === "number" ? a.sort : toMinutes(a.sort)) - (typeof b.sort === "number" ? b.sort : toMinutes(b.sort)));
    card.insertAdjacentHTML("beforeend", `<ul class="plan-list">${rows.map(row => row.html).join("")}</ul>`);
  }
  $$("#week-days .plan-list [data-block] button").forEach(button => button.onclick = async () => {
    const id = button.closest("[data-block]").dataset.block;
    const block = plan.days.flatMap(day => day.blocks).find(item => item.id === id);
    if (block.status === "draft") await acceptBlocks([block], button);
    else { await api("/api/assistant/plan/unaccept", { method: "POST", body: JSON.stringify({ block_id: id }) }); await refreshPlanViews(); }
  });
}
window.renderWeekPlan = renderWeekPlan;

async function acceptBlocks(blocks, button) {
  if (button) button.disabled = true;
  try {
    const result = await api("/api/assistant/plan/accept", { method: "POST", body: JSON.stringify({ blocks: blocks.map(({ status, start_label, end_label, ...rest }) => rest) }) });
    await refreshPlanViews();
    if (window.showUndo && result.accepted) { /* accepted blocks can be removed individually with × */ }
  } catch (error) { alert(error.message); if (button) button.disabled = false; }
}

async function refreshPlanViews() {
  if (window.renderWeek) await window.renderWeek();
  if (window.renderAssistant) await window.renderAssistant();
}

/* ---------------- School (read-only) */

async function renderSchool() {
  const data = await api("/api/assistant/school");
  const status = data.status;
  if (!status.connected) {
    $("#school-status").textContent = `${status.message || "School To Do isn't connected."} Compass looks for it in Documents\\ai-assistant\\school_todo.`;
    $("#school-groups").innerHTML = "";
    return;
  }
  $("#school-status").textContent = `Read from School To Do · ${status.open} open · ${Math.round(status.open_minutes / 60)} h estimated. Check tasks off and edit estimates there; Compass never changes them.`;
  const today = state.today.today;
  const weekEnd = new Date(asDate(today).getTime() + 7 * 86400000).toISOString().slice(0, 10);
  const groups = [
    ["Overdue", data.tasks.filter(t => t.overdue && t.kind !== "exam")],
    ["Next 7 days", data.tasks.filter(t => !t.overdue && t.due_date <= weekEnd)],
    ["Later", data.tasks.filter(t => !t.overdue && t.due_date > weekEnd)],
  ];
  const fmtTime = hhmm => { const [h, m] = hhmm.split(":").map(Number); return `${(h % 12) || 12}:${String(m).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`; };
  $("#school-groups").innerHTML = groups.filter(([, tasks]) => tasks.length).map(([label, tasks]) => `<div><h2 class="section-title">${label} · ${tasks.length}</h2><article class="card list-card">${tasks.map(task => `
      <div class="row-item school-task ${task.kind}"><div class="date-chip"><span>${esc(fmtDow(task.due_date))}</span><b>${asDate(task.due_date).getDate()}</b></div>
        <div class="grow"><b>${esc(task.title)}</b><small>${esc(task.course)} · ${esc(task.kind)} · due ${esc(fmtTime(task.due_time))}${task.minutes ? ` · ~${task.minutes} min` : ""}${task.planned_minutes ? ` · ${task.planned_minutes} min planned` : ""}</small></div>
        ${task.overdue ? `<button type="button" class="chip${task.forced ? " active" : ""}" data-force="${esc(task.key)}" data-on="${task.forced ? 0 : 1}">${task.forced ? "Planned" : "Plan it anyway"}</button>` : ""}</div>`).join("")}</article></div>`).join("")
    || `<article class="card empty"><b>Nothing open</b>Every School To Do task is checked off.</article>`;
  $$("[data-force]").forEach(button => button.onclick = async () => {
    await api("/api/assistant/school/force", { method: "POST", body: JSON.stringify({ key: button.dataset.force, on: button.dataset.on === "1" }) });
    renderSchool();
  });
  const note = $("#more-school-note"); if (note) note.textContent = `${status.open} open · ${status.overdue} overdue`;
}
window.renderSchool = renderSchool;

/* ---------------- Work schedule */

let shiftMode = "weekly";

async function renderWork(data = null) {
  data = data || await api("/api/assistant/work");
  $("#work-status").textContent = `Today counts as a ${data.today_kind === "break" ? "break" : "school term"} day. Shifts are fixed time the planner works around.`;
  const timeLabel = hhmm => { const [h, m] = hhmm.split(":").map(Number); return `${(h % 12) || 12}:${String(m).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`; };
  const appliesLabel = { school: "school terms", break: "breaks", always: "always" };
  $("#shift-list").innerHTML = data.shifts.length ? data.shifts.map(shift => `<div class="row-item"><div class="grow"><b>${shift.weekday_name ? `Every ${esc(shift.weekday_name)}` : esc(fmtLong(shift.shift_date))}</b><small>${esc(timeLabel(shift.start))}–${esc(timeLabel(shift.end))}${shift.weekday_name ? ` · during ${appliesLabel[shift.applies]}` : ""}</small></div><div class="record-actions"><button type="button" data-shift-delete="${shift.id}">Remove</button></div></div>`).join("")
    : `<div class="empty"><b>No shifts yet</b>Add your regular weekly shifts, and one-off shifts for breaks.</div>`;
  $("#term-list").innerHTML = data.terms.length ? data.terms.map(term => `<div class="row-item"><div class="grow"><b>${esc(term.name)}</b><small>${term.kind === "break" ? "Break" : "School term"} · ${esc(fmtShort(term.start_date))} – ${esc(fmtShort(term.end_date))}</small></div><div class="record-actions"><button type="button" data-term-delete="${term.id}">Remove</button></div></div>`).join("")
    : `<div class="empty"><b>No terms defined</b>Every day is treated as a school term.</div>`;
  $$("[data-shift-delete]").forEach(button => button.onclick = async () => renderWork(await api(`/api/assistant/work/shifts/${button.dataset.shiftDelete}`, { method: "DELETE" })));
  $$("[data-term-delete]").forEach(button => button.onclick = async () => renderWork(await api(`/api/assistant/work/terms/${button.dataset.termDelete}`, { method: "DELETE" })));
}
window.renderWork = renderWork;

$$("#shift-mode [data-mode]").forEach(button => button.onclick = () => {
  shiftMode = button.dataset.mode;
  $$("#shift-mode [data-mode]").forEach(b => b.classList.toggle("active", b === button));
  $("#shift-weekday-label").hidden = shiftMode !== "weekly"; $("#shift-applies-label").hidden = shiftMode !== "weekly";
  $("#shift-date-label").hidden = shiftMode !== "once";
});
$("#shift-form").addEventListener("submit", async event => {
  event.preventDefault();
  const body = { start: $("#shift-start").value, end: $("#shift-end").value, note: "Work" };
  if (shiftMode === "weekly") Object.assign(body, { weekday: Number($("#shift-weekday").value), applies: $("#shift-applies").value });
  else body.shift_date = $("#shift-date").value;
  try { renderWork(await api("/api/assistant/work/shifts", { method: "POST", body: JSON.stringify(body) })); $("#shift-status").hidden = true; }
  catch (error) { $("#shift-status").textContent = error.message; $("#shift-status").hidden = false; }
});
$("#term-form").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    renderWork(await api("/api/assistant/work/terms", { method: "POST", body: JSON.stringify({ name: $("#term-name").value.trim(), kind: $("#term-kind").value, start_date: $("#term-start").value, end_date: $("#term-end").value }) }));
    $("#term-status").hidden = true; $("#term-form").reset();
  } catch (error) { $("#term-status").textContent = error.message; $("#term-status").hidden = false; }
});
