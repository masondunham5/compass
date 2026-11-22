/* Health Hub: health features (alerts, habits, load, sleep coach, body, pain, records,
   goals, insights, long-term trends, reminders). Loaded after app.js and uses its helpers. */

const healthState = { hub: null, habits: null, goals: null, insights: null, trends: null, records: null, trendDays: 180, painArea: null };

async function renderHealthHub() {
  const [hub, habits, goals, insights, trends] = await Promise.all([
    api("/api/health/hub"), api("/api/health/habits"), api("/api/health/goals"), api("/api/health/insights"), api(`/api/health/trends?days=${healthState.trendDays}`),
  ]);
  Object.assign(healthState, { hub, habits: habits.habits, goals: goals.goals, insights, trends });
  renderAlerts(); renderHabits(); renderLoad(); renderSleepCoach(); renderBody(); renderPain();
  renderGoals(); renderInsights(); renderLongTrends(); renderCheckinExtras();
  scheduleReminders();
}
window.renderHealthHub = renderHealthHub;

/* ---------------- Today: alerts + habits */

function renderAlerts() {
  $("#today-alerts").innerHTML = healthState.hub.alerts.map(alert =>
    `<div class="alert ${esc(alert.level)}">${icon(alert.level === "warn" ? "alert" : "recovery")}<div><b>${esc(alert.title)}</b>${alert.message ? `<p>${esc(alert.message)}</p>` : ""}</div></div>`).join("");
}

function renderHabits() {
  const rows = healthState.habits.map(habit => {
    const dots = habit.history.slice(-14).map(done => `<i class="${done ? "on" : ""}"></i>`).join("");
    const tag = habit.automatic ? "button" : "button";
    return `<${tag} type="button" class="row-item habit${habit.done_today ? " done" : ""}" data-habit="${habit.id}" data-auto="${habit.automatic ? 1 : 0}"><span class="check">✓</span><div class="grow"><b>${esc(habit.name)}</b><small>${habit.automatic ? "Tracked automatically" : "Tap to check off today"}${habit.streak ? ` · ${habit.streak}-day streak` : ""}</small><div class="dots" aria-label="Last 14 days">${dots}</div></div></${tag}>`;
  }).join("");
  $("#habit-list").innerHTML = rows + `<button type="button" class="row-item habit-add" id="add-habit">+ Add a habit</button>`;
  $$("#habit-list [data-habit]").forEach(row => row.onclick = async () => {
    if (row.dataset.auto === "1") { row.querySelector("small").textContent = "Comes from your Garmin data and check-ins — nothing to tap."; return; }
    healthState.habits = (await api(`/api/health/habits/${row.dataset.habit}/toggle`, { method: "POST" })).habits;
    renderHabits();
  });
  $("#add-habit").onclick = () => { $("#habit-name").value = ""; openDialog($("#habit-dialog")); };
}

/* ---------------- Recovery: load, sleep coach, body, pain */

function renderLoad() {
  const load = healthState.hub.load;
  const illness = healthState.hub.illness;
  const max = Math.max(1, ...load.daily.map(day => day.load));
  const bars = load.daily.map((day, index) => `<i class="${index >= load.daily.length - 7 ? "recent" : ""}" style="height:${Math.max(2, (day.load / max) * 100)}%" title="${esc(fmtShort(day.date))}: load ${day.load}"></i>`).join("");
  const zones = load.zone_minutes.map((minutes, index) => `<div>Z${index + 1}<b>${minutes}</b>min</div>`).join("");
  const flagged = illness.flagged_days.length ? `<p class="note">Illness watch: ${illness.flagged_days.map(day => `${esc(fmtShort(day.date))} (${esc(day.signs.join(", "))})`).join("; ")}. ${illness.warning ? "" : "One more day like this triggers a warning."}</p>` : `<p class="note">Illness watch: resting HR, HRV and breathing rate are within your normal range.</p>`;
  $("#load-card").innerHTML = `<div class="gauge"><b>${load.ratio ?? "—"}×</b><span class="status ${esc(load.status)}">${esc(load.status)}</span></div>
    <p class="muted">7-day load ${load.acute} vs ${load.chronic ?? "—"} weekly norm (4 weeks). ${esc(load.message)}</p>
    <div class="load-bars" role="img" aria-label="Daily training load, last 6 weeks">${bars}</div>
    <p class="faint">Garmin training load per day · last 6 weeks (bright = this week)</p>
    <div class="zone-row" aria-label="Heart-rate zone minutes this week">${zones}</div>
    <p class="faint" style="margin-top:8px">Run miles: ${load.run_miles.acute} this week vs ${load.run_miles.chronic} average${load.run_miles.ratio ? ` (${load.run_miles.ratio}×)` : ""}</p>${flagged}`;
}

function renderSleepCoach() {
  const sleep = healthState.hub.sleep;
  const tonight = sleep.tonight;
  $("#sleep-coach").innerHTML = `<div class="review-stats"><div><span>Sleep debt · 7 nights</span><b>${(sleep.debt_minutes / 60).toFixed(1)} h</b><small>vs Garmin's sleep need</small></div>
      <div><span>Bedtime</span><b>${esc(sleep.average_bedtime || "—")}</b><small>${sleep.consistency ? `${esc(sleep.consistency)} (±${sleep.bedtime_spread_minutes} min)` : "average"}</small></div>
      <div><span>Tonight</span><b>${tonight?.window_start ? esc(tonight.window_start) : "—"}</b><small>${tonight?.need_minutes ? `need ${hm(tonight.need_minutes * 60)}` : "bed by"}</small></div></div>
    ${sleep.tips.length ? `<ul class="coach-tips">${sleep.tips.map(tip => `<li>${esc(tip)}</li>`).join("")}</ul>` : ""}`;
}

function renderBody() {
  const body = healthState.hub.body;
  const change = body.weight_change_4wk;
  $("#body-card").innerHTML = `<div class="review-stats"><div><span>Weight · 7-day avg</span><b>${body.weight_7day_avg ?? body.latest_weight ?? "—"}${body.weight_7day_avg || body.latest_weight ? " lb" : ""}</b><small>${change != null ? `${change > 0 ? "+" : ""}${change} lb vs 4 weeks ago` : "log a few weigh-ins"}</small></div>
      <div><span>Waist</span><b>${body.latest_waist ?? "—"}${body.latest_waist ? " in" : ""}</b><small>${body.latest_body_fat ? `${body.latest_body_fat}% body fat` : "optional"}</small></div>
      <div><span>Protein · 7-day avg</span><b>${body.protein_avg ?? "—"}${body.protein_avg ? " g" : ""}</b><small>${body.protein_target ? `target ${body.protein_target} g` : "set bodyweight for a target"}</small></div></div>
    <div class="block-actions"><button class="btn btn-ghost" type="button" id="add-measurement">Add measurements</button><button class="btn btn-ghost" type="button" id="body-checkin">Log protein</button></div>`;
  $("#add-measurement").onclick = () => { $("#measure-form").reset(); $("#measure-date").value = state.today.today; openDialog($("#measure-dialog")); };
  $("#body-checkin").onclick = () => openDialog($("#health-dialog"));
}

function renderPain() {
  const pain = healthState.hub.pain;
  $("#pain-list").innerHTML = pain.length ? pain.map(entry => `<div class="row-item"><div class="grow"><b>${esc(titleCase(entry.area))} · ${entry.severity}/10</b><small>Since ${esc(fmtShort(entry.logged_on))}${entry.notes ? ` · ${esc(entry.notes)}` : ""}</small></div><div class="record-actions"><button type="button" data-pain-edit="${esc(entry.area)}">Update</button><button type="button" data-pain-resolve="${entry.id}">Resolved</button></div></div>`).join("")
    : `<div class="empty"><b>No pain logged</b>Log any niggle and workouts will flag exercises that load it.</div>`;
  $$("[data-pain-resolve]").forEach(button => button.onclick = async () => { await api(`/api/health/pain/${button.dataset.painResolve}/resolve`, { method: "POST" }); await renderHealthHub(); });
  $$("[data-pain-edit]").forEach(button => button.onclick = () => openPainDialog(button.dataset.painEdit));
}

function openPainDialog(area = null) {
  healthState.painArea = area;
  const existing = healthState.hub.pain.find(entry => entry.area === area);
  $("#pain-areas").innerHTML = healthState.hub.pain_areas.map(name => `<button type="button" class="chip${name === area ? " active" : ""}" data-area="${esc(name)}">${esc(titleCase(name))}</button>`).join("");
  $$("#pain-areas .chip").forEach(chip => chip.onclick = () => { healthState.painArea = chip.dataset.area; $$("#pain-areas .chip").forEach(c => c.classList.toggle("active", c === chip)); });
  $("#pain-severity").value = existing?.severity || 4; $("#pain-severity-label").textContent = `${$("#pain-severity").value}/10`;
  $("#pain-notes").value = existing?.notes || "";
  openDialog($("#pain-dialog"));
}

/* ---------------- Check-in extras */

function renderCheckinExtras() {
  const today = state.health?.records?.find(record => record.checkin_date === state.today.today);
  const selected = new Set(JSON.parse(today?.tags_json || "[]"));
  $("#health-tags").innerHTML = healthState.hub.tags.map(tag => `<button type="button" class="chip${selected.has(tag) ? " active" : ""}" data-tag="${esc(tag)}">${esc(titleCase(tag))}</button>`).join("");
  $$("#health-tags .chip").forEach(chip => chip.onclick = () => chip.classList.toggle("active"));
  const target = healthState.hub.body.protein_target;
  $("#protein-target-hint").textContent = target ? `grams · target ${target}` : "grams";
  if (today) { $("#health-protein-g").value = today.protein_g ?? ""; $("#health-water").value = today.water_l ?? ""; }
}

/* ---------------- Progress: goals, insights, trends */

function renderGoals() {
  const goals = healthState.goals;
  $("#goal-list").innerHTML = goals.length ? goals.map(goal => {
    const unit = goal.kind === "lift" || goal.kind === "bodyweight" ? " lb" : goal.kind === "weekly_miles" ? " mi" : "";
    return `<article class="card goal-card${goal.achieved ? " achieved" : ""}"><div class="goal-top"><h3>${goal.achieved ? "✓ " : ""}${esc(goal.title)}</h3><span>${goal.current ?? "—"} / ${goal.target}${unit}</span></div>
      <div class="meter"><i style="width:${goal.percent ?? 0}%"></i></div>
      <p class="faint" style="margin:6px 0 0">${goal.kind === "lift" ? `${esc(goal.exercise)} · estimated 1RM` : esc(goal.kind.replace("_", " "))}${goal.deadline ? ` · by ${esc(fmtShort(goal.deadline))}` : ""}${goal.achieved_on ? ` · reached ${esc(fmtShort(goal.achieved_on))}` : ""}</p>
      <button type="button" data-goal-delete="${goal.id}">Remove</button></article>`;
  }).join("") : `<article class="card empty"><b>No goals yet</b>Set a strength, bodyweight, mileage or VO₂ max target and track progress here.</article>`;
  $$("[data-goal-delete]").forEach(button => button.onclick = async () => {
    if (!confirm("Remove this goal?")) return;
    healthState.goals = (await api(`/api/health/goals/${button.dataset.goalDelete}`, { method: "DELETE" })).goals; renderGoals();
  });
}

function renderInsights() {
  const { insights, pending } = healthState.insights;
  const items = insights.map(item => {
    const sign = item.difference > 0 ? "+" : "";
    const tone = item.favorable === true ? "good" : item.favorable === false ? "bad" : "";
    return `<div class="insight"><b>${esc(item.title)}</b><span class="delta ${tone}">${sign}${item.difference}${esc(item.unit)}</span> <span class="muted">(${item.with_avg} vs ${item.without_avg})</span><br><small>${esc(item.confidence)} · ${item.n_with} vs ${item.n_without} days</small></div>`;
  }).join("");
  const todo = pending.length ? `<p class="faint" style="margin-top:10px">${pending.map(esc).join(" ")}</p>` : "";
  $("#insight-list").innerHTML = (items || `<p class="muted">Tag your check-ins (alcohol, late caffeine, travel…) and patterns from your own data will show up here.</p>`) + todo;
}

function sparkCard(label, points, format) {
  const values = points.filter(point => point.value != null);
  if (values.length < 2) return `<article class="card trend"><div class="trend-head"><span>${esc(label)}</span><b>—</b></div><div class="bars"><span class="empty">Not enough data</span></div></article>`;
  const nums = values.map(point => point.value);
  const min = Math.min(...nums); const max = Math.max(...nums); const spread = Math.max(0.1, max - min);
  const change = nums[nums.length - 1] - nums[0];
  const bars = values.map(point => `<i style="height:${15 + ((point.value - min) / spread) * 85}%" title="${esc(fmtShort(point.week))}: ${esc(format(point.value))}"></i>`).join("");
  return `<article class="card trend"><div class="trend-head"><span>${esc(label)}</span><b>${esc(format(nums[nums.length - 1]))}</b><span>${change >= 0 ? "+" : ""}${format(change)} over period</span></div><div class="bars">${bars}</div></article>`;
}

function renderLongTrends() {
  const t = healthState.trends;
  const series = key => t.weeks.map(week => ({ week: week.week, value: week[key] }));
  const cards = [
    sparkCard("VO₂ max", series("vo2_max"), v => (+v).toFixed(1)),
    sparkCard("Resting HR", series("resting_hr"), v => `${Math.round(v)}`),
    sparkCard("HRV", series("hrv"), v => `${Math.round(v)}`),
    sparkCard("Sleep", series("sleep_hours"), v => `${(+v).toFixed(1)}h`),
    sparkCard("Weight", series("weight"), v => `${(+v).toFixed(1)}`),
    sparkCard("Run miles / week", series("run_miles"), v => (+v).toFixed(1)),
    ...t.lifts.map(name => sparkCard(`${name} e1RM`, t.weeks.map(week => ({ week: week.week, value: week.lifts[name] ?? null })), v => `${Math.round(v)}`)),
  ];
  $("#long-trends").innerHTML = cards.join("");
  $$("[data-trend-days]").forEach(button => button.classList.toggle("active", Number(button.dataset.trendDays) === healthState.trendDays));
  const prediction = t.race_predictions;
  $("#race-predictions").innerHTML = prediction
    ? `<p class="kicker">Race predictions · VO₂ max ${prediction.vo2_max}</p><div class="prediction-grid">${Object.entries(prediction.times).map(([label, time]) => `<div><span>${esc(label)}</span><b>${esc(time)}</b></div>`).join("")}</div><p class="faint" style="margin-top:8px">${esc(prediction.note)}</p>`
    : `<p class="muted">Race predictions appear once Garmin reports a VO₂ max.</p>`;
  const lifts = $("#goal-lifts");
  lifts.innerHTML = (state.progress?.exercises || []).map(exercise => `<option value="${esc(exercise.name)}"></option>`).join("");
}

/* ---------------- Records */

async function renderRecords() {
  const data = await api("/api/health/records");
  healthState.records = data.records;
  const labels = { blood_pressure: "Blood pressure", lab: "Lab results", medication: "Medications", supplement: "Supplements", visit: "Doctor visits" };
  const groups = Object.keys(labels).map(kind => {
    const rows = data.records.filter(record => record.kind === kind);
    if (!rows.length) return "";
    return `<div class="record-group"><h3>${labels[kind]}</h3><article class="card list-card">${rows.map(record => {
      const value = [record.value, record.unit].filter(Boolean).join(" ");
      const stoppable = kind === "medication" || kind === "supplement";
      return `<div class="row-item"><div class="grow"><b>${esc(record.name)}${value ? ` · ${esc(value)}` : ""}${stoppable && !record.active ? " (stopped)" : ""}</b><small>${esc(fmtShort(record.recorded_on))}${record.reference ? ` · ref ${esc(record.reference)}` : ""}${record.notes ? ` · ${esc(record.notes)}` : ""}</small></div><div class="record-actions">${stoppable ? `<button type="button" data-record-toggle="${record.id}">${record.active ? "Stop" : "Resume"}</button>` : ""}<button type="button" data-record-delete="${record.id}">Delete</button></div></div>`;
    }).join("")}</article></div>`;
  }).join("");
  $("#record-groups").innerHTML = groups || `<article class="card empty"><b>No records yet</b>Add blood pressure readings, lab results, medications, supplements, or visit notes.</article>`;
  $$("[data-record-toggle]").forEach(button => button.onclick = async () => { await api(`/api/health/records/${button.dataset.recordToggle}/toggle`, { method: "POST" }); renderRecords(); });
  $$("[data-record-delete]").forEach(button => button.onclick = async () => {
    if (!confirm("Delete this record permanently?")) return;
    await api(`/api/health/records/${button.dataset.recordDelete}`, { method: "DELETE" }); renderRecords();
  });
}

function syncRecordForm() {
  const kind = $("#record-kind").value;
  const hints = {
    blood_pressure: ["Reading", "Blood pressure", "120/80", "mmHg"], lab: ["Test", "e.g. LDL cholesterol", "", ""],
    supplement: ["Supplement", "e.g. Creatine", "5", "g/day"], medication: ["Medication", "", "", ""], visit: ["Visit", "e.g. Annual physical", "", ""],
  }[kind];
  $("#record-name-label").firstChild.textContent = hints[0];
  $("#record-name").placeholder = hints[1];
  $("#record-value").placeholder = hints[2];
  $("#record-unit").placeholder = hints[3];
  if (kind === "blood_pressure") { $("#record-name").value = "Blood pressure"; $("#record-unit").value = "mmHg"; }
}

/* ---------------- Reminders (fire while the app is open or installed) */

let reminderTimer = null;
let reminderSettings = null;

async function loadReminders() {
  reminderSettings = await api("/api/health/reminders");
  $("#reminder-enabled").checked = reminderSettings.enabled;
  $("#reminder-checkin").value = reminderSettings.checkin || "";
  $("#reminder-workout").value = reminderSettings.workout || "";
  $("#reminder-bedtime").checked = reminderSettings.bedtime;
  const bedWindow = reminderSettings.bedtime_window;
  $("#reminder-bedtime-note").textContent = bedWindow?.window_start ? `30 minutes before Garmin's window (tonight ${bedWindow.window_start}).` : "30 minutes before Garmin's recommended window.";
  $("#reminder-permission").textContent = !("Notification" in globalThis) ? "This browser doesn't support notifications." : Notification.permission === "granted" ? "Notifications allowed." : Notification.permission === "denied" ? "Notifications are blocked for this site in browser settings." : "Your browser will ask permission.";
}

function scheduleReminders() {
  if (!reminderSettings) loadReminders().then(scheduleReminders).catch(() => {});
  clearInterval(reminderTimer);
  reminderTimer = setInterval(checkReminders, 60 * 1000);
}

function notify(key, title, body) {
  const stamp = `reminder:${key}:${new Date().toDateString()}`;
  try { if (localStorage.getItem(stamp)) return; localStorage.setItem(stamp, "1"); } catch { /* storage optional */ }
  if (!("Notification" in globalThis) || Notification.permission !== "granted") return;
  navigator.serviceWorker?.getRegistration?.().then(registration => registration ? registration.showNotification(title, { body, tag: key }) : new Notification(title, { body })).catch(() => new Notification(title, { body }));
}

function checkReminders() {
  const r = reminderSettings;
  if (!r?.enabled) return;
  const now = new Date();
  const minutes = now.getHours() * 60 + now.getMinutes();
  const at = text => { const [h, m] = text.split(":").map(Number); return h * 60 + m; };
  if (r.checkin && minutes >= at(r.checkin) && !state.today?.checked_in_today) notify("checkin", "Daily check-in", "Log sleep, energy and soreness — it takes under a minute.");
  if (r.workout && minutes >= at(r.workout) && state.today?.focus && state.today.focus.status === "planned") notify("workout", `Today: ${cleanTitle(state.today.focus.title)}`, state.today.recommendation?.headline || "Your planned session is ready.");
  const start = r.bedtime_window?.start_minutes;
  if (r.bedtime && start != null) {
    const remindAt = (start - 30 + 1440) % 1440;
    const late = (minutes - remindAt + 1440) % 1440;
    if (late < 60) notify("bedtime", "Wind down for bed", `Garmin suggests sleeping between ${r.bedtime_window.window_start} and ${r.bedtime_window.window_end}.`);
  }
}

/* ---------------- wiring */

$$("[data-trend-days]").forEach(button => button.onclick = async () => {
  healthState.trendDays = Number(button.dataset.trendDays);
  healthState.trends = await api(`/api/health/trends?days=${healthState.trendDays}`);
  renderLongTrends();
});
$("#log-pain").onclick = () => openPainDialog();
$("#pain-severity").oninput = () => { $("#pain-severity-label").textContent = `${$("#pain-severity").value}/10`; };
$("#pain-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (!healthState.painArea) { alert("Pick where it hurts."); return; }
  await api("/api/health/pain", { method: "POST", body: JSON.stringify({ area: healthState.painArea, severity: Number($("#pain-severity").value), notes: $("#pain-notes").value.trim() }) });
  $("#pain-dialog").close(); await renderHealthHub();
});
$("#measure-form").addEventListener("submit", async event => {
  event.preventDefault();
  const value = id => ($(id).value ? Number($(id).value) : null);
  try {
    await api("/api/health/body", { method: "POST", body: JSON.stringify({ measured_on: $("#measure-date").value || null, weight_lbs: value("#measure-weight"), waist_in: value("#measure-waist"), body_fat_pct: value("#measure-bf") }) });
    $("#measure-dialog").close(); await load();
  } catch (error) { alert(error.message); }
});
$("#record-kind").onchange = syncRecordForm;
$("#add-record").onclick = () => { $("#record-form").reset(); $("#record-date").value = state.today.today; $("#record-status").hidden = true; syncRecordForm(); openDialog($("#record-dialog")); };
$("#record-form").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    await api("/api/health/records", { method: "POST", body: JSON.stringify({ kind: $("#record-kind").value, name: $("#record-name").value.trim() || $("#record-kind").selectedOptions[0].textContent, value: $("#record-value").value.trim(), unit: $("#record-unit").value.trim(), reference: $("#record-reference").value.trim(), notes: $("#record-notes").value.trim(), recorded_on: $("#record-date").value || null }) });
    $("#record-dialog").close(); renderRecords();
  } catch (error) { $("#record-status").textContent = error.message; $("#record-status").hidden = false; }
});
$$('[data-view="records"]').forEach(button => button.addEventListener("click", () => renderRecords()));
$("#add-goal").onclick = () => { $("#goal-form").reset(); $("#goal-status").hidden = true; $("#goal-exercise-label").hidden = false; openDialog($("#goal-dialog")); };
$("#goal-kind").onchange = () => { $("#goal-exercise-label").hidden = $("#goal-kind").value !== "lift"; };
$("#goal-form").addEventListener("submit", async event => {
  event.preventDefault();
  const kind = $("#goal-kind").value;
  const target = Number($("#goal-target").value);
  const exercise = $("#goal-exercise").value.trim();
  const title = $("#goal-title").value.trim() || (kind === "lift" ? `${exercise} ${target} lb` : kind === "bodyweight" ? `Bodyweight ${target} lb` : kind === "weekly_miles" ? `${target} miles a week` : `VO₂ max ${target}`);
  try {
    healthState.goals = (await api("/api/health/goals", { method: "POST", body: JSON.stringify({ kind, target, exercise: kind === "lift" ? exercise : null, title, deadline: $("#goal-deadline").value || null }) })).goals;
    $("#goal-dialog").close(); renderGoals();
  } catch (error) { $("#goal-status").textContent = error.message; $("#goal-status").hidden = false; }
});
$("#habit-form").addEventListener("submit", async event => {
  event.preventDefault();
  healthState.habits = (await api("/api/health/habits", { method: "POST", body: JSON.stringify({ name: $("#habit-name").value.trim() }) })).habits;
  $("#habit-dialog").close(); renderHabits();
});
$("#reminder-form").addEventListener("submit", async event => {
  event.preventDefault();
  let enabled = $("#reminder-enabled").checked;
  if (enabled && "Notification" in globalThis && Notification.permission === "default") enabled = (await Notification.requestPermission()) === "granted";
  reminderSettings = await api("/api/health/reminders", { method: "PUT", body: JSON.stringify({ enabled, checkin: $("#reminder-checkin").value || null, workout: $("#reminder-workout").value || null, bedtime: $("#reminder-bedtime").checked }) });
  await loadReminders(); scheduleReminders();
});
$$("#pain-dialog, #measure-dialog, #record-dialog, #goal-dialog, #habit-dialog").forEach(dialog => {
  dialog.addEventListener("click", event => { if (event.target === dialog) dialog.close(); });
  dialog.querySelector(".sheet-close").onclick = () => dialog.close();
});
loadReminders().catch(() => {});
