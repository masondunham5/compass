const $ = selector => document.querySelector(selector);
const sessionsElement = $("#sessions");
const todayElement = $("#today");
const summaryElement = $("#summary");
const dialog = $("#checkin-dialog");
const planDialog = $("#plan-dialog");
const planReviewDialog = $("#plan-review-dialog");
const editorDialog = $("#session-editor-dialog");
const healthDialog = $("#health-dialog");
let overview = null;
let healthData = null;
let importedActivities = [];
let timerId = null;
let timerSeconds = 90;
let installPrompt = null;
let editingSessionId = null;
let activePlan = null;

async function api(path, options = {}) {
  const headers = options.body instanceof FormData ? {} : { "Content-Type": "application/json" };
  const response = await fetch(path, { headers, ...options });
  if (!response.ok) {
    let message = "Could not save your change.";
    try { message = (await response.json()).detail || message; } catch { /* response did not include JSON */ }
    throw new Error(message);
  }
  return response.json();
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" })[character]);
}

function displayDate(value) {
  return new Date(`${value}T12:00:00`).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

function displayWeight(value) { return Number(value || 0).toLocaleString(undefined, { maximumFractionDigits: 1 }); }
function formatPace(seconds) { if (!Number.isFinite(seconds) || seconds <= 0) return "—"; return `${Math.floor(seconds / 60)}:${String(Math.round(seconds % 60)).padStart(2, "0")} / mi`; }

function selectView(view) {
  document.querySelectorAll(".view").forEach(element => element.classList.toggle("active", element.id === `view-${view}`));
  document.querySelectorAll("[data-view]").forEach(button => button.classList.toggle("active", button.dataset.view === view));
  const titles = { plan: "Your health today", health: "Recovery & body", import: "Garmin activity import", routines: "Training plan", track: "Active workout", history: "Training history", insights: "Strength progress", settings: "Settings" };
  $("#page-title").textContent = titles[view] || "Health Hub";
}

function openDialog(target) {
  if (!target) return;
  if (typeof target.showModal === "function") target.showModal();
  else target.setAttribute("open", "");
}

function openPlanBuilder(plan = null) {
  activePlan = plan;
  const config = plan?.config;
  $("#plan-dialog-heading").textContent = plan ? "Adjust your active block" : "Build your next block";
  $("#plan-dialog-copy").textContent = plan ? "Review the changes first. Applying them only refreshes future unlocked sessions; completed, locked, and logged sessions stay intact." : "Start with an honest baseline. You will review every session before it becomes active.";
  $("#plan-name").value = config?.name || plan?.name || "Strength & endurance rebuild";
  $("#plan-goals").value = (config?.goals || plan?.goals || ["Build strength", "Build muscle", "Improve running endurance"]).join("\n");
  $("#plan-split").value = config?.split || "body-part-balanced";
  $("#plan-mobility").checked = Boolean(config?.include_mobility);
  $("#plan-weeks").value = config?.weeks || 10;
  $("#plan-lifting-days").value = config?.lifting_days || 5;
  $("#plan-run-days").value = config?.run_days ?? 3;
  $("#plan-miles").value = config?.starting_miles ?? 6;
  $("#plan-minutes").value = config?.available_minutes || 60;
  $("#plan-avoid").value = config?.avoid || "Bulgarian split squats";
  $("#plan-form button[type='submit']").textContent = plan ? "Review future changes" : "Create review draft";
  $("#plan-form-status").hidden = true;
  $("#plan-split").dispatchEvent(new Event("change"));
  openDialog(planDialog);
}

function sessionCard(session, compact = false) {
  const element = document.createElement("article");
  element.className = `session ${session.status}`;
  const action = session.status === "planned" ? '<button class="outline manage">Manage</button><button class="outline start">Start</button>' : session.status === "cancelled" ? '<span class="cancelled">Cancelled</span><button class="outline manage">Edit</button>' : '<button class="outline review">Review</button>';
  element.innerHTML = `<div class="session-date"><b>${new Date(`${session.date}T12:00:00`).toLocaleDateString(undefined, { weekday: "short" })}</b><span>${new Date(`${session.date}T12:00:00`).getDate()}</span></div><div class="session-content"><span class="tag ${escapeHtml(session.kind)}">${escapeHtml(session.kind)}</span><strong>${escapeHtml(session.title)}</strong><small>${session.minutes} min · ${escapeHtml(session.description || "Training session")}</small></div><div class="actions"><button class="lock" title="${session.locked ? "Unlock" : "Lock"} this workout">${session.locked ? "🔒" : "◌"}</button>${action}</div>`;
  element.querySelector(".lock").onclick = async () => { await api(`/api/sessions/${encodeURIComponent(session.id)}/lock`, { method: "POST" }); await load(); };
  const start = element.querySelector(".start");
  if (start) start.onclick = () => openSession(session.id);
  const manage = element.querySelector(".manage");
  if (manage) manage.onclick = () => openSessionEditor(session);
  const review = element.querySelector(".review");
  if (review) review.onclick = () => openSession(session.id);
  if (compact) element.classList.add("compact");
  return element;
}

function renderSessions(data) {
  if (!data.sessions.length) {
    sessionsElement.innerHTML = '<div class="empty-list"><span>◌</span><b>No upcoming sessions yet</b><p>Your phase plan will appear here after it is added to Lifting Hub.</p></div>';
    $("#routine-list").innerHTML = '<div class="empty-list"><b>No routine library yet</b><p>Once your 10-week block is added, its workouts will be reusable here.</p></div>';
    return;
  }
  sessionsElement.replaceChildren(...data.sessions.slice(0, 8).map(sessionCard));
  const routines = $("#routine-list");
  routines.replaceChildren(...data.sessions.map(session => sessionCard(session, true)));
}

function renderBalance(data, insights) {
  const planned = data.sessions.filter(session => session.status === "planned").length;
  $("#balance-sessions").textContent = `${data.completed} / ${data.completed + planned}`;
  $("#session-progress").style.width = `${Math.min(100, (data.completed / Math.max(1, data.completed + planned)) * 100)}%`;
  $("#balance-volume").textContent = insights ? `${Number(insights.volume || 0).toLocaleString()} lb` : "0 lb";
  $("#balance-title").textContent = planned ? `${planned} sessions ahead` : "Week complete";
  $("#insight-sessions").textContent = data.completed;
  $("#insight-volume").textContent = `${Number(insights?.volume || 0).toLocaleString()} lb`;
  $("#insight-action").textContent = planned ? "Start your next session" : "Plan next week";
}

function weightLabel(value, delta) {
  if (!value) return "—";
  const change = delta == null || delta === 0 ? "steady" : `${delta > 0 ? "+" : ""}${delta} lb`;
  return `${value.toFixed(1)} lb · ${change}`;
}

function renderHealth(data) {
  healthData = data;
  const latest = data.latest;
  $("#health-status").textContent = latest ? `Energy ${latest.energy}/10 · sleep ${latest.sleep_hours}h` : "No check-in yet";
  $("#health-sleep").textContent = data.average_sleep ? `${data.average_sleep} h` : "—";
  $("#health-weight").textContent = weightLabel(data.weight_lbs, data.weight_delta);
  $("#health-readiness").textContent = latest ? `Energy ${latest.energy}/10 · soreness ${latest.soreness}/10` : "Log a check-in";
  $("#health-recommendation").textContent = data.recommendation;
  $("#health-checkins").textContent = `${data.checkins_this_week} / 7`;
  $("#health-average-sleep").textContent = data.average_sleep ? `${data.average_sleep} h` : "—";
  $("#health-run-miles").textContent = `${data.imported_run_miles || 0} mi`;
  const proteinDays = latest?.protein_goal_met ? "Met today" : latest ? "Not logged today" : "—";
  $("#health-protein-status").textContent = proteinDays;
  const target = $("#health-history-list");
  if (!latest) {
    target.innerHTML = '<div class="empty-list"><span>♡</span><b>Your first check-in takes under a minute</b><p>Sleep, energy, soreness, stress, and optional weight create useful context without turning the app into a medical tracker.</p></div>';
    return;
  }
  target.innerHTML = data.records.map((record, index) => `<article class="health-log-card"><div><p class="eyebrow">${index === 0 ? "LATEST CHECK-IN" : "HEALTH CHECK-IN"}</p><h3>${displayDate(record.checkin_date)}</h3><p>${record.notes ? escapeHtml(record.notes) : "No notes added."}</p></div><div class="health-stat-pills"><span>Sleep ${record.sleep_hours}h</span><span>Energy ${record.energy}/10</span><span>Soreness ${record.soreness}/10</span><span>Stress ${record.stress}/10</span>${record.weight_lbs ? `<span>${record.weight_lbs} lb</span>` : ""}</div></article>`).join("");
}

function renderActivities(data) {
  importedActivities = data.activities;
  const target = $("#activity-list");
  if (!data.activities.length) {
    target.innerHTML = '<div class="empty-list"><span>↥</span><b>No Garmin activities imported</b><p>Export a run from Garmin Connect as CSV, TCX, or GPX, then import it here. You will approve any link to a planned run.</p></div>';
    return;
  }
  target.innerHTML = data.activities.map(activity => `<article class="activity-card"><div><p class="eyebrow">${escapeHtml(activity.source)} · ${escapeHtml(activity.activity_type)}</p><h3>${escapeHtml(activity.title)}</h3><p>${displayDate(activity.activity_date)} · ${activity.distance_miles ? `${activity.distance_miles.toFixed(2)} mi` : "Distance unavailable"}${activity.duration_minutes ? ` · ${Math.round(activity.duration_minutes)} min` : ""}${activity.average_hr ? ` · avg HR ${activity.average_hr}` : ""}</p></div>${activity.linked_session_id ? '<span class="done">Linked to plan</span>' : '<button class="outline link-activity" data-activity-id="">Link planned run</button>'}</article>`).join("");
  target.querySelectorAll(".link-activity").forEach((button, index) => {
    button.onclick = async () => {
      const activity = data.activities[index];
      button.disabled = true; button.textContent = "Linking…";
      try { const result = await api(`/api/activities/${encodeURIComponent(activity.id)}/link`, { method: "POST" }); $("#import-status").textContent = `Linked to ${result.session_title}.`; await load(); }
      catch (error) { $("#import-status").textContent = error.message; button.disabled = false; button.textContent = "Link planned run"; }
    };
  });
}

function renderHistory(data) {
  const target = $("#history-list");
  if (!data.sessions.length) {
    target.innerHTML = '<div class="empty-list"><span>◷</span><b>No finished workouts yet</b><p>Once you finish a session, its exercises and logged sets will remain available here.</p></div>';
    return;
  }
  target.innerHTML = data.sessions.map(session => { const run = session.metrics?.distance_miles ? `${session.metrics.distance_miles} mi · ${formatPace(session.metrics.pace_seconds_per_mile)} · effort ${session.metrics.effort}/10` : `${session.sets_logged} logged sets · ${session.minutes} min planned`; return `<article class="history-card"><div><p class="eyebrow">${escapeHtml(session.kind)} · ${displayDate(session.date)}</p><h3>${escapeHtml(session.title)}</h3><p>${escapeHtml(session.description || "Completed workout")}</p><small>${run}</small></div><button class="outline review-workout" data-session-id="${escapeHtml(session.id)}">Review</button></article>`; }).join("");
  target.querySelectorAll(".review-workout").forEach(button => button.onclick = () => openSession(button.dataset.sessionId));
}

function showExerciseDetail(exercise) {
  const detail = $("#exercise-detail");
  const history = exercise.history.slice(-12);
  const max = Math.max(...history.map(point => point.e1rm), 1);
  detail.hidden = false;
  detail.innerHTML = `<div class="detail-heading"><div><p class="eyebrow">EXERCISE HISTORY</p><h3>${escapeHtml(exercise.name)}</h3><p>Best estimated 1RM: <b>${exercise.best_set.e1rm.toFixed(1)} lb</b> from ${displayWeight(exercise.best_set.weight)} lb × ${exercise.best_set.reps}</p></div><button class="lock" id="close-exercise-detail">×</button></div><div class="mini-chart">${history.map(point => `<span title="${point.date}: ${point.e1rm.toFixed(1)} lb e1RM" style="height:${Math.max(10, (point.e1rm / max) * 100)}%"></span>`).join("")}</div><div class="progress-table"><div><b>Date</b><b>Best set</b><b>e1RM</b><b>RPE</b></div>${history.slice().reverse().map(point => `<div><span>${displayDate(point.date)}</span><span>${displayWeight(point.weight)} lb × ${point.reps}</span><span>${point.e1rm.toFixed(1)} lb</span><span>${point.rpe ?? "—"}</span></div>`).join("")}</div>`;
  detail.querySelector("#close-exercise-detail").onclick = () => { detail.hidden = true; };
  detail.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderProgress(data) {
  const target = $("#progress-list");
  $("#exercise-detail").hidden = true;
  if (!data.exercises.length) {
    target.innerHTML = '<div class="empty-list"><span>⌁</span><b>Progress starts with your first logged set</b><p>Log a working set and Lifting Hub will track its best set and estimated 1RM.</p></div>';
    return;
  }
  target.innerHTML = data.exercises.map((exercise, index) => `<button class="progress-card" data-exercise="${index}"><span>${escapeHtml(exercise.name)}</span><strong>${exercise.best_set.e1rm.toFixed(1)} lb</strong><small>Best: ${exercise.best_set.weight} lb × ${exercise.best_set.reps} · ${exercise.history.length} logged sets</small></button>`).join("");
  target.querySelectorAll(".progress-card").forEach(button => button.onclick = () => showExerciseDetail(data.exercises[Number(button.dataset.exercise)]));
}

function renderPlanReview(plan) {
  const sessionsByWeek = plan.phases.map(phase => ({ ...phase, sessions: plan.sessions.filter(session => Math.floor((new Date(`${session.date}T12:00:00`) - new Date(`${plan.start_date}T12:00:00`)) / 86400000 / 7) + 1 === phase.week) }));
  const revision = Boolean(plan.revision_of);
  $("#plan-review").innerHTML = `<button class="dialog-close" id="close-review" type="button" aria-label="Close">×</button><p class="eyebrow">${revision ? "FUTURE-PLAN PREVIEW — NOTHING CHANGED YET" : "DRAFT — NOTHING HAS BEEN SCHEDULED"}</p><h2>${escapeHtml(plan.name)}</h2><p class="muted">Starts ${displayDate(plan.start_date)} · ${plan.config.weeks} weeks · ${plan.sessions.length} sessions</p><section class="goal-summary"><b>Priorities</b><ol>${plan.goals.map(goal => `<li>${escapeHtml(goal)}</li>`).join("")}</ol><small>Split: ${escapeHtml(plan.config.split.replace(/-/g, " "))} · Dedicated mobility: ${plan.config.include_mobility ? "included" : "not included"}</small></section><section class="phase-preview">${sessionsByWeek.map(phase => `<article><span>WEEK ${phase.week}</span><h3>${escapeHtml(phase.name)}</h3><p>${escapeHtml(phase.message)}</p><small>${phase.sessions.map(session => escapeHtml(session.title.replace(/^W\d+ · /, ""))).join(" · ")}</small></article>`).join("")}</section><p class="review-note">${revision ? "Applying this keeps completed, locked, and already logged sessions, then rebuilds only the remaining flexible portion of the block." : "Review the block above. Activate only when you are comfortable with its starting structure; you can still change individual workouts afterward."}</p><div class="review-actions"><button class="secondary" id="edit-draft" type="button">Edit inputs</button><button class="primary" id="activate-draft" type="button">${revision ? "Apply future changes" : "Activate this block"}</button></div>`;
  $("#close-review").onclick = () => planReviewDialog.close();
  $("#edit-draft").onclick = () => { planReviewDialog.close(); openDialog(planDialog); };
  $("#activate-draft").onclick = async () => {
    const button = $("#activate-draft"); button.disabled = true; button.textContent = "Activating…";
    const endpoint = revision ? `/api/plans/${encodeURIComponent(plan.revision_of)}/revise` : `/api/plans/${encodeURIComponent(plan.id)}/activate`;
    const options = revision ? { method: "POST", body: JSON.stringify(plan.config) } : { method: "POST" };
    try {
      await api(endpoint, options);
      planReviewDialog.close(); await load(); selectView("plan");
      $("#suggestion-title").textContent = revision ? "Future plan updated" : "Block activated";
      $("#suggestion-copy").textContent = revision ? "Your remaining unlocked sessions now reflect the changes. Completed, logged, and locked workouts stayed where they were." : "Your draft is now active. Log sessions and use readiness check-ins to guide day-to-day adjustments.";
    } catch (error) {
      const note = $("#plan-review .review-note");
      note.textContent = error.message || "The plan could not be applied. Nothing was changed.";
      note.classList.add("form-status");
    } finally {
      button.disabled = false;
      button.textContent = revision ? "Apply future changes" : "Activate this block";
    }
  };
}

async function submitPlanDraft(event) {
  event.preventDefault();
  const goals = $("#plan-goals").value.split("\n").map(goal => goal.trim()).filter(Boolean);
  const payload = {
    name: $("#plan-name").value.trim() || "My next training block", goals,
    weeks: Number($("#plan-weeks").value), lifting_days: Number($("#plan-lifting-days").value),
    run_days: Number($("#plan-run-days").value), starting_miles: Number($("#plan-miles").value),
    available_minutes: Number($("#plan-minutes").value), avoid: $("#plan-avoid").value.trim(), split: $("#plan-split").value, include_mobility: $("#plan-mobility").checked, equipment: "Full gym",
  };
  const submit = event.submitter; const submitLabel = activePlan ? "Review future changes" : "Create review draft"; submit.disabled = true; submit.textContent = "Building draft…";
  try { const endpoint = activePlan ? `/api/plans/${encodeURIComponent(activePlan.id)}/revision-preview` : "/api/plans/draft"; const plan = await api(endpoint, { method: "POST", body: JSON.stringify(payload) }); planDialog.close(); renderPlanReview(plan); openDialog(planReviewDialog); }
  catch (error) { const status = $("#plan-form-status"); status.textContent = error.message || "The plan could not be generated. Please check the inputs and try again."; status.hidden = false; }
  finally { submit.disabled = false; submit.textContent = submitLabel; }
}

async function load() {
  const [data, insights, history, progress, plans, health, activities] = await Promise.all([api("/api/overview"), api("/api/insights"), api("/api/history"), api("/api/progress"), api("/api/plans"), api("/api/health/summary"), api("/api/activities")]);
  overview = data;
  todayElement.textContent = new Date(`${data.today}T12:00:00`).toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
  summaryElement.textContent = `${data.completed} completed · ${data.sessions.length} ahead`;
  renderSessions(data);
  renderBalance(data, insights);
  renderHistory(history);
  renderProgress(progress);
  renderHealth(health);
  renderActivities(activities);
  activePlan = plans.plans.find(plan => plan.status === "active") || null;
  $("#adjust-plan-button").hidden = !activePlan;
}

function formatTimer() { return `${String(Math.floor(timerSeconds / 60)).padStart(2, "0")}:${String(timerSeconds % 60).padStart(2, "0")}`; }
function stopTimer() { clearInterval(timerId); timerId = null; }
function startTimer() {
  stopTimer(); timerSeconds = 90; updateTimer();
  timerId = setInterval(() => { timerSeconds -= 1; updateTimer(); if (timerSeconds <= 0) stopTimer(); }, 1000);
}
function updateTimer() { const output = $("#rest-clock"); if (output) output.textContent = formatTimer(); }

function setRow(sessionId, exercise, setNumber, logged) {
  const weight = logged?.weight ?? exercise.suggested_weight ?? "";
  const reps = logged?.reps ?? exercise.suggested_reps ?? "";
  return `<div class="set-row" data-exercise-index="${exercise.index}" data-set-number="${setNumber}"><span>${setNumber}</span><input aria-label="Weight for set ${setNumber}" type="number" value="${weight}" min="0" step="2.5"><input aria-label="Reps for set ${setNumber}" type="number" value="${reps}" min="0" placeholder="${exercise.target_reps}"><input aria-label="RPE for set ${setNumber}" type="number" value="${logged?.rpe ?? ""}" min="1" max="10" step=".5" placeholder="RPE"><button class="add-weight" type="button">+5</button><button class="set-check ${logged ? "logged" : ""}" type="button">${logged ? "✓" : "Log"}</button></div>`;
}

function openSessionEditor(session) {
  editingSessionId = session.id;
  $("#editor-heading").textContent = session.status === "cancelled" ? "Reopen or edit workout" : "Manage workout";
  $("#editor-title").value = session.title;
  $("#editor-date").value = session.date;
  $("#editor-minutes").value = session.minutes;
  $("#editor-description").value = session.description || "";
  $("#editor-workout").value = session.workout.join("\n");
  $("#editor-cancel-session").textContent = session.status === "cancelled" ? "Restore workout" : "Cancel workout";
  openDialog(editorDialog);
}

async function saveSessionEditor(event) {
  event.preventDefault();
  if (!editingSessionId) return;
  const workout = $("#editor-workout").value.split("\n").map(line => line.trim()).filter(Boolean);
  const payload = { session_date: $("#editor-date").value, title: $("#editor-title").value.trim(), minutes: Number($("#editor-minutes").value), description: $("#editor-description").value.trim(), workout, status: "planned" };
  const submit = event.submitter; submit.disabled = true; submit.textContent = "Saving…";
  try { await api(`/api/sessions/${encodeURIComponent(editingSessionId)}`, { method: "PATCH", body: JSON.stringify(payload) }); editorDialog.close(); await load(); selectView("plan"); }
  finally { submit.disabled = false; submit.textContent = "Save changes"; }
}

function openRunSession(session, target) {
  const targetMiles = Number((session.description.match(/target\s+([\d.]+)\s+mi/i) || [])[1] || "");
  target.className = "active-session run-session";
  target.innerHTML = `<div class="active-header"><button class="back" type="button">← Plan</button><div><p class="eyebrow">RUN SESSION</p><h2>${escapeHtml(session.title)}</h2><span>${displayDate(session.date)} · ${session.minutes} min planned</span></div>${session.status === "completed" ? "" : '<button class="secondary manage-active" type="button">Manage</button>'}<span class="session-status">${session.status === "completed" ? "Completed" : "Ready to run"}</span></div><section class="run-brief"><p class="eyebrow">TODAY'S TARGET</p><h3>${targetMiles ? `${targetMiles.toFixed(1)} mi easy run` : "Run by easy effort"}</h3><p>${escapeHtml(session.description || "Keep the effort conversational.")}</p></section>${session.status === "completed" ? '<div class="empty-list"><span>✓</span><b>This run is in your history</b><p>Open History to review its distance, pace, heart rate, and effort.</p></div>' : `<form id="run-log-form" class="run-log"><div class="run-field-grid"><label>Distance <span>miles</span><input id="run-distance" type="number" min=".1" step=".01" value="${targetMiles || ""}" required></label><label>Time <span>minutes</span><input id="run-duration" type="number" min="1" step=".1" required></label><label>Average HR <span>optional</span><input id="run-average-hr" type="number" min="30" max="260"></label><label>Maximum HR <span>optional</span><input id="run-max-hr" type="number" min="30" max="260"></label><label>Perceived effort <span>1 easy — 10 maximal</span><input id="run-effort" type="number" min="1" max="10" value="5" required></label></div><div class="pace-readout"><span>PACE</span><b id="run-pace-preview">—</b></div><label>Run notes <textarea id="run-notes" rows="3" placeholder="How did it feel? Terrain, weather, or anything worth remembering."></textarea></label><button class="primary full" type="submit">Log run &amp; finish</button></form>`}`;
  target.querySelector(".back").onclick = () => selectView("plan");
  const manage = target.querySelector(".manage-active");
  if (manage) manage.onclick = () => openSessionEditor(session);
  const form = target.querySelector("#run-log-form");
  if (!form) return;
  const updatePace = () => { $("#run-pace-preview").textContent = formatPace(Number($("#run-duration").value) * 60 / Number($("#run-distance").value)); };
  target.querySelectorAll("#run-distance, #run-duration").forEach(input => input.oninput = updatePace);
  updatePace();
  form.onsubmit = async event => {
    event.preventDefault();
    const submit = form.querySelector('button[type="submit"]'); submit.disabled = true; submit.textContent = "Saving run…";
    try {
      await api(`/api/sessions/${encodeURIComponent(session.id)}/log-run`, { method: "POST", body: JSON.stringify({ distance_miles: Number($("#run-distance").value), duration_minutes: Number($("#run-duration").value), average_hr: $("#run-average-hr").value ? Number($("#run-average-hr").value) : null, max_hr: $("#run-max-hr").value ? Number($("#run-max-hr").value) : null, effort: Number($("#run-effort").value), notes: $("#run-notes").value.trim() }) });
      await load(); selectView("plan");
    } finally { submit.disabled = false; submit.textContent = "Log run & finish"; }
  };
}

async function openSession(sessionId) {
  const session = await api(`/api/sessions/${encodeURIComponent(sessionId)}`);
  selectView("track");
  const target = $("#active-session");
  if (session.kind === "run") { openRunSession(session, target); return; }
  target.className = "active-session";
  target.innerHTML = `<div class="active-header"><button class="back" type="button">← Plan</button><div><p class="eyebrow">WORKOUT SESSION</p><h2>${escapeHtml(session.title)}</h2><span>${displayDate(session.date)} · ${session.minutes} min</span></div>${session.status === "completed" ? "" : '<button class="secondary manage-active" type="button">Manage</button>'}<span class="session-status">${session.status === "completed" ? "Completed" : "In progress"}</span></div><div class="workout-progress"><i></i></div><div class="exercise-list">${session.exercises.map(exercise => `<article class="exercise"><div class="exercise-heading"><div><p class="eyebrow">EXERCISE ${exercise.index + 1} · ${exercise.target_sets} WORKING SETS</p><h3>${escapeHtml(exercise.name)}</h3><span>${escapeHtml(exercise.prescription || "Use a controlled working set")}</span><p class="starting-guide">${escapeHtml(exercise.starting_guide)}</p></div><span class="last-lift">Last: ${exercise.suggested_weight ? `${exercise.suggested_weight} lb × ${exercise.suggested_reps}` : "No baseline"}</span></div><div class="set-table"><div class="set-labels"><span>SET</span><span>WEIGHT</span><span>REPS<br><small>${exercise.target_reps}</small></span><span>RPE</span><span></span><span>STATUS</span></div>${Array.from({ length: exercise.target_sets }, (_, index) => setRow(session.id, exercise, index + 1, exercise.sets?.find(log => log.set_number === index + 1))).join("")}</div></article>`).join("")}</div><div class="rest-timer"><button id="timer-toggle" type="button">◷ <b id="rest-clock">01:30</b><span>REST TIMER</span>${session.status === "completed" ? "" : '<button class="primary" id="finish-session">Finish workout</button>'}</div>`;
  target.querySelector(".back").onclick = () => selectView("plan");
  const manageActive = target.querySelector(".manage-active");
  if (manageActive) manageActive.onclick = () => openSessionEditor(session);
  target.querySelector("#timer-toggle").onclick = () => timerId ? stopTimer() : startTimer();
  target.querySelectorAll(".set-row").forEach(row => {
    const exerciseIndex = Number(row.dataset.exerciseIndex);
    const setNumber = Number(row.dataset.setNumber);
    row.querySelector(".add-weight").onclick = () => { const input = row.querySelector('input[type="number"]'); input.value = Number(input.value || 0) + 5; };
    row.querySelector(".set-check").onclick = async () => {
      const inputs = row.querySelectorAll("input");
      await api(`/api/sessions/${encodeURIComponent(session.id)}/sets`, { method: "PUT", body: JSON.stringify({ exercise_index: exerciseIndex, set_number: setNumber, weight: Number(inputs[0].value), reps: Number(inputs[1].value), rpe: inputs[2].value ? Number(inputs[2].value) : null, note: "" }) });
      row.querySelector(".set-check").classList.add("logged"); row.querySelector(".set-check").textContent = "✓"; startTimer();
    };
  });
  const finish = target.querySelector("#finish-session");
  if (finish) finish.onclick = async () => { await api(`/api/sessions/${encodeURIComponent(session.id)}/complete`, { method: "POST", body: JSON.stringify({ minutes: session.minutes, notes: "Logged in Lifting Hub", metrics: {} }) }); stopTimer(); await load(); selectView("plan"); };
}

document.querySelectorAll("[data-view]").forEach(button => button.addEventListener("click", () => selectView(button.dataset.view)));
$("#checkin-button").onclick = () => openDialog(dialog);
$("#quick-checkin").onclick = () => openDialog(dialog);
$("#health-checkin-button").onclick = () => openDialog(healthDialog);
$("#health-checkin-button-secondary").onclick = () => openDialog(healthDialog);
$("#open-health-button").onclick = () => selectView("health");
$("#build-plan-button").onclick = () => openPlanBuilder();
$("#adjust-plan-button").onclick = () => openPlanBuilder(activePlan);
$("#plan-form").addEventListener("submit", submitPlanDraft);
planDialog.querySelector(".dialog-close").onclick = () => planDialog.close();
$("#session-editor-form").addEventListener("submit", saveSessionEditor);
editorDialog.querySelector(".dialog-close").onclick = () => editorDialog.close();
healthDialog.querySelector(".dialog-close").onclick = () => healthDialog.close();
$("#editor-add-exercise").onclick = () => { $("#editor-workout").value += `${$("#editor-workout").value ? "\n" : ""}New exercise — 3 × 8–12`; $("#editor-workout").focus(); };
$("#editor-cancel-session").onclick = async () => {
  if (!editingSessionId) return;
  const restore = $("#editor-cancel-session").textContent.includes("Restore");
  await api(`/api/sessions/${encodeURIComponent(editingSessionId)}`, { method: "PATCH", body: JSON.stringify({ status: restore ? "planned" : "cancelled" }) });
  editorDialog.close(); await load(); selectView("plan");
};
$("#plan-split").onchange = event => {
  const help = {
    "body-part-balanced": "Separate chest/triceps/shoulders, back/biceps, and lower-body sessions. With five days, chest/tri/shoulders receives a second volume day.",
    "push-pull-legs": "A familiar push/pull/legs rotation; useful if you prefer simple repeated patterns.",
    "balanced-upper-lower": "Alternates upper and lower emphasis with a balanced amount of pressing and pulling.",
    "full-body": "Fewer, more comprehensive sessions; useful if you want flexibility around a busy week.",
  };
  $("#split-help").textContent = help[event.target.value];
};
$("#submit-checkin").onclick = async event => {
  event.preventDefault();
  const result = await api("/api/checkins", { method: "POST", body: JSON.stringify({ available_minutes: Number($("#minutes").value), readiness: Number($("#readiness").value), soreness: Number($("#soreness").value), location: $("#location").value }) });
  $("#checkin-copy").textContent = result.recommendation;
  $("#suggestion-title").textContent = "Today’s recommendation";
  $("#suggestion-copy").textContent = result.recommendation;
  dialog.close();
};
$("#health-form").onsubmit = async event => {
  event.preventDefault();
  const submit = event.submitter; submit.disabled = true; submit.textContent = "Saving…";
  try {
    const result = await api("/api/health/checkin", { method: "POST", body: JSON.stringify({
      sleep_hours: Number($("#health-sleep-hours").value), sleep_quality: Number($("#health-sleep-quality").value),
      energy: Number($("#health-energy").value), soreness: Number($("#health-soreness").value), stress: Number($("#health-stress").value),
      weight_lbs: $("#health-weight-lbs").value ? Number($("#health-weight-lbs").value) : null,
      protein_goal_met: $("#health-protein").checked, notes: $("#health-notes").value.trim(),
    }) });
    healthDialog.close(); await load();
    $("#suggestion-title").textContent = "Health-informed suggestion";
    $("#suggestion-copy").textContent = result.recommendation;
  } catch (error) { alert(error.message); }
  finally { submit.disabled = false; submit.textContent = "Save health check-in"; }
};
$("#garmin-import-form").onsubmit = async event => {
  event.preventDefault();
  const file = $("#garmin-file").files[0];
  const status = $("#import-status");
  if (!file) { status.textContent = "Choose a CSV, TCX, or GPX activity export first."; return; }
  const submit = event.submitter; submit.disabled = true; submit.textContent = "Importing…"; status.textContent = "Reading the export locally…";
  try {
    const form = new FormData(); form.append("file", file);
    const result = await api("/api/activities/import", { method: "POST", body: form });
    status.textContent = result.imported ? `Imported ${result.imported} run${result.imported === 1 ? "" : "s"}. Review and link one only if it matches your plan.` : "No running activities were found in that file.";
    $("#garmin-file").value = ""; await load();
  } catch (error) { status.textContent = error.message; }
  finally { submit.disabled = false; submit.textContent = "Import activity"; }
};
document.addEventListener("keydown", event => { if (event.code === "Space" && $("#view-track").classList.contains("active")) { event.preventDefault(); timerId ? stopTimer() : startTimer(); } });
window.addEventListener("beforeinstallprompt", event => {
  event.preventDefault();
  installPrompt = event;
  $("#install-app").hidden = false;
});
$("#install-app").onclick = async () => {
  if (!installPrompt) return;
  installPrompt.prompt();
  await installPrompt.userChoice;
  installPrompt = null;
  $("#install-app").hidden = true;
};
window.addEventListener("appinstalled", () => { $("#sync").textContent = "● Installed locally"; });
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/static/service-worker.js");
load().catch(() => { todayElement.textContent = "Could not reach your private lifting server"; $("#sync").textContent = "○ Offline"; });
