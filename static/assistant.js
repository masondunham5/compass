/* Assistant: Today's schedule (calendar events, free time, best workout slot), exam
   heads-ups, and calendar management in Settings. Uses app.js helpers. */

const timeOf = iso => new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });

async function renderAssistant() {
  const [agenda, alerts, feeds, todayPlan] = await Promise.all([api("/api/assistant/agenda"), api("/api/assistant/alerts"), api("/api/assistant/calendars"), api("/api/assistant/plan?days=1").catch(() => null)]);
  renderAgenda(agenda, alerts.alerts, todayPlan);
  if (window.renderCheckin) window.renderCheckin("#checkin-today");
  renderCalendarSettings(feeds.calendars);
  renderOutlook(await api("/api/assistant/outlook").catch(() => null));
}

/* ---------------- Personal Outlook (write-back) */

let outlookPoll = null;

function renderOutlook(status) {
  const box = $("#outlook-body");
  if (!box || !status) return;
  clearTimeout(outlookPoll);
  const signIn = status.sign_in || {};
  if (signIn.state === "waiting" && !status.connected) {
    box.innerHTML = `<div class="device-code"><span>Your code</span><b>${esc(signIn.code)}</b></div>
      <p>1. Open <a href="${esc(signIn.url)}" target="_blank" rel="noopener">${esc(signIn.url.replace(/^https?:\/\//, ""))}</a> and enter the code.<br>2. Sign in with your <b>personal</b> Microsoft account and allow access to your calendars.</p>
      <p class="faint">Waiting for you to finish… this page updates by itself.</p>`;
    outlookPoll = setTimeout(async () => renderOutlook(await api("/api/assistant/outlook")), 3000);
    return;
  }
  if (status.connected) {
    box.innerHTML = `<p><b>Connected</b> as ${esc(status.account || "your account")}.<br>${status.linked_events} event${status.linked_events === 1 ? "" : "s"} Compass added to your Calendar${status.last_sync ? ` · last sync ${esc(syncTime(status.last_sync))}` : ""}.</p>
      ${status.last_error ? `<p class="note error">${esc(status.last_error)}</p>` : ""}
      <p class="faint">Syncs a few seconds after you accept or change plan blocks, and every 30 minutes. Edits made in Outlook are overwritten — change things in Compass.</p>
      <div class="sheet-actions"><button class="btn btn-primary" type="button" id="outlook-sync">Sync now</button><button class="btn btn-ghost" type="button" id="outlook-disconnect">Disconnect</button></div>
      <p class="note" id="outlook-status" hidden></p>`;
    $("#outlook-sync").onclick = async event => {
      event.target.disabled = true; event.target.textContent = "Syncing…";
      try {
        const result = await api("/api/assistant/outlook/sync", { method: "POST" });
        renderOutlook(result);
        const r = result.result; const note = $("#outlook-status");
        note.textContent = `Done: ${r.created} added, ${r.updated} updated, ${r.deleted} removed, ${r.unchanged} unchanged.`; note.hidden = false;
      } catch (error) { const note = $("#outlook-status"); note.textContent = error.message; note.classList.add("error"); note.hidden = false; event.target.disabled = false; event.target.textContent = "Sync now"; }
    };
    $("#outlook-disconnect").onclick = async () => {
      if (!confirm("Disconnect Outlook on this computer? Events Compass already added stay in your Outlook Calendar.")) return;
      renderOutlook(await api("/api/assistant/outlook/disconnect", { method: "POST" }));
    };
    return;
  }
  box.innerHTML = `${signIn.state === "error" ? `<p class="note error">${esc(signIn.message)}</p>` : ""}
    <p class="faint">One-time setup: create a free app registration in the Azure portal (personal Microsoft accounts, “Allow public client flows” on, delegated Calendars.ReadWrite). Paste its <b>Application (client) ID</b> here — it isn't a password.</p>
    <form id="outlook-form" class="inline-form"><input id="outlook-client" placeholder="Application (client) ID" autocomplete="off" spellcheck="false" required><button class="btn btn-primary" type="submit">Connect</button></form>
    <p class="note error" id="outlook-error" hidden></p>`;
  $("#outlook-form").onsubmit = async event => {
    event.preventDefault();
    try { renderOutlook({ connected: false, sign_in: await api("/api/assistant/outlook/connect", { method: "POST", body: JSON.stringify({ client_id: $("#outlook-client").value.trim() }) }) }); }
    catch (error) { $("#outlook-error").textContent = error.message; $("#outlook-error").hidden = false; }
  };
}
window.renderAssistant = renderAssistant;

function renderAgenda(agenda, alerts, todayPlan = null) {
  const card = $("#agenda-card");
  card.hidden = !agenda.has_calendar;
  const kicker = $("#today-kicker");
  const greeting = kicker.textContent.split(" · ")[0];
  const count = agenda.events.filter(event => event.busy || event.all_day).length;
  kicker.textContent = agenda.has_calendar ? `${greeting} · ${count ? `${count} on your calendar` : "nothing on your calendar"}` : greeting;
  if (!agenda.has_calendar) return;
  const allDay = agenda.events.filter(event => event.all_day);
  const timed = agenda.events.filter(event => !event.all_day);
  const rows = [
    ...allDay.map(event => ({ sort: -1, html: `<li class="${/exam|midterm|final|quiz|test/i.test(event.title) ? "exam" : ""}"><time>All day</time><div><b>${esc(event.title)}</b>${event.location ? `<small>${esc(event.location)}</small>` : ""}</div></li>` })),
    ...timed.map(event => ({ sort: new Date(event.start).getTime(), html: `<li class="${event.busy ? "" : "free"}${String(event.feed_kind || "").startsWith("plan") ? " planned" : event.feed_kind === "work" ? " work" : /exam|midterm|final|quiz|test/i.test(event.title) ? " exam" : ""}"><time>${esc(timeOf(event.start))}</time><div><b>${esc(event.title)}</b><small>${esc(timeOf(event.start))}–${esc(timeOf(event.end))}${event.location ? ` · ${esc(event.location)}` : ""}${event.busy ? "" : " · shown as free"}</small></div></li>` })),
  ].sort((a, b) => a.sort - b.sort);
  const planned = agenda.workouts.filter(workout => workout.status === "planned");
  const busyHours = (agenda.busy_minutes / 60).toFixed(1);
  const slot = agenda.best_slot && planned.length
    ? `<div class="best-slot">Best time for ${esc(cleanTitle(planned[0].title))}: ${esc(agenda.best_slot.start)}–${esc(agenda.best_slot.end)}</div>`
    : planned.length ? `<div class="best-slot">No free ${planned[0].minutes}-minute gap between 6 AM and 10 PM — consider the short version or moving it.</div>` : "";
  const suggested = (todayPlan?.days?.[0]?.blocks || []).filter(block => block.status === "draft");
  const suggestion = suggested.length ? `<button type="button" class="best-slot as-link" id="review-today-plan">${suggested.length} suggested block${suggested.length === 1 ? "" : "s"} for today (${suggested.map(b => KIND_LABEL_SHORT[b.kind]).filter((v, i, a) => a.indexOf(v) === i).join(" + ")}) — review in Week</button>` : "";
  const examAlerts = alerts.map(alert => `<div class="alert ${esc(alert.level)}" style="margin-top:10px">${icon("alert")}<div><b>${esc(alert.title)}</b><p>${esc(alert.message)}</p></div></div>`).join("");
  card.innerHTML = `<div class="agenda-head"><h3>Today's schedule</h3><span>${agenda.events.length ? `${busyHours} h busy` : "Nothing on the calendar"}</span></div>
    ${rows.length ? `<ul class="agenda-list">${rows.map(row => row.html).join("")}</ul>` : `<p class="muted">No classes or events today.</p>`}${slot}${suggestion}${examAlerts}`;
  const review = $("#review-today-plan");
  if (review) review.onclick = () => selectView("week");
}
const KIND_LABEL_SHORT = { study: "study", review: "exam review", workout: "workout", task: "to-do", move: "move" };

function renderCalendarSettings(feeds) {
  $("#calendar-list").innerHTML = feeds.map(feed => `<div class="feed"><b>${esc(feed.name)} <span class="muted">· ${esc(feed.kind)}${feed.source === "file" ? " · uploaded file" : ""}</span></b>
      <small>${feed.url_hint ? `${esc(feed.url_hint)} · ` : ""}${feed.event_count} events · ${feed.last_synced ? `synced ${esc(syncTime(feed.last_synced))}` : "not synced"}</small>
      ${feed.last_error ? `<small class="error">${esc(feed.last_error)}</small>` : ""}
      <div class="record-actions">${feed.source === "url" ? `<button type="button" data-feed-sync="${esc(feed.id)}">Refresh now</button>` : ""}<button type="button" data-feed-remove="${esc(feed.id)}">Remove</button></div></div>`).join("");
  $$("[data-feed-sync]").forEach(button => button.onclick = async () => {
    button.disabled = true; button.textContent = "Refreshing…";
    try { await api(`/api/assistant/calendars/${button.dataset.feedSync}/sync`, { method: "POST" }); } catch (error) { calendarStatus(error.message, true); }
    await renderAssistant();
  });
  $$("[data-feed-remove]").forEach(button => button.onclick = async () => {
    if (!confirm("Remove this calendar from Compass? Your actual calendar isn't affected.")) return;
    await api(`/api/assistant/calendars/${button.dataset.feedRemove}`, { method: "DELETE" });
    await renderAssistant();
  });
}

function calendarStatus(text, error = false) {
  const status = $("#calendar-status");
  status.textContent = text; status.hidden = !text; status.classList.toggle("error", error);
}

/* ---------------- Week view: calendar + training, day by day */

let weekStartOffset = 0;

async function renderWeek(offset = weekStartOffset) {
  weekStartOffset = offset;
  const today = asDate(state.today.today);
  const monday = new Date(today); monday.setDate(today.getDate() - ((today.getDay() + 6) % 7) + offset * 7);
  const start = monday.toISOString().slice(0, 10);
  const end = new Date(monday.getTime() + 6 * 86400000).toISOString().slice(0, 10);
  $("#week-kicker").textContent = offset === 0 ? "This week" : offset === 1 ? "Next week" : offset === -1 ? "Last week" : `Week of ${fmtShort(start).replace(/^\w+, /, "")}`;
  $("#week-title").textContent = `${fmtShort(start).replace(/^\w+, /, "")} – ${fmtShort(end).replace(/^\w+, /, "")}`;
  $("#week-prev").disabled = offset <= -4;
  if (window.syncCalendarModeUI) window.syncCalendarModeUI();
  if (window.calendarMode && window.calendarMode() !== "list") {  // Day / Week time grid (calendar.js)
    await window.renderCalendar(start).catch(error => { $("#cal-grid").innerHTML = `<p class="note error">${esc(error.message)}</p>`; });
    return;
  }
  if (window.renderTodos) window.renderTodos();
  const { days } = await api(`/api/assistant/week?start=${start}&days=7`);
  $("#week-kicker").textContent = offset === 0 ? "This week" : offset === 1 ? "Next week" : offset === -1 ? "Last week" : `Week of ${fmtShort(start).replace(/^\w+, /, "")}`;
  $("#week-title").textContent = `${fmtShort(start).replace(/^\w+, /, "")} – ${fmtShort(end).replace(/^\w+, /, "")}`;
  $("#week-prev").disabled = offset <= -4;
  const todayIso = state.today.today;
  $("#week-days").innerHTML = days.map(day => {
    const isToday = day.date === todayIso;
    const workouts = day.workouts.map(w => `<button type="button" class="week-workout ${esc(w.status)}" data-session="${esc(w.id)}"><span>${w.kind === "run" ? "Run" : w.kind === "strength" ? "Lift" : "Move"}</span>${esc(cleanTitle(w.title))}${w.status === "completed" ? " ✓" : ""}</button>`).join("");
    const busy = day.busy_minutes ? `${(day.busy_minutes / 60).toFixed(1)} h busy` : day.event_count ? `${day.event_count} event${day.event_count === 1 ? "" : "s"}` : "Free";
    const exams = day.exams.length ? `<span class="exam-tag">${esc(day.exams[0])}${day.exams.length > 1 ? ` +${day.exams.length - 1}` : ""}</span>` : "";
    const slot = day.best_slot && day.workouts.some(w => w.status === "planned") ? `<small>Best time to train ${esc(day.best_slot.start)}</small>` : "";
    return `<article class="card week-day${isToday ? " is-today" : ""}${day.heavy ? " heavy" : ""}"><button type="button" class="week-day-head" data-day="${day.date}"><div class="date-chip"><span>${esc(fmtDow(day.date))}</span><b>${asDate(day.date).getDate()}</b></div><div class="grow"><b>${busy}</b>${exams}${slot}</div>${icon("chevron")}</button>${workouts ? `<div class="week-workouts">${workouts}</div>` : ""}</article>`;
  }).join("");
  $$("#week-days [data-session]").forEach(button => button.onclick = async () => openSessionSheet(await api(`/api/sessions/${encodeURIComponent(button.dataset.session)}`)));
  $$("#week-days [data-day]").forEach(button => button.onclick = () => openDaySheet(button.dataset.day));
  if (window.renderWeekPlan) await window.renderWeekPlan(start).catch(error => { $("#plan-summary").innerHTML = `<p class="note error">${esc(error.message)}</p>`; });
}
window.renderWeek = renderWeek;

async function openDaySheet(day) {
  const agenda = await api(`/api/assistant/agenda?day=${day}`);
  const sheet = $("#session-sheet");
  const events = agenda.events.map(event => `<li><time>${event.all_day ? "All day" : esc(timeOf(event.start))}</time><div><b>${esc(event.title)}</b>${event.all_day ? "" : `<small>${esc(timeOf(event.start))}–${esc(timeOf(event.end))}${event.location ? ` · ${esc(event.location)}` : ""}</small>`}</div></li>`).join("");
  const free = agenda.free_slots.map(slot => `${slot.start}–${slot.end}`).join(", ");
  $("#session-sheet-body").innerHTML = `<button class="sheet-close" type="button" aria-label="Close">${icon("close")}</button>
    <p class="kicker">${agenda.has_calendar ? `${(agenda.busy_minutes / 60).toFixed(1)} h busy` : "No calendar connected"}</p><h2>${esc(fmtLong(day))}</h2>
    ${events ? `<ul class="agenda-list">${events}</ul>` : `<p class="muted">Nothing on the calendar.</p>`}
    ${free ? `<p class="faint">Free: ${esc(free)}</p>` : ""}
    ${agenda.workouts.map(w => `<button type="button" class="row-item" data-open="${esc(w.id)}"><div class="grow"><b>${esc(cleanTitle(w.title))}</b><small>${esc(titleCase(w.kind))} · ${w.minutes} min · ${esc(w.status)}</small></div>${icon("chevron")}</button>`).join("")}`;
  sheet.querySelector(".sheet-close").onclick = () => sheet.close();
  sheet.querySelectorAll("[data-open]").forEach(button => button.onclick = async () => {
    const session = await api(`/api/sessions/${encodeURIComponent(button.dataset.open)}`);
    sheet.close();
    openSessionSheet(session);
  });
  openDialog(sheet);
}

$("#week-prev").onclick = () => renderWeek(weekStartOffset - 1);
$("#week-next").onclick = () => renderWeek(weekStartOffset + 1);
$("#week-push").onclick = () => window.openReschedule(null, "push");

$("#calendar-form").addEventListener("submit", async event => {
  event.preventDefault();
  const url = $("#calendar-url").value.trim();
  if (!url) { calendarStatus("Paste the calendar link first, or use Upload .ics.", true); return; }
  const submit = event.submitter; submit.disabled = true; submit.textContent = "Reading calendar…";
  try {
    const result = await api("/api/assistant/calendars", { method: "POST", body: JSON.stringify({ name: $("#calendar-name").value.trim(), url, kind: $("#calendar-kind").value }) });
    $("#calendar-url").value = "";  // don't leave the secret link sitting in the form
    calendarStatus(`Added ${result.calendar.name}: ${result.calendar.event_count} events imported.`);
    await renderAssistant();
  } catch (error) { calendarStatus(error.message, true); }
  finally { submit.disabled = false; submit.textContent = "Add link"; }
});

$("#calendar-file").addEventListener("change", async event => {
  const file = event.target.files[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file); form.append("name", $("#calendar-name").value.trim() || file.name.replace(/\.ics$/i, "")); form.append("kind", $("#calendar-kind").value);
  calendarStatus("Reading file…");
  try {
    const result = await api("/api/assistant/calendars/upload", { method: "POST", body: form });
    calendarStatus(`Added ${result.calendar.name}: ${result.calendar.event_count} events imported.`);
    await renderAssistant();
  } catch (error) { calendarStatus(error.message, true); }
  finally { event.target.value = ""; }
});
