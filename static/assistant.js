/* Assistant: Today's schedule (calendar events, free time, best workout slot), exam
   heads-ups, and calendar management in Settings. Uses app.js helpers. */

const timeOf = iso => new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });

async function renderAssistant() {
  const [agenda, alerts, feeds] = await Promise.all([api("/api/assistant/agenda"), api("/api/assistant/alerts"), api("/api/assistant/calendars")]);
  renderAgenda(agenda, alerts.alerts);
  renderCalendarSettings(feeds.calendars);
}
window.renderAssistant = renderAssistant;

function renderAgenda(agenda, alerts) {
  const card = $("#agenda-card");
  card.hidden = !agenda.has_calendar;
  if (!agenda.has_calendar) return;
  const allDay = agenda.events.filter(event => event.all_day);
  const timed = agenda.events.filter(event => !event.all_day);
  const rows = [
    ...allDay.map(event => ({ sort: -1, html: `<li class="${/exam|midterm|final|quiz|test/i.test(event.title) ? "exam" : ""}"><time>All day</time><div><b>${esc(event.title)}</b>${event.location ? `<small>${esc(event.location)}</small>` : ""}</div></li>` })),
    ...timed.map(event => ({ sort: new Date(event.start).getTime(), html: `<li class="${event.busy ? "" : "free"}${/exam|midterm|final|quiz|test/i.test(event.title) ? " exam" : ""}"><time>${esc(timeOf(event.start))}</time><div><b>${esc(event.title)}</b><small>${esc(timeOf(event.start))}–${esc(timeOf(event.end))}${event.location ? ` · ${esc(event.location)}` : ""}${event.busy ? "" : " · shown as free"}</small></div></li>` })),
  ].sort((a, b) => a.sort - b.sort);
  const planned = agenda.workouts.filter(workout => workout.status === "planned");
  const busyHours = (agenda.busy_minutes / 60).toFixed(1);
  const slot = agenda.best_slot && planned.length
    ? `<div class="best-slot">Best time for ${esc(cleanTitle(planned[0].title))}: ${esc(agenda.best_slot.start)}–${esc(agenda.best_slot.end)}</div>`
    : planned.length ? `<div class="best-slot">No free ${planned[0].minutes}-minute gap between 6 AM and 10 PM — consider the short version or moving it.</div>` : "";
  const examAlerts = alerts.map(alert => `<div class="alert ${esc(alert.level)}" style="margin-top:10px">${icon("alert")}<div><b>${esc(alert.title)}</b><p>${esc(alert.message)}</p></div></div>`).join("");
  card.innerHTML = `<div class="agenda-head"><h3>Today's schedule</h3><span>${agenda.events.length ? `${busyHours} h busy` : "Nothing on the calendar"}</span></div>
    ${rows.length ? `<ul class="agenda-list">${rows.map(row => row.html).join("")}</ul>` : `<p class="muted">No classes or events today.</p>`}${slot}${examAlerts}`;
}

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
    if (!confirm("Remove this calendar from Health Hub? Your actual calendar isn't affected.")) return;
    await api(`/api/assistant/calendars/${button.dataset.feedRemove}`, { method: "DELETE" });
    await renderAssistant();
  });
}

function calendarStatus(text, error = false) {
  const status = $("#calendar-status");
  status.textContent = text; status.hidden = !text; status.classList.toggle("error", error);
}

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
