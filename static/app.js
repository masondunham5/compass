window.__healthHubBootState = "script-started";

function reportStartupIssue(detail) {
  window.__healthHubBootState = `failed: ${detail}`;
  const diagnostic = document.getElementById("startup-diagnostic");
  if (!diagnostic) return;
  diagnostic.hidden = false;
  diagnostic.textContent = `Health Hub error: ${detail}`;
}
window.addEventListener("error", event => reportStartupIssue(`${event.message || "Unknown JavaScript error"}${event.lineno ? ` (line ${event.lineno})` : ""}`));
window.addEventListener("unhandledrejection", event => reportStartupIssue(`Request failed: ${event.reason?.message || String(event.reason || "unknown")}`));

/* ---------------------------------------------------------------- helpers */

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];

async function api(path, options = {}) {
  const headers = options.body instanceof FormData ? {} : { "Content-Type": "application/json" };
  const response = await fetch(path, { headers, ...options });
  if (!response.ok) {
    let message = "Could not save your change.";
    try { message = (await response.json()).detail || message; } catch { /* not JSON */ }
    throw new Error(message);
  }
  return response.json();
}
const quietly = promise => promise.catch(() => null);

function esc(value = "") {
  return String(value ?? "").replace(/[&<>'"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" })[c]);
}
const icon = name => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const asDate = iso => new Date(`${iso}T12:00:00`);
const fmtLong = iso => asDate(iso).toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
const fmtShort = iso => asDate(iso).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
const fmtDow = iso => asDate(iso).toLocaleDateString(undefined, { weekday: "short" });
const num = value => (value === null || value === undefined || value === "" || Number.isNaN(Number(value)) ? null : Number(value));
const round = value => (num(value) === null ? "—" : Math.round(Number(value)).toLocaleString());
const cleanTitle = title => String(title || "").replace(/^W\d+\s*·\s*/, "");
const runMiles = session => num((String(session?.description || "").match(/([\d.]+)\s*mi\b/i) || [])[1]);
const titleCase = value => String(value || "").toLowerCase().replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());

function hm(seconds, compact = false) {
  const value = num(seconds);
  if (value === null) return "—";
  let hours = Math.floor(value / 3600);
  let minutes = Math.round((value % 3600) / 60);
  if (minutes === 60) { hours += 1; minutes = 0; }
  if (compact) return hours ? `${hours}h ${String(minutes).padStart(2, "0")}` : `${minutes}m`;
  return hours ? `${hours}h ${String(minutes).padStart(2, "0")}m` : `${minutes}m`;
}
function syncTime(value) {
  const parsed = value ? new Date(value) : null;
  return parsed && !Number.isNaN(parsed.getTime()) ? parsed.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "—";
}
function formatPace(seconds) { return Number.isFinite(seconds) && seconds > 0 ? `${Math.floor(seconds / 60)}:${String(Math.round(seconds % 60)).padStart(2, "0")} / mi` : "—"; }

/* ---------------------------------------------------------------- state + navigation */

const state = {
  today: null, dashboard: null, health: null, syncHistory: null, overview: null, history: null,
  activities: null, progress: null, insights: null, plans: null,
  garminDays: 7, selectedDay: null, fullSession: false, trainTab: "plan", returnView: "today",
};
let timerId = null;
let timerSeconds = 90;
let installPrompt = null;
let editingSessionId = null;
let activePlan = null;

const TAB_FOR_VIEW = { today: "today", recovery: "recovery", train: "train", workout: "train", progress: "progress", settings: "progress" };

function selectView(view) {
  $$(".view").forEach(element => element.classList.toggle("active", element.id === `view-${view}`));
  $$(".tabbar [data-view]").forEach(button => button.classList.toggle("active", button.dataset.view === TAB_FOR_VIEW[view]));
  if (view !== "workout") state.returnView = view;
  window.scrollTo({ top: 0 });
}

function openDialog(dialog) {
  if (!dialog) return;
  if (typeof dialog.showModal === "function") { if (!dialog.open) dialog.showModal(); } else dialog.setAttribute("open", "");
}

/* ---------------------------------------------------------------- data load */

async function load() {
  const [today, dashboard, health, syncHistory, overview, history, activities, progress, insights, plans] = await Promise.all([
    api("/api/today"),
    quietly(api(`/api/garmin/dashboard?days=${state.garminDays}`)),
    quietly(api("/api/health/summary")),
    quietly(api("/api/garmin/sync-history")),
    quietly(api("/api/overview")),
    quietly(api("/api/history")),
    quietly(api("/api/activities")),
    quietly(api("/api/progress")),
    quietly(api("/api/insights")),
    quietly(api("/api/plans")),
  ]);
  Object.assign(state, { today, dashboard, health, syncHistory, overview, history, activities, progress, insights, plans });
  activePlan = plans?.plans?.find(plan => plan.status === "active") || null;
  if (!state.selectedDay || !today.week.some(day => day.date === state.selectedDay)) state.selectedDay = today.today;
  renderToday();
  renderRecovery();
  renderTrain();
  renderProgress();
  renderSettings();
}

function planKicker(plan) {
  if (!plan) return "No active training block";
  return plan.in_block ? `Week ${plan.week} of ${plan.weeks}${plan.phase ? ` · ${plan.phase}` : ""}` : plan.name;
}

/* ---------------------------------------------------------------- Today */

function renderToday() {
  const t = state.today;
  $("#today-kicker").textContent = planKicker(t.plan);
  $("#today-title").textContent = fmtLong(t.today);

  $("#week-strip").innerHTML = t.week.map(day => {
    const isToday = day.date === t.today;
    const selected = day.date === state.selectedDay;
    const letter = asDate(day.date).toLocaleDateString(undefined, { weekday: "narrow" });
    const label = `${fmtLong(day.date)}: ${day.state === "done" ? "trained" : day.state === "planned" ? "session planned" : "rest day"}`;
    return `<button type="button" role="tab" class="day${isToday ? " today" : ""}${selected ? " selected" : ""}" data-day="${day.date}" aria-selected="${selected}" aria-label="${esc(label)}"><span class="dow">${letter}</span><span class="num">${asDate(day.date).getDate()}</span><i class="dot ${day.state}"></i></button>`;
  }).join("");
  $$("#week-strip .day").forEach(button => button.onclick = () => { state.selectedDay = button.dataset.day; renderToday(); });

  const selectedDay = t.week.find(day => day.date === state.selectedDay);
  if (state.selectedDay === t.today) renderTodayCard(t); else renderDayCard(selectedDay, t.today);

  renderStatStrip(t);

  const next = t.up_next;
  $("#up-next").hidden = !next;
  if (next) {
    const miles = next.kind === "run" ? runMiles(next) : null;
    $("#up-next").innerHTML = `<span>Up next · ${esc(fmtDow(next.date))}</span><b>${esc(cleanTitle(next.title))}${miles ? ` · ${miles} mi` : ""} · ${next.minutes} min</b>`;
  }
}

function linesHtml(lines, changed = false) {
  return `<ul class="lines">${lines.map(line => `<li class="${changed && line.sets !== line.planned_sets ? "changed" : ""}"><span>${esc(line.name)}</span><span>${line.sets ? `${line.sets} × ${esc(line.reps)}` : esc(line.note || "")}</span></li>`).join("")}</ul>`;
}

function runLinesHtml(session, minutes) {
  const miles = runMiles(session);
  return `<ul class="lines"><li><span>Distance</span><span>${miles ? `${miles} mi` : "By feel"}</span></li><li class="${minutes !== session.minutes ? "changed" : ""}"><span>Time</span><span>${minutes} min</span></li><li><span>Effort</span><span>Easy, conversational</span></li></ul>`;
}

function renderTodayCard(t) {
  const rec = t.recommendation;
  const focus = t.focus;
  const card = $("#today-card");
  const bannerIcon = rec.mode === "shorten" || rec.mode === "recovery" ? "alert" : "check";
  const banner = `<div class="banner ${esc(rec.mode)}">${icon(bannerIcon)}<span><b>Readiness ${rec.score}.</b> ${esc(rec.headline || rec.guidance)}</span></div>`;

  if (!focus) {
    card.innerHTML = `${banner}<div class="empty-day"><h2>Rest day</h2><p class="muted">${esc(rec.guidance)}</p></div>`;
  } else {
    const shortened = Boolean(focus.adjusted_sets || focus.adjusted_minutes) && !state.fullSession;
    const minutes = shortened && focus.adjusted_minutes ? focus.adjusted_minutes : focus.minutes;
    const body = focus.kind === "run" ? runLinesHtml(focus, minutes) : linesHtml(shortened ? focus.lines : focus.full_lines, shortened);
    const done = focus.status === "completed";
    const isRecovery = rec.mode === "recovery" && !done;
    let primary = `<button class="btn btn-primary full" id="start-focus" type="button">${done ? "Review workout" : focus.kind === "run" ? "Start run" : "Start workout"}</button>`;
    if (isRecovery) primary = `<button class="btn btn-ghost full" id="start-focus" type="button">Train anyway</button>`;
    let toggle = "";
    if ((focus.adjusted_sets || focus.adjusted_minutes) && !done) {
      toggle = `<button class="text-link" id="toggle-length" type="button">${state.fullSession ? `Use the short version (${focus.adjusted_minutes} min)` : `Do the full ${focus.minutes} min instead`}</button>`;
    }
    card.innerHTML = `${banner}<div class="session-body"><div class="session-title"><h2>${esc(cleanTitle(focus.title))}</h2><span>${done ? "Done" : `${minutes} min`}</span></div>${body}${primary}${toggle}</div>`;
    $("#start-focus").onclick = () => openSession(focus.id, { cap: shortened ? focus.adjusted_sets : null, minutes });
    const toggleButton = $("#toggle-length");
    if (toggleButton) toggleButton.onclick = () => { state.fullSession = !state.fullSession; renderTodayCard(t); };
  }

  const others = t.other_today || [];
  $("#today-extra").innerHTML = others.length
    ? `<article class="card list-card also-today">${others.map(session => sessionRow(session, t.today)).join("")}</article>` : "";
  bindSessionRows($("#today-extra"), others);
}

function renderDayCard(day, todayIso) {
  const card = $("#today-card");
  const past = day.date < todayIso;
  const activities = day.activities.length
    ? `<ul class="day-activities">${day.activities.map(activity => `<li>${icon("check")}<span>Garmin: ${esc(activity.title || activity.activity_type)}${activity.duration_minutes ? ` · ${Math.round(activity.duration_minutes)} min` : ""}${activity.distance_miles ? ` · ${activity.distance_miles} mi` : ""}</span></li>`).join("")}</ul>` : "";
  if (!day.sessions.length) {
    card.innerHTML = `<div class="empty-day"><h2>${esc(fmtLong(day.date))}</h2><p class="muted">${day.activities.length ? "No planned session." : "Rest day."}</p>${activities}</div>`;
    return;
  }
  card.innerHTML = day.sessions.map(session => {
    const body = session.kind === "run" ? runLinesHtml(session, session.minutes) : linesHtml(session.lines);
    const status = session.status === "completed" ? "Done" : past ? "Planned" : `${session.minutes} min`;
    return `<div class="session-body"><div class="session-title"><h2>${esc(cleanTitle(session.title))}</h2><span>${status}</span></div>${body}<button class="btn btn-ghost full" type="button" data-open-session="${esc(session.id)}">${session.status === "completed" ? "Review" : "Session details"}</button></div>`;
  }).join("") + (activities ? `<div class="session-body">${activities}</div>` : "");
  card.querySelectorAll("[data-open-session]").forEach(button => {
    const session = day.sessions.find(item => item.id === button.dataset.openSession);
    button.onclick = () => openSessionSheet(session);
  });
}

function renderStatStrip(t) {
  const row = t.recovery;
  const caption = $("#today-stats-caption");
  caption.hidden = !row || row.calendar_date === t.today;
  if (row && row.calendar_date !== t.today) caption.textContent = `Latest Garmin night · ${fmtShort(row.calendar_date)}`;
  const sleepWarn = num(row?.sleep_seconds) !== null && row.sleep_seconds < 7 * 3600;
  $("#today-stats").innerHTML = [
    [hm(row?.sleep_seconds, true), "Sleep", sleepWarn],
    [round(row?.hrv_ms), "HRV ms"],
    [round(row?.resting_hr), "Rest HR"],
    [round(row?.body_battery), "Battery"],
  ].map(([value, label, warn]) => `<div class="stat${warn ? " warn" : ""}"><b>${esc(value)}</b><span>${label}</span></div>`).join("");
}

/* ---------------------------------------------------------------- Recovery */

function hrvPill(status) {
  if (!status) return "";
  const key = String(status).toUpperCase();
  const tone = key === "BALANCED" ? "good" : key === "UNBALANCED" ? "warn" : key === "LOW" || key === "POOR" ? "bad" : "neutral";
  return `<span class="pill ${tone}">${esc(titleCase(status))}</span>`;
}

function renderRecovery() {
  const row = state.today.recovery;
  const dash = state.dashboard;
  const t = state.today;
  $("#recovery-kicker").textContent = row ? `${row.calendar_date === t.today ? "Last night" : "Latest night"} · ${fmtShort(row.calendar_date)}` : "No Garmin data yet";

  // Sleep
  const stages = row ? [["deep", "Deep", row.sleep_deep_seconds], ["light", "Light", row.sleep_light_seconds], ["rem", "REM", row.sleep_rem_seconds], ["awake", "Awake", row.sleep_awake_seconds]] : [];
  const stageTotal = stages.reduce((sum, [, , value]) => sum + (num(value) || 0), 0);
  const sleepWarn = num(row?.sleep_seconds) !== null && row.sleep_seconds < 7 * 3600;
  const score = num(row?.sleep_score) !== null ? `<small>Score ${Math.round(row.sleep_score)}${row.sleep_score_qualifier ? ` · ${esc(titleCase(row.sleep_score_qualifier))}` : ""}</small>` : "";
  $("#sleep-card").innerHTML = row && num(row.sleep_seconds) !== null
    ? `<div class="sleep-head"><h2>Sleep${score}</h2><span class="big${sleepWarn ? " warn" : ""}">${hm(row.sleep_seconds)}</span></div>
       ${stageTotal ? `<div class="stage-bar" role="img" aria-label="Sleep stages">${stages.map(([key, , value]) => num(value) ? `<i class="${key}" style="width:${(value / stageTotal) * 100}%"></i>` : "").join("")}</div>
       <div class="stage-legend">${stages.map(([, label, value]) => `<div><span>${label}</span><b>${hm(value)}</b></div>`).join("")}</div>` : ""}`
    : `<div class="sleep-head"><h2>Sleep</h2><span class="big">—</span></div><p class="muted">No sleep recorded for this night.</p>`;

  // Metric rows
  const steps = num(row?.steps); const goal = num(row?.step_goal);
  const hrvSub = [row?.hrv_baseline_low && row?.hrv_baseline_high ? `Baseline ${Math.round(row.hrv_baseline_low)}–${Math.round(row.hrv_baseline_high)}` : null, num(row?.hrv_weekly_avg) !== null ? `7-day ${Math.round(row.hrv_weekly_avg)}` : null].filter(Boolean).join(" · ");
  $("#recovery-metrics").innerHTML = `
    <div class="metric-row"><div><h3>HRV</h3><p>${esc(hrvSub || "Overnight average")}</p></div><div class="value">${hrvPill(row?.hrv_status)}${num(row?.hrv_ms) !== null ? `${Math.round(row.hrv_ms)} ms` : "—"}</div></div>
    <div class="metric-row"><div><h3>Resting HR</h3><p>${num(row?.resting_hr_7d_avg) !== null ? `7-day avg ${Math.round(row.resting_hr_7d_avg)}` : "Daily resting heart rate"}</p></div><div class="value">${num(row?.resting_hr) !== null ? `${Math.round(row.resting_hr)} bpm` : "—"}</div></div>
    <div class="metric-row"><div><h3>Body Battery</h3><p>Most recent reading</p></div><div class="value">${round(row?.body_battery)}</div></div>
    <div class="metric-row"><div><h3>Stress</h3><p>Daily average${num(row?.stress_max) !== null ? ` · peak ${Math.round(row.stress_max)}` : ""}</p></div><div class="value">${round(row?.stress_avg)}</div></div>
    <div class="metric-row steps"><div class="row"><h3>Steps</h3><div class="value">${round(steps)}${goal ? `<small>/ ${round(goal)}</small>` : ""}</div></div>${goal ? `<div class="meter"><i style="width:${Math.min(100, ((steps || 0) / goal) * 100)}%"></i></div>` : ""}</div>`;

  // Sync row
  const status = dash?.status || {};
  const lastSync = status.last_sync;
  const historyDays = dash?.history_days || 0;
  const synced = lastSync && ["success", "partial"].includes(lastSync.status) ? `Synced ${syncTime(lastSync.finished_at)}` : status.configured ? "Not synced yet" : "Garmin not connected";
  $("#sync-status").innerHTML = `${esc(synced)}<br>${historyDays} day${historyDays === 1 ? "" : "s"} of history stored`;
  const syncButton = $("#garmin-sync");
  syncButton.dataset.mode = historyDays < 30 ? "backfill" : "sync";
  syncButton.textContent = historyDays < 30 ? "Backfill 30 days" : "Sync now";
  syncButton.disabled = !status.configured;
  const message = $("#garmin-sync-message");
  const notice = lastSync && ["error", "partial"].includes(lastSync.status) ? lastSync.message : status.warning;
  if (!message.dataset.sticky) { message.hidden = !notice; message.textContent = notice || ""; message.classList.toggle("error", lastSync?.status === "error"); }

  // Readiness
  const rec = t.recommendation;
  $("#readiness-title").textContent = rec.title;
  $("#readiness-guidance").textContent = rec.guidance;
  $("#readiness-score").textContent = rec.score;
  $("#readiness-score").className = `score-ring ${rec.mode}`;
  $("#readiness-reasons").innerHTML = rec.reasons.map(reason => `<li>${esc(reason)}</li>`).join("");
  $("#recovery-checkin").textContent = t.checked_in_today ? "Update check-in" : "Log check-in";

  renderTrends();
  renderEndurance();
  renderCheckinLog();
  renderSyncHistory();
}

function renderTrends() {
  const records = [...(state.dashboard?.records || [])].reverse();
  const metrics = [
    ["Sleep", "sleep_seconds", value => `${(value / 3600).toFixed(1)} h`],
    ["HRV", "hrv_ms", value => `${Math.round(value)} ms`],
    ["Resting HR", "resting_hr", value => `${Math.round(value)} bpm`],
    ["Battery at wake", "body_battery_at_wake", value => `${Math.round(value)}`],
    ["Stress avg", "stress_avg", value => `${Math.round(value)}`],
    ["Steps", "steps", value => Math.round(value).toLocaleString()],
  ];
  $("#trend-grid").innerHTML = metrics.map(([label, key, format]) => {
    const points = records.filter(record => num(record[key]) !== null);
    if (!points.length) return `<article class="card trend"><div class="trend-head"><span>${label}</span><b>—</b></div><div class="bars"><span class="empty">No data yet</span></div></article>`;
    const values = points.map(record => Number(record[key]));
    const min = Math.min(...values); const max = Math.max(...values); const spread = Math.max(1, max - min);
    const average = values.reduce((sum, value) => sum + value, 0) / values.length;
    const bars = points.map((record, index) => `<i style="height:${values.length === 1 ? 60 : 18 + ((values[index] - min) / spread) * 82}%" title="${esc(fmtShort(record.calendar_date))} · ${esc(format(values[index]))}"></i>`).join("");
    return `<article class="card trend"><div class="trend-head"><span>${label}</span><b>${esc(format(average))}</b></div><div class="bars">${bars}</div></article>`;
  }).join("");
  $$("[data-garmin-days]").forEach(button => button.classList.toggle("active", Number(button.dataset.garminDays) === state.garminDays));
}

function renderEndurance() {
  const performance = state.dashboard?.performance || {};
  $("#endurance-tiles").innerHTML = [
    ["VO₂ max", performance.vo2_max ? performance.vo2_max.toFixed(1) : "—", performance.vo2_max_date ? `Garmin · ${fmtShort(performance.vo2_max_date)}` : "From compatible runs"],
    ["Run miles", `${Number(performance.run_miles_7d || 0).toFixed(1)}`, "Last 7 days"],
    ["Endurance", performance.endurance_score ?? "—", performance.run_miles_baseline ? `vs ${performance.run_miles_baseline.toFixed(1)} mi/wk baseline` : "Needs 4 weeks of runs"],
  ].map(([label, value, note]) => `<article class="card tile"><span>${label}</span><b>${esc(value)}</b><small>${esc(note)}</small></article>`).join("");
}

function renderCheckinLog() {
  const records = state.health?.records || [];
  $("#checkin-log").innerHTML = records.length
    ? records.map(record => `<article class="card log-card"><h3>${esc(fmtShort(record.checkin_date))}</h3>${record.notes ? `<p>${esc(record.notes)}</p>` : ""}<div class="chips"><span>Sleep ${record.sleep_hours}h</span><span>Energy ${record.energy}/10</span><span>Soreness ${record.soreness}/10</span><span>Stress ${record.stress}/10</span>${record.weight_lbs ? `<span>${record.weight_lbs} lb</span>` : ""}</div></article>`).join("")
    : `<article class="card empty"><b>No check-ins this week</b>Energy and soreness from a check-in feed the readiness score. Use + on Today.</article>`;
}

function renderSyncHistory() {
  const syncs = state.syncHistory?.syncs || [];
  $("#sync-history").innerHTML = syncs.length
    ? syncs.map(sync => `<div class="row-item"><span class="status-tag ${esc(sync.status)}">${esc(sync.status)}</span><div class="grow"><b>${esc(syncTime(sync.finished_at || sync.started_at))}</b><small>${sync.activities_imported} activities · ${sync.metrics_updated} days${sync.message && sync.message !== "Sync complete." ? ` · ${esc(sync.message)}` : ""}</small></div></div>`).join("")
    : `<div class="empty"><b>No syncs yet</b>Your first Garmin sync will appear here.</div>`;
}

async function runGarminSync(mode) {
  const button = $("#garmin-sync"); const message = $("#garmin-sync-message");
  button.disabled = true; button.textContent = mode === "backfill" ? "Backfilling…" : "Syncing…";
  message.hidden = false; message.classList.remove("error"); message.dataset.sticky = "1";
  message.textContent = mode === "backfill" ? "Fetching 30 days from Garmin. This takes about 3 minutes." : "Pulling your latest Garmin data…";
  try {
    const result = await api(mode === "backfill" ? "/api/garmin/backfill" : "/api/garmin/sync", { method: "POST" });
    message.textContent = result.status === "partial" ? result.message : `Done: ${result.metrics_updated} days and ${result.activities_imported} activities updated.`;
    await load();
  } catch (error) {
    message.textContent = error.message; message.classList.add("error");
    button.disabled = false; button.textContent = mode === "backfill" ? "Backfill 30 days" : "Sync now";
  } finally { delete message.dataset.sticky; }
}

/* ---------------------------------------------------------------- Train */

function sessionMeta(session) {
  if (session.kind === "run") { const miles = runMiles(session); return `Run${miles ? ` · ${miles} mi` : ""} · ${session.minutes} min`; }
  const count = (session.workout || []).length;
  return `${titleCase(session.kind)} · ${session.minutes} min${count ? ` · ${count} exercises` : ""}`;
}

function sessionRow(session, todayIso) {
  const classes = ["row-item", session.date === todayIso ? "is-today" : "", session.status === "completed" ? "is-done" : ""].join(" ");
  const end = session.status === "completed" ? "Done" : session.status === "cancelled" ? "Cancelled" : "";
  return `<button type="button" class="${classes}" data-session-id="${esc(session.id)}"><div class="date-chip"><span>${esc(fmtDow(session.date))}</span><b>${asDate(session.date).getDate()}</b></div><div class="grow"><b>${esc(cleanTitle(session.title))}</b><small>${esc(sessionMeta(session))}</small></div>${session.locked ? `<svg class="lock-icon" aria-label="Locked"><use href="#i-lock"/></svg>` : ""}${end ? `<span class="end">${end}</span>` : ""}${icon("chevron")}</button>`;
}

function bindSessionRows(container, sessions) {
  container.querySelectorAll("[data-session-id]").forEach(button => {
    const session = sessions.find(item => item.id === button.dataset.sessionId);
    if (session) button.onclick = () => openSessionSheet(session);
  });
}

function weekStart(iso) { const day = asDate(iso); day.setDate(day.getDate() - ((day.getDay() + 6) % 7)); return day.toISOString().slice(0, 10); }

function renderTrain() {
  const t = state.today;
  $("#train-kicker").textContent = planKicker(t.plan);
  const plan = t.plan;
  $("#block-card").innerHTML = plan
    ? `<h2>${esc(plan.name)}</h2><p>${esc(plan.phase_message || "")}</p>${plan.in_block ? `<div class="meter"><i style="width:${Math.min(100, (plan.week / plan.weeks) * 100)}%"></i></div><div class="meter-label"><span>Week ${plan.week}</span><span>${plan.weeks} weeks</span></div>` : ""}<div class="block-actions"><button class="btn btn-ghost" type="button" id="adjust-block">Adjust block</button><button class="btn btn-outline" type="button" id="build-block">New block</button></div>`
    : `<h2>No active block</h2><p>Build a block to fill your week with planned sessions.</p><div class="block-actions"><button class="btn btn-primary" type="button" id="build-block">Build a block</button></div>`;
  $("#build-block").onclick = () => openPlanBuilder();
  const adjust = $("#adjust-block"); if (adjust) adjust.onclick = () => openPlanBuilder(activePlan);

  // Upcoming, grouped by week
  const sessions = state.overview?.sessions || [];
  const thisWeek = weekStart(t.today);
  const groups = new Map();
  sessions.forEach(session => { const key = weekStart(session.date); if (!groups.has(key)) groups.set(key, []); groups.get(key).push(session); });
  $("#train-plan").innerHTML = sessions.length
    ? [...groups.entries()].map(([key, items]) => `<p class="week-label">${key === thisWeek ? "This week" : `Week of ${esc(fmtShort(key).replace(/^\w+, /, ""))}`}</p><article class="card list-card">${items.map(session => sessionRow(session, t.today)).join("")}</article>`).join("")
    : `<article class="card empty"><b>Nothing planned in the next two weeks</b>Build or adjust a block to schedule sessions.</article>`;
  bindSessionRows($("#train-plan"), sessions);

  // History: Health Hub logs + Garmin activities
  const logged = (state.history?.sessions || []).map(session => ({ date: session.date, kind: "session", session }));
  const garmin = (state.activities?.activities || []).map(activity => ({ date: activity.activity_date, kind: "activity", activity }));
  const items = [...logged, ...garmin].sort((a, b) => b.date.localeCompare(a.date)).slice(0, 60);
  $("#train-history").innerHTML = items.length
    ? `<article class="card list-card">${items.map(item => {
        if (item.kind === "session") {
          const s = item.session;
          const detail = s.metrics?.distance_miles ? `${s.metrics.distance_miles} mi · ${formatPace(s.metrics.pace_seconds_per_mile)}` : `${s.sets_logged} sets logged`;
          return `<button type="button" class="row-item" data-review="${esc(s.id)}"><div class="date-chip"><span>${esc(fmtDow(s.date))}</span><b>${asDate(s.date).getDate()}</b></div><div class="grow"><b>${esc(cleanTitle(s.title))}</b><small>Logged here · ${esc(detail)}</small></div>${icon("chevron")}</button>`;
        }
        const a = item.activity;
        const bits = [a.activity_type, a.distance_miles ? `${a.distance_miles} mi` : null, a.duration_minutes ? `${Math.round(a.duration_minutes)} min` : null, a.average_hr ? `${Math.round(a.average_hr)} bpm avg` : null].filter(Boolean).join(" · ");
        return `<div class="row-item"><div class="date-chip"><span>${esc(fmtDow(a.activity_date))}</span><b>${asDate(a.activity_date).getDate()}</b></div><div class="grow"><b>${esc(a.title)}</b><small>Garmin · ${esc(bits)}</small></div></div>`;
      }).join("")}</article>`
    : `<article class="card empty"><b>No history yet</b>Finished workouts and Garmin activities show up here.</article>`;
  $$("#train-history [data-review]").forEach(button => button.onclick = () => openSession(button.dataset.review));

  $$("[data-train-tab]").forEach(button => button.classList.toggle("active", button.dataset.trainTab === state.trainTab));
  $("#train-plan").hidden = state.trainTab !== "plan";
  $("#train-history").hidden = state.trainTab !== "history";
}

function openSessionSheet(session) {
  const sheet = $("#session-sheet");
  const lines = session.kind === "run" ? runLinesHtml(session, session.minutes) : linesHtml(session.lines || (session.workout || []).map(line => {
    const [name, prescription = ""] = line.split("—");
    const match = prescription.match(/(\d+)\s*[×x]\s*([\d–-]+)/);
    return { name: name.trim(), sets: match ? Number(match[1]) : null, reps: match ? match[2] : null, note: match ? null : prescription.trim() };
  }));
  const planned = session.status === "planned";
  $("#session-sheet-body").innerHTML = `<button class="sheet-close" type="button" aria-label="Close">${icon("close")}</button>
    <p class="kicker">${esc(fmtLong(session.date))}</p><h2>${esc(cleanTitle(session.title))}</h2>
    <p class="muted">${esc(sessionMeta(session))}${session.status !== "planned" ? ` · ${esc(titleCase(session.status))}` : ""}</p>
    ${lines}
    ${planned || session.status === "completed" ? `<button class="btn btn-primary full" type="button" id="sheet-start">${session.status === "completed" ? "Review workout" : session.kind === "run" ? "Start run" : "Start workout"}</button>` : ""}
    <div class="sheet-actions"><button class="btn btn-ghost" type="button" id="sheet-edit">Edit</button><button class="btn btn-ghost" type="button" id="sheet-lock">${session.locked ? "Unlock" : "Lock"}</button></div>
    <p class="faint">Locked sessions are never moved when you adjust the block.</p>`;
  sheet.querySelector(".sheet-close").onclick = () => sheet.close();
  const start = $("#sheet-start"); if (start) start.onclick = () => { sheet.close(); openSession(session.id); };
  $("#sheet-edit").onclick = () => { sheet.close(); openSessionEditor(session); };
  $("#sheet-lock").onclick = async () => { await api(`/api/sessions/${encodeURIComponent(session.id)}/lock`, { method: "POST" }); sheet.close(); await load(); };
  openDialog(sheet);
}

/* ---------------------------------------------------------------- Workout */

function formatTimer() { return `${String(Math.floor(timerSeconds / 60)).padStart(2, "0")}:${String(timerSeconds % 60).padStart(2, "0")}`; }
function stopTimer() { clearInterval(timerId); timerId = null; }
function updateTimer() { const output = $("#rest-clock"); if (output) output.textContent = formatTimer(); }
function startTimer() { stopTimer(); timerSeconds = 90; updateTimer(); timerId = setInterval(() => { timerSeconds -= 1; updateTimer(); if (timerSeconds <= 0) stopTimer(); }, 1000); }

function setRow(exercise, setNumber, logged) {
  const weight = logged?.weight ?? exercise.suggested_weight ?? "";
  const reps = logged?.reps ?? exercise.suggested_reps ?? "";
  return `<div class="set-row" data-exercise-index="${exercise.index}" data-set-number="${setNumber}"><span>${setNumber}</span><input aria-label="Weight, set ${setNumber}" type="number" inputmode="decimal" value="${weight}" min="0" step="2.5" placeholder="lb"><input aria-label="Reps, set ${setNumber}" type="number" inputmode="numeric" value="${reps}" min="0" placeholder="${esc(exercise.target_reps)}"><input aria-label="RPE, set ${setNumber}" type="number" inputmode="decimal" value="${logged?.rpe ?? ""}" min="1" max="10" step=".5" placeholder="RPE"><button class="add-weight" type="button" aria-label="Add 5 lb">+5</button><button class="set-check ${logged ? "logged" : ""}" type="button">${logged ? "✓" : "Log"}</button></div>`;
}

function updateWorkoutProgress() {
  const rows = $$("#workout .set-row");
  const logged = rows.filter(row => row.querySelector(".set-check.logged")).length;
  const bar = $("#workout .workout-progress i");
  if (bar) bar.style.width = `${rows.length ? (logged / rows.length) * 100 : 0}%`;
}

async function openSession(sessionId, options = {}) {
  const session = await api(`/api/sessions/${encodeURIComponent(sessionId)}`);
  const back = state.returnView;
  selectView("workout");
  const target = $("#workout");
  const done = session.status === "completed";
  const minutes = options.minutes || session.minutes;
  const header = `<header class="workout-head"><button class="back-link" type="button" id="workout-back">${icon("back")}${back === "today" ? "Today" : "Train"}</button><h1>${esc(cleanTitle(session.title))}</h1><p>${esc(fmtLong(session.date))} · ${minutes} min${options.cap ? ` · short version, ${options.cap} sets each` : ""}${done ? " · completed" : ""}</p></header>`;

  if (session.kind === "run") {
    const miles = runMiles(session);
    target.innerHTML = `${header}<article class="card"><p class="kicker">Target</p><h2>${miles ? `${miles.toFixed(1)} mi easy` : "Run by easy effort"}</h2><p class="muted">${esc(session.description || "Keep the effort conversational.")}</p></article>${done
      ? `<article class="card empty"><b>This run is logged</b>Find it under Train → History.</article>`
      : `<form id="run-log-form" class="card stack"><div class="run-fields"><label>Distance <span>miles</span><input id="run-distance" type="number" inputmode="decimal" min=".1" step=".01" value="${miles || ""}" required></label><label>Time <span>minutes</span><input id="run-duration" type="number" inputmode="decimal" min="1" step=".1" required></label><label>Average HR <span>optional</span><input id="run-average-hr" type="number" inputmode="numeric" min="30" max="260"></label><label>Max HR <span>optional</span><input id="run-max-hr" type="number" inputmode="numeric" min="30" max="260"></label></div><label>Effort <span>1 easy — 10 maximal</span><input id="run-effort" type="number" min="1" max="10" value="5" required></label><div class="pace"><span>PACE</span><b id="run-pace">—</b></div><label>Notes <textarea id="run-notes" rows="3" placeholder="How did it feel?"></textarea></label><button class="btn btn-primary full" type="submit">Log run &amp; finish</button></form>`}`;
    $("#workout-back").onclick = () => selectView(back);
    const form = $("#run-log-form");
    if (!form) return;
    const updatePace = () => { $("#run-pace").textContent = formatPace(Number($("#run-duration").value) * 60 / Number($("#run-distance").value)); };
    target.querySelectorAll("#run-distance, #run-duration").forEach(input => input.oninput = updatePace);
    updatePace();
    form.onsubmit = async event => {
      event.preventDefault();
      const submit = form.querySelector('button[type="submit"]'); submit.disabled = true; submit.textContent = "Saving…";
      try {
        await api(`/api/sessions/${encodeURIComponent(session.id)}/log-run`, { method: "POST", body: JSON.stringify({ distance_miles: Number($("#run-distance").value), duration_minutes: Number($("#run-duration").value), average_hr: $("#run-average-hr").value ? Number($("#run-average-hr").value) : null, max_hr: $("#run-max-hr").value ? Number($("#run-max-hr").value) : null, effort: Number($("#run-effort").value), notes: $("#run-notes").value.trim() }) });
        await load(); selectView(back);
      } catch (error) { alert(error.message); } finally { submit.disabled = false; submit.textContent = "Log run & finish"; }
    };
    return;
  }

  const exercises = session.exercises.map(exercise => {
    const loggedCount = Math.max(0, ...(exercise.sets || []).map(set => set.set_number));
    const count = Math.max(options.cap ? Math.min(exercise.target_sets, options.cap) : exercise.target_sets, loggedCount);
    return `<article class="card exercise"><div class="exercise-head"><div><h3>${esc(exercise.name)}</h3><p>${count} × ${esc(exercise.target_reps)} · ${esc(exercise.starting_guide)}</p></div><span class="last">${exercise.suggested_weight ? `Last ${exercise.suggested_weight} × ${exercise.suggested_reps}` : "No baseline"}</span></div><div class="set-grid"><div class="set-labels"><span>SET</span><span>LB</span><span>REPS</span><span>RPE</span><span></span><span></span></div>${Array.from({ length: count }, (_, index) => setRow(exercise, index + 1, exercise.sets?.find(log => log.set_number === index + 1))).join("")}</div></article>`;
  }).join("");
  target.innerHTML = `${header}<div class="workout-progress"><i></i></div>${exercises}<div class="rest-bar"><button class="timer" type="button" id="timer-toggle"><b id="rest-clock">01:30</b><span>Rest</span></button>${done ? "" : '<button class="btn btn-primary" type="button" id="finish-session">Finish workout</button>'}</div>`;
  $("#workout-back").onclick = () => { stopTimer(); selectView(back); };
  $("#timer-toggle").onclick = () => (timerId ? stopTimer() : startTimer());
  target.querySelectorAll(".set-row").forEach(row => {
    const inputs = row.querySelectorAll("input");
    row.querySelector(".add-weight").onclick = () => { inputs[0].value = Number(inputs[0].value || 0) + 5; };
    row.querySelector(".set-check").onclick = async () => {
      await api(`/api/sessions/${encodeURIComponent(session.id)}/sets`, { method: "PUT", body: JSON.stringify({ exercise_index: Number(row.dataset.exerciseIndex), set_number: Number(row.dataset.setNumber), weight: Number(inputs[0].value), reps: Number(inputs[1].value), rpe: inputs[2].value ? Number(inputs[2].value) : null, note: "" }) });
      const check = row.querySelector(".set-check"); check.classList.add("logged"); check.textContent = "✓";
      updateWorkoutProgress(); startTimer();
    };
  });
  updateWorkoutProgress();
  const finish = $("#finish-session");
  if (finish) finish.onclick = async () => {
    finish.disabled = true;
    await api(`/api/sessions/${encodeURIComponent(session.id)}/complete`, { method: "POST", body: JSON.stringify({ minutes, notes: options.cap ? `Short version: ${options.cap} sets per exercise` : "Logged in Health Hub", metrics: options.cap ? { capped_sets: options.cap } : {} }) });
    stopTimer(); state.fullSession = false; await load(); selectView(back);
  };
}

/* ---------------------------------------------------------------- Progress + settings */

function renderProgress() {
  const exercises = state.progress?.exercises || [];
  const completed = (state.insights?.sessions || []).filter(row => row.status === "completed").reduce((sum, row) => sum + row.count, 0);
  $("#progress-stats").className = "card stat-strip static three";
  const volume = Number(state.insights?.volume || 0);
  $("#progress-stats").innerHTML = [[completed, "Sessions"], [volume >= 10000 ? `${Math.round(volume / 1000).toLocaleString()}k` : Math.round(volume).toLocaleString(), "Volume lb"], [exercises.length, "Lifts tracked"]]
    .map(([value, label]) => `<div class="stat"><b>${esc(value)}</b><span>${label}</span></div>`).join("");
  $("#progress-list").innerHTML = exercises.length
    ? exercises.map((exercise, index) => `<button type="button" class="row-item" data-exercise="${index}"><div class="grow"><b>${esc(exercise.name)}</b><small>Best ${exercise.best_set.weight} lb × ${exercise.best_set.reps} · ${exercise.history.length} sets</small></div><span class="end">${exercise.best_set.e1rm.toFixed(0)} lb</span>${icon("chevron")}</button>`).join("")
    : `<div class="empty"><b>Progress starts with your first logged set</b>Log working sets during a workout and each lift's estimated 1RM is tracked here.</div>`;
  $$("#progress-list [data-exercise]").forEach(button => button.onclick = () => openExerciseSheet(exercises[Number(button.dataset.exercise)]));
}

function openExerciseSheet(exercise) {
  const sheet = $("#exercise-sheet");
  const history = exercise.history.slice(-16);
  const max = Math.max(...history.map(point => point.e1rm), 1);
  $("#exercise-sheet-body").innerHTML = `<button class="sheet-close" type="button" aria-label="Close">${icon("close")}</button><p class="kicker">Estimated 1RM</p><h2>${esc(exercise.name)}</h2><p class="muted">Best ${exercise.best_set.e1rm.toFixed(1)} lb from ${exercise.best_set.weight} lb × ${exercise.best_set.reps}</p><div class="detail-chart">${history.map(point => `<i title="${esc(point.date)}: ${point.e1rm.toFixed(1)} lb" style="height:${Math.max(8, (point.e1rm / max) * 100)}%"></i>`).join("")}</div><div class="table"><div><span>Date</span><span>Set</span><span>e1RM</span><span>RPE</span></div>${history.slice().reverse().map(point => `<div><span>${esc(fmtShort(point.date))}</span><span>${point.weight} × ${point.reps}</span><span>${point.e1rm.toFixed(1)}</span><span>${point.rpe ?? "—"}</span></div>`).join("")}</div>`;
  sheet.querySelector(".sheet-close").onclick = () => sheet.close();
  openDialog(sheet);
}

function renderSettings() {
  const status = state.dashboard?.status || {};
  $("#garmin-settings-status").textContent = status.configured
    ? `Connected locally${status.expires_on ? ` · key expires ${status.expires_on}` : ""}${status.warning ? ` · ${status.warning}` : ""}`
    : "Not connected yet. No Garmin key is stored in this app or in GitHub.";
}

/* ---------------------------------------------------------------- Block builder */

const SPLIT_HELP = {
  "body-part-balanced": "Separate chest/triceps/shoulders, back/biceps, and lower-body sessions. With five days, chest/tri/shoulders gets a second volume day.",
  "push-pull-legs": "A familiar push/pull/legs rotation; useful if you prefer simple repeated patterns.",
  "balanced-upper-lower": "Alternates upper and lower emphasis with a balanced amount of pressing and pulling.",
  "full-body": "Fewer, more comprehensive sessions; useful if you want flexibility around a busy week.",
};

function openPlanBuilder(plan = null) {
  activePlan = plan || activePlan;
  const editing = Boolean(plan);
  const config = plan?.config;
  $("#plan-dialog-heading").textContent = editing ? "Adjust your active block" : "Build your next block";
  $("#plan-dialog-copy").textContent = editing ? "You'll review the changes first. Only future unlocked sessions change; completed, locked, and logged sessions stay." : "Start with an honest baseline. You will review every session before it becomes active.";
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
  $("#plan-form").dataset.mode = editing ? "revise" : "new";
  $("#plan-form button[type='submit']").textContent = editing ? "Review future changes" : "Create review draft";
  $("#plan-form-status").hidden = true;
  $("#split-help").textContent = SPLIT_HELP[$("#plan-split").value];
  openDialog($("#plan-dialog"));
}

async function submitPlanDraft(event) {
  event.preventDefault();
  const revise = $("#plan-form").dataset.mode === "revise" && activePlan;
  const payload = {
    name: $("#plan-name").value.trim() || "My next training block",
    goals: $("#plan-goals").value.split("\n").map(goal => goal.trim()).filter(Boolean),
    weeks: Number($("#plan-weeks").value), lifting_days: Number($("#plan-lifting-days").value),
    run_days: Number($("#plan-run-days").value), starting_miles: Number($("#plan-miles").value),
    available_minutes: Number($("#plan-minutes").value), avoid: $("#plan-avoid").value.trim(),
    split: $("#plan-split").value, include_mobility: $("#plan-mobility").checked, equipment: "Full gym",
  };
  const submit = event.submitter; const label = submit.textContent; submit.disabled = true; submit.textContent = "Building draft…";
  try {
    const plan = await api(revise ? `/api/plans/${encodeURIComponent(activePlan.id)}/revision-preview` : "/api/plans/draft", { method: "POST", body: JSON.stringify(payload) });
    $("#plan-dialog").close(); renderPlanReview(plan); openDialog($("#plan-review-dialog"));
  } catch (error) {
    const status = $("#plan-form-status"); status.textContent = error.message || "The plan could not be generated."; status.hidden = false;
  } finally { submit.disabled = false; submit.textContent = label; }
}

function renderPlanReview(plan) {
  const revision = Boolean(plan.revision_of);
  const weekOf = session => Math.floor((asDate(session.date) - asDate(plan.start_date)) / 86400000 / 7) + 1;
  $("#plan-review").innerHTML = `<button class="sheet-close" type="button" aria-label="Close">${icon("close")}</button>
    <p class="kicker">${revision ? "Preview · nothing changed yet" : "Draft · nothing scheduled yet"}</p><h2>${esc(plan.name)}</h2>
    <p class="muted">Starts ${esc(fmtShort(plan.start_date))} · ${plan.config.weeks} weeks · ${plan.sessions.length} sessions · ${esc(plan.config.split.replace(/-/g, " "))}</p>
    <div class="chips">${plan.goals.map(goal => `<span>${esc(goal)}</span>`).join("")}</div>
    <div class="phase-list">${plan.phases.map(phase => `<article><span>WEEK ${phase.week} · ${esc(phase.name.toUpperCase())}</span><p>${esc(phase.message)}</p><p class="faint">${plan.sessions.filter(session => weekOf(session) === phase.week).map(session => esc(cleanTitle(session.title))).join(" · ")}</p></article>`).join("")}</div>
    <p class="note" id="review-note">${revision ? "Applying this keeps completed, locked, and logged sessions, and rebuilds only the remaining flexible sessions." : "You can still edit individual workouts after activating."}</p>
    <div class="sheet-actions"><button class="btn btn-ghost" type="button" id="edit-draft">Edit inputs</button><button class="btn btn-primary" type="button" id="activate-draft">${revision ? "Apply changes" : "Activate block"}</button></div>`;
  const dialog = $("#plan-review-dialog");
  dialog.querySelector(".sheet-close").onclick = () => dialog.close();
  $("#edit-draft").onclick = () => { dialog.close(); openDialog($("#plan-dialog")); };
  $("#activate-draft").onclick = async () => {
    const button = $("#activate-draft"); button.disabled = true; button.textContent = "Applying…";
    try {
      await api(revision ? `/api/plans/${encodeURIComponent(plan.revision_of)}/revise` : `/api/plans/${encodeURIComponent(plan.id)}/activate`, revision ? { method: "POST", body: JSON.stringify(plan.config) } : { method: "POST" });
      dialog.close(); await load(); selectView("train");
    } catch (error) {
      $("#review-note").textContent = error.message || "The plan could not be applied. Nothing was changed."; $("#review-note").classList.add("error");
      button.disabled = false; button.textContent = revision ? "Apply changes" : "Activate block";
    }
  };
}

/* ---------------------------------------------------------------- Session editor */

function openSessionEditor(session) {
  editingSessionId = session.id;
  $("#editor-heading").textContent = cleanTitle(session.title);
  $("#editor-title").value = session.title;
  $("#editor-date").value = session.date;
  $("#editor-minutes").value = session.minutes;
  $("#editor-description").value = session.description || "";
  $("#editor-workout").value = (session.workout || []).join("\n");
  $("#editor-cancel-session").textContent = session.status === "cancelled" ? "Restore workout" : "Cancel workout";
  openDialog($("#session-editor-dialog"));
}

async function saveSessionEditor(event) {
  event.preventDefault();
  if (!editingSessionId) return;
  const workout = $("#editor-workout").value.split("\n").map(line => line.trim()).filter(Boolean);
  const payload = { session_date: $("#editor-date").value, title: $("#editor-title").value.trim(), minutes: Number($("#editor-minutes").value), description: $("#editor-description").value.trim(), workout, status: "planned" };
  const submit = event.submitter; submit.disabled = true; submit.textContent = "Saving…";
  try { await api(`/api/sessions/${encodeURIComponent(editingSessionId)}`, { method: "PATCH", body: JSON.stringify(payload) }); $("#session-editor-dialog").close(); await load(); }
  catch (error) { alert(error.message); }
  finally { submit.disabled = false; submit.textContent = "Save changes"; }
}

/* ---------------------------------------------------------------- wiring */

$$("[data-view]").forEach(button => button.addEventListener("click", () => selectView(button.dataset.view)));
$("#today-stats").onclick = () => selectView("recovery");
$("#today-checkin").onclick = () => openDialog($("#health-dialog"));
$("#recovery-checkin").onclick = () => openDialog($("#health-dialog"));
$("#garmin-sync").onclick = () => runGarminSync($("#garmin-sync").dataset.mode);
$$("[data-garmin-days]").forEach(button => button.onclick = async () => {
  state.garminDays = Number(button.dataset.garminDays);
  state.dashboard = await api(`/api/garmin/dashboard?days=${state.garminDays}`);
  renderTrends();
});
$$("[data-train-tab]").forEach(button => button.onclick = () => { state.trainTab = button.dataset.trainTab; renderTrain(); });

// Every sheet closes from its × button or a tap on the backdrop.
$$("dialog.sheet").forEach(dialog => {
  dialog.addEventListener("click", event => { if (event.target === dialog) dialog.close(); });
  dialog.querySelectorAll("form > .sheet-close").forEach(button => button.onclick = () => dialog.close());
});

$("#plan-form").addEventListener("submit", submitPlanDraft);
$("#plan-split").onchange = event => { $("#split-help").textContent = SPLIT_HELP[event.target.value]; };
$("#session-editor-form").addEventListener("submit", saveSessionEditor);
$("#editor-add-exercise").onclick = () => { const area = $("#editor-workout"); area.value += `${area.value ? "\n" : ""}New exercise — 3 × 8–12`; area.focus(); };
$("#editor-cancel-session").onclick = async () => {
  if (!editingSessionId) return;
  const restore = $("#editor-cancel-session").textContent.includes("Restore");
  await api(`/api/sessions/${encodeURIComponent(editingSessionId)}`, { method: "PATCH", body: JSON.stringify({ status: restore ? "planned" : "cancelled" }) });
  $("#session-editor-dialog").close(); await load();
};
$("#health-form").onsubmit = async event => {
  event.preventDefault();
  const submit = event.submitter; submit.disabled = true; submit.textContent = "Saving…";
  try {
    await api("/api/health/checkin", { method: "POST", body: JSON.stringify({
      sleep_hours: Number($("#health-sleep-hours").value), sleep_quality: Number($("#health-sleep-quality").value),
      energy: Number($("#health-energy").value), soreness: Number($("#health-soreness").value), stress: Number($("#health-stress").value),
      weight_lbs: $("#health-weight-lbs").value ? Number($("#health-weight-lbs").value) : null,
      protein_goal_met: $("#health-protein").checked, notes: $("#health-notes").value.trim(),
    }) });
    $("#health-dialog").close(); await load();
  } catch (error) { alert(error.message); }
  finally { submit.disabled = false; submit.textContent = "Save check-in"; }
};
document.addEventListener("keydown", event => {
  if (event.code === "Space" && $("#view-workout").classList.contains("active") && !["INPUT", "TEXTAREA", "BUTTON"].includes(document.activeElement?.tagName)) {
    event.preventDefault(); timerId ? stopTimer() : startTimer();
  }
});
window.addEventListener("beforeinstallprompt", event => { event.preventDefault(); installPrompt = event; $("#install-app").hidden = false; });
$("#install-app").onclick = async () => { if (!installPrompt) return; installPrompt.prompt(); await installPrompt.userChoice; installPrompt = null; $("#install-app").hidden = true; };
// Offline support is optional; some browsers refuse service workers on plain http.
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/static/service-worker.js").catch(() => {});

window.__healthHubBootState = "ready";
load().catch(error => {
  $("#today-title").textContent = "Can't reach Health Hub";
  $("#today-card").innerHTML = `<div class="empty-day"><p class="muted">${esc(error.message || "The server did not respond.")} Check that the app is running on this computer.</p></div>`;
});
