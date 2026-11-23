/* Calendar: Day / Week time grids inside the Week tab, your own events, and to-dos.
   Everything comes from GET /api/assistant/calendar (school + Outlook + your events + work +
   plan blocks + draft suggestions + workouts). Uses app.js / planner.js helpers. */

const HOUR_PX = 48;
const cal = { mode: "day", day: null, data: null, start: null };
try { cal.mode = localStorage.getItem("compass.calMode") || "day"; } catch { /* storage blocked */ }

const SOURCE_LABEL = { feed: "Calendar", outlook: "Outlook", mine: "Your event", work: "Work", plan: "Plan", draft: "Suggestion", session: "Workout", travel: "Travel" };
const directionsUrl = destination => `https://www.google.com/maps/dir/?api=1&destination=${encodeURIComponent(destination)}`;
const BLOCK_LABEL = { study: "Study", review: "Exam review", workout: "Workout", task: "To-do", move: "Suggested move" };
const REPEAT_LABEL = { none: "Doesn't repeat", daily: "Every day", weekdays: "Weekdays", weekly: "Every week", biweekly: "Every 2 weeks", monthly: "Every month" };

const clockLabel = minutes => { const h = Math.floor(minutes / 60) % 24, m = minutes % 60; return `${(h % 12) || 12}${m ? `:${String(m).padStart(2, "0")}` : ""} ${h < 12 ? "AM" : "PM"}`; };
const hhmm = minutes => { const m = Math.min(minutes, 24 * 60 - 1); return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`; };
const fromHHMM = value => { const [h, m] = String(value || "0:0").split(":").map(Number); return h * 60 + m; };
// addDays(iso, n) comes from schedule.js.

/* The color key for an item: plan blocks by kind, everything else by source. */
function colorKey(item) {
  if (item.kind === "move") return "outlook";
  if (item.source === "plan" || item.source === "draft") return item.kind;
  if (item.source === "session") return "workout";
  return item.source;
}

/* ---------------------------------------------------------------- entry point (called by renderWeek) */

async function renderCalendar(start) {
  cal.start = start;
  const end = addDays(start, 6);
  const today = state.today.today;
  if (!cal.day || cal.day < start || cal.day > end) cal.day = today >= start && today <= end ? today : start;
  const data = await api(`/api/assistant/calendar?start=${start}&days=7`);
  cal.data = data;
  renderPlanStrip(data);
  renderCheckin("#checkin-week");
  renderDayStrip();
  drawGrid();
  renderLegend();
  renderTodos();
}
window.renderCalendar = renderCalendar;

function setMode(mode) {
  cal.mode = mode;
  try { localStorage.setItem("compass.calMode", mode); } catch { /* storage blocked */ }
  window.renderWeek();
}
window.calendarMode = () => cal.mode;

function syncModeUI() {
  $$("#cal-mode [data-mode]").forEach(button => button.classList.toggle("active", button.dataset.mode === cal.mode));
  const grid = cal.mode !== "list";
  $("#cal-grid").hidden = !grid; $("#cal-legend").hidden = !grid; $("#cal-strip").hidden = cal.mode !== "day";
  $("#week-days").hidden = grid; $("#week-push").hidden = grid;
}
window.syncCalendarModeUI = syncModeUI;

function renderPlanStrip(data) {
  const drafts = data.days.flatMap(day => day.items.filter(item => item.source === "draft"));
  const moves = drafts.filter(item => item.kind === "move").length;
  const warnings = data.warnings || [];
  if (!drafts.length && !warnings.length) { $("#plan-summary").innerHTML = ""; return; }
  $("#plan-summary").innerHTML = `<article class="card plan-card">
      <div class="plan-top"><div><b>${drafts.length ? `${drafts.length} suggested block${drafts.length === 1 ? "" : "s"}` : "Plan"}</b>
        <small>Dashed blocks are suggestions: tap one to accept it, or accept them all.${moves ? ` Includes ${moves} suggested move${moves === 1 ? "" : "s"} of your Outlook events to make room before a deadline.` : ""}</small></div>
        ${drafts.length ? `<button class="btn btn-primary small" type="button" id="cal-accept-all">Accept all</button>` : ""}</div>
      ${warnings.length ? `<ul class="plan-warnings">${warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}</article>`;
  const all = $("#cal-accept-all");
  if (all) all.onclick = () => acceptBlocks(drafts.map(item => item.block), all);
}

function renderDayStrip() {
  if (cal.mode !== "day") return;
  const today = state.today.today;
  $("#cal-strip").innerHTML = cal.data.days.map(day => {
    const timed = day.items.filter(item => !item.all_day).length;
    return `<button type="button" class="${day.date === cal.day ? "active" : ""}${day.date === today ? " is-today" : ""}" data-strip="${day.date}">
      <span>${esc(fmtDow(day.date))}</span><b>${asDate(day.date).getDate()}</b><i>${timed ? "•".repeat(Math.min(3, Math.ceil(timed / 2))) : ""}</i></button>`;
  }).join("");
  $$("#cal-strip [data-strip]").forEach(button => button.onclick = () => { cal.day = button.dataset.strip; renderDayStrip(); drawGrid(); });
}

/* ---------------------------------------------------------------- the grid */

function layoutLanes(items) {
  // Overlapping timed items share the column width: clusters of overlaps, first free lane each.
  const sorted = [...items].sort((a, b) => a.start - b.start || b.end - a.end);
  let cluster = [], clusterEnd = -1;
  const finish = () => { const lanes = Math.max(...cluster.map(item => item._lane)) + 1; cluster.forEach(item => { item._lanes = lanes; }); };
  for (const item of sorted) {
    if (cluster.length && item.start >= clusterEnd) { finish(); cluster = []; clusterEnd = -1; }
    const used = cluster.filter(other => other.end > item.start).map(other => other._lane);
    let lane = 0; while (used.includes(lane)) lane += 1;
    item._lane = lane; cluster.push(item); clusterEnd = Math.max(clusterEnd, item.end);
  }
  if (cluster.length) finish();
  return sorted;
}

function drawGrid() {
  const days = cal.mode === "day" ? cal.data.days.filter(day => day.date === cal.day) : cal.data.days;
  const timed = days.flatMap(day => day.items.filter(item => !item.all_day));
  const firstHour = Math.max(0, Math.min(7, ...timed.map(item => Math.floor(item.start / 60))));
  const lastHour = Math.min(24, Math.max(22, ...timed.map(item => Math.ceil(item.end / 60))));
  const hours = lastHour - firstHour;
  const today = state.today.today;
  const now = new Date(); const nowMin = now.getHours() * 60 + now.getMinutes();
  const isWeek = cal.mode === "week";

  const head = isWeek ? `<div class="cal-head"><span></span>${days.map(day => `<button type="button" class="${day.date === today ? "is-today" : ""}" data-pick-day="${day.date}"><span>${esc(fmtDow(day.date))}</span><b>${asDate(day.date).getDate()}</b></button>`).join("")}</div>` : "";
  const allDayRow = days.some(day => day.items.some(item => item.all_day))
    ? `<div class="cal-allday"><span>all day</span>${days.map(day => `<div>${day.items.filter(item => item.all_day).map(item => chip(item)).join("")}</div>`).join("")}</div>` : "";
  const times = Array.from({ length: hours }, (_, i) => `<span style="top:${i * HOUR_PX}px">${i === 0 ? "" : clockLabel((firstHour + i) * 60)}</span>`).join("");
  const columns = days.map(day => {
    const items = layoutLanes(day.items.filter(item => !item.all_day && item.end > firstHour * 60));
    const blocks = items.map(item => {
      const top = (Math.max(item.start, firstHour * 60) - firstHour * 60) * HOUR_PX / 60;
      const height = Math.max(16, (Math.min(item.end, lastHour * 60) - Math.max(item.start, firstHour * 60)) * HOUR_PX / 60 - 2);
      // Side by side when there's room; in narrow week columns, 3+ overlaps cascade instead of becoming slivers.
      const cascade = isWeek && item._lanes > 2;
      const left = cascade ? Math.min(item._lane, 3) * 18 : item._lane * (100 / item._lanes);
      const width = cascade ? 100 - left : 100 / item._lanes;
      const short = height < 34;
      return `<button type="button" class="cal-item c-${esc(colorKey(item))}${item.source === "draft" ? " draft" : ""}${item.busy === false && item.source !== "draft" ? " free" : ""}${item.moving || item.outcome === "missed" ? " moving" : ""}${item.outcome === "done" ? " done" : ""}${short ? " short" : ""}"
        style="top:${top}px;height:${height}px;left:calc(${left}% + 1px);width:calc(${width}% - 2px);z-index:${item._lane + 1}" data-item="${esc(item.id)}" data-date="${day.date}">
        <b>${item.kind === "move" ? "Move: " : ""}${item.outcome === "done" ? "✓ " : ""}${esc(cleanTitle(item.title))}</b>${short || isWeek ? "" : `<small>${clockLabel(item.start)} – ${clockLabel(item.end)}${item.location ? ` · ${esc(item.location)}` : ""}</small>`}</button>`;
    }).join("");
    const nowLine = day.date === today && nowMin >= firstHour * 60 && nowMin <= lastHour * 60 ? `<i class="cal-now" style="top:${(nowMin - firstHour * 60) * HOUR_PX / 60}px"></i>` : "";
    return `<div class="cal-col${day.date === today ? " is-today" : ""}" data-col="${day.date}">${blocks}${nowLine}</div>`;
  }).join("");
  $("#cal-grid").innerHTML = `<div class="cal ${isWeek ? "is-week" : "is-day"}" style="--cols:${days.length}">${head}${allDayRow}
    <div class="cal-body" style="height:${hours * HOUR_PX}px;--hour:${HOUR_PX}px"><div class="cal-times">${times}</div><div class="cal-cols">${columns}</div></div></div>`;

  $$("#cal-grid [data-pick-day]").forEach(button => button.onclick = () => { cal.day = button.dataset.pickDay; setMode("day"); });
  $$("#cal-grid [data-item]").forEach(button => button.onclick = event => { event.stopPropagation(); openItem(button.dataset.date, button.dataset.item); });
  $$("#cal-grid [data-col]").forEach(column => column.onclick = event => {
    if (event.target !== column) return;
    const minutes = firstHour * 60 + Math.round((event.offsetY / HOUR_PX) * 60 / 15) * 15;
    openAddSheet({ date: column.dataset.col, start: Math.min(minutes, 23 * 60), end: Math.min(minutes + 60, 24 * 60 - 1) });
  });
}

function chip(item) {
  return `<button type="button" class="cal-chip c-${esc(colorKey(item))}${item.status === "completed" ? " done" : ""}" data-item="${esc(item.id)}" data-date="${item.occurrence || ""}">${esc(cleanTitle(item.title))}${item.status === "completed" ? " ✓" : ""}</button>`;
}

function renderLegend() {
  const present = new Set(cal.data.days.flatMap(day => day.items.map(colorKey)));
  const names = { feed: "School / calendars", outlook: "Outlook", mine: "Your events", work: "Work", study: "Study", review: "Exam review", workout: "Workouts", task: "To-dos", travel: "Travel" };
  $("#cal-legend").innerHTML = Object.entries(names).filter(([key]) => present.has(key)).map(([key, label]) => `<span class="c-${key}"><i></i>${label}</span>`).join("")
    + (cal.data.days.some(day => day.items.some(item => item.source === "draft")) ? `<span class="draft-key"><i></i>Suggested</span>` : "");
}

/* ---------------------------------------------------------------- item sheet */

function findItem(date, id) {
  for (const day of cal.data.days) {
    const item = day.items.find(candidate => candidate.id === id);
    if (item) return { item, date: day.date };
  }
  return null;
}

async function openItem(date, id) {
  const found = findItem(date, id);
  if (!found) return;
  const { item } = found;
  if (item.source === "session") { openSessionSheet(await api(`/api/sessions/${encodeURIComponent(item.session_id)}`)); return; }
  if (item.source === "mine") { const { event } = await api(`/api/assistant/events/${encodeURIComponent(item.event_id)}`); openEventForm(event, item.occurrence); return; }
  if (item.source === "outlook") { openOutlookForm(item, found.date); return; }
  if (item.kind === "move") { openMoveSheet(item, found.date); return; }
  const when = item.all_day ? "All day" : `${clockLabel(item.start)} – ${clockLabel(item.end)}`;
  const kindLabel = item.source === "plan" || item.source === "draft" ? BLOCK_LABEL[item.kind] || "Plan" : SOURCE_LABEL[item.source];
  const where = { feed: `From ${item.calendar || "your calendar"} (read-only). Change it there.`, outlook: "From your personal Outlook. Change it there; Compass plans around it.",
    work: "From your Work schedule (More → Work schedule).", plan: item.outcome === "missed" ? "Missed: taken off your Outlook Calendar, and the work goes back into the plan." : "Accepted into your plan and in your Outlook Calendar.",
    draft: "A suggestion. Accept it to make it part of your schedule.",
    travel: `${item.calendar}. Set places and travel times in More → Settings → Places & travel.` }[item.source] || "";
  let actions = "";
  if (item.source === "draft") actions = `<button class="btn btn-primary full" type="button" id="item-accept">Accept this block</button>`;
  const started = found.date < state.today.today || (found.date === state.today.today && item.start <= new Date().getHours() * 60 + new Date().getMinutes());
  if (item.source === "plan") {
    const checkin = !started ? "" : item.outcome
      ? `<p class="muted">Marked <b>${item.outcome === "done" ? "done ✓" : "missed"}</b>. <button class="link-btn" type="button" id="item-undo">Undo</button></p>`
      : `<div class="sheet-actions"><button class="btn btn-ghost" type="button" data-outcome="missed">Missed it</button><button class="btn btn-primary" type="button" data-outcome="done">Done ✓</button></div>`;
    actions = `${checkin}<p class="note" id="item-result" hidden></p><div class="sheet-actions">${item.kind === "workout" ? `<button class="btn btn-ghost" type="button" id="item-open-workout">Open workout</button>` : ""}<button class="btn btn-danger" type="button" id="item-remove">Remove from plan</button></div>`;
  }
  if (item.all_day && item.allday_key) {
    actions += `<label class="toggle-row"><input id="item-block-day" type="checkbox" ${item.busy ? "checked" : ""}><span><b>Block this whole day</b>
      <small>Keeps plans off this day. Leave it off for reminders and labels like “Pay rent”.${item.blocked_here ? " (Set in Compass.)" : ""}</small></span></label>`;
  }
  sheet(`<p class="kicker">${esc(kindLabel)} · ${esc(fmtShort(found.date))}</p><h2>${esc(cleanTitle(item.title))}</h2>
    <p class="muted">${esc(when)}${item.location ? ` · ${esc(item.location)}` : ""}${item.place ? ` (${esc(item.place)})` : ""}${item.busy === false && !item.all_day && item.source !== "draft" && item.source !== "plan" ? " · shown as free" : ""}</p>
    ${item.directions ? `<p><a class="link-btn" href="${esc(directionsUrl(item.directions))}" target="_blank" rel="noopener">Directions ↗</a></p>` : ""}
    <p class="faint">${esc(where)}</p>${actions}`);
  const blockDay = $("#item-block-day");
  if (blockDay) blockDay.onchange = async () => {
    try { await api("/api/assistant/allday", { method: "POST", body: JSON.stringify({ key: item.allday_key, busy: blockDay.checked }) }); closeSheet(); await refreshPlanViews(); }
    catch (error) { alert(error.message); blockDay.checked = !blockDay.checked; }
  };
  const accept = $("#item-accept");
  if (accept) accept.onclick = async () => { await acceptBlocks([item.block], accept); closeSheet(); };
  $$("#cal-sheet [data-outcome]").forEach(button => button.onclick = async () => {
    const result = await markBlock(item.id, button.dataset.outcome);
    if (result?.message) { $("#item-result").textContent = result.message; $("#item-result").hidden = false; $$("#cal-sheet [data-outcome]").forEach(b => b.remove()); }
    else closeSheet();
  });
  const undo = $("#item-undo");
  if (undo) undo.onclick = async () => { await markBlock(item.id, ""); closeSheet(); };
  const remove = $("#item-remove");
  if (remove) remove.onclick = async () => { await api("/api/assistant/plan/unaccept", { method: "POST", body: JSON.stringify({ block_id: item.id }) }); closeSheet(); await refreshPlanViews(); };
  const workout = $("#item-open-workout");
  if (workout) workout.onclick = async () => { closeSheet(); openSessionSheet(await api(`/api/sessions/${encodeURIComponent(item.block.ref)}`)); };
}

/* ---------------------------------------------------------------- check-in: done / missed */

async function markBlock(blockId, outcome) {
  try {
    const result = await api(`/api/assistant/plan/blocks/${encodeURIComponent(blockId)}/outcome`, { method: "POST", body: JSON.stringify({ outcome }) });
    await refreshPlanViews();
    return result;
  } catch (error) { alert(error.message); return null; }
}

/* "How did these go?" — blocks that ended in the last few days and haven't been marked.
   The last result ("Rescheduled to Tue 3 PM") stays on the card for a little while. */
const checkinNote = { text: "", timer: null };

async function renderCheckin(selector) {
  const box = $(selector);
  if (!box) return;
  let blocks = [];
  try { blocks = (await api("/api/assistant/plan/checkin")).blocks; } catch { /* leave it empty */ }
  if (!blocks.length && !checkinNote.text) { box.innerHTML = ""; return; }
  const today = state.today.today;
  const note = checkinNote.text ? `<p class="note">${esc(checkinNote.text)}</p>` : "";
  box.innerHTML = `<article class="card checkin-card">${blocks.length ? `<div class="agenda-head"><h3>How did these go?</h3><span>${blocks.length} to check in</span></div>
    <ul class="checkin-list">${blocks.map(block => `<li data-block="${esc(block.id)}">
      <div><b>${esc(cleanTitle(block.title))}</b><small>${block.block_date === today ? "Today" : esc(fmtShort(block.block_date))} · ${esc(block.start_label)}–${esc(block.end_label)}</small></div>
      <button type="button" class="btn btn-ghost small" data-mark="missed">Missed</button><button type="button" class="btn btn-primary small" data-mark="done">Done</button></li>`).join("")}</ul>
    <p class="faint">Missed work is rescheduled automatically. Anything you don't mark counts as done.</p>` : ""}${note}</article>`;
  box.querySelectorAll("[data-mark]").forEach(button => button.onclick = async () => {
    button.closest("li").querySelectorAll("button").forEach(b => { b.disabled = true; });
    const result = await markBlock(button.closest("li").dataset.block, button.dataset.mark);
    if (!result) return;
    clearTimeout(checkinNote.timer);
    checkinNote.text = result.message || "";
    checkinNote.timer = setTimeout(() => { checkinNote.text = ""; renderCheckin("#checkin-today"); renderCheckin("#checkin-week"); }, 20000);
    await renderCheckin(selector);
  });
}
window.renderCheckin = renderCheckin;

/* A suggestion to move one of your personal Outlook events so schoolwork or a to-do fits. */
function openMoveSheet(item, date) {
  const b = item.block;
  const from = `${fmtShort(b.from_date)}, ${clockLabel(b.from_start)} – ${clockLabel(b.from_end)}`;
  const to = `${fmtShort(date)}, ${clockLabel(item.start)} – ${clockLabel(item.end)}`;
  sheet(`<p class="kicker">Suggested move · Outlook</p><h2>${esc(cleanTitle(item.title))}</h2>
    <p class="muted"><b>From</b> ${esc(from)}<br><b>To</b> ${esc(to)}</p>
    <p class="faint">Makes room for “${esc(cleanTitle(b.for_title || "your work"))}” before it's due. Accepting changes the event in your Outlook Calendar; ignoring it changes nothing.</p>
    <p class="note error" id="move-error" hidden></p>
    <button class="btn btn-primary full" type="button" id="move-accept">Move it</button>`);
  $("#move-accept").onclick = async () => {
    try { await acceptBlocks([b], $("#move-accept")); closeSheet(); }
    catch (error) { $("#move-error").textContent = error.message; $("#move-error").hidden = false; }
  };
}

/* Edit or delete one of your personal Outlook events (writes to Outlook right away). */
function openOutlookForm(item, date) {
  const notes = [item.series ? "Part of a repeating series: changes apply to this one only. Change the whole series in Outlook." : "",
    item.organizer === false ? "You were invited to this. Changing it only changes your copy; the organizer isn't told." : ""].filter(Boolean);
  sheet(`<p class="kicker">Outlook · ${esc(fmtShort(date))}</p>
    <form id="ol-form" class="stack">
      <label>Title<input id="ol-title" maxlength="200" required value="${esc(item.title)}" autocomplete="off"></label>
      <label>Date<input id="ol-date" type="date" required value="${esc(date)}"></label>
      ${item.all_day ? `<p class="faint">All-day event.</p>` : `<div class="form-grid"><label>Starts<input id="ol-start" type="time" step="300" value="${hhmm(item.start)}"></label><label>Ends<input id="ol-end" type="time" step="300" value="${hhmm(item.end)}"></label></div>`}
      <label>Location <span>optional</span><input id="ol-location" maxlength="120" value="${esc(item.location || "")}"></label>
      <label class="toggle-row"><input id="ol-busy" type="checkbox" ${item.busy !== false ? "checked" : ""}><span><b>${item.all_day ? "Block the whole day" : "Busy"}</b><small>Sets “Show as” in Outlook. ${item.all_day ? "Busy keeps plans off this day." : "The planner keeps this time free."}</small></span></label>
      ${item.directions ? `<a class="link-btn" href="${esc(directionsUrl(item.directions))}" target="_blank" rel="noopener">Directions ↗</a>` : ""}
      ${notes.map(note => `<p class="faint">${esc(note)}</p>`).join("")}
      <p class="note error" id="ol-error" hidden></p>
      <div class="sheet-actions"><button class="btn btn-danger" type="button" id="ol-delete">Delete</button><button class="btn btn-primary" type="submit">Save to Outlook</button></div>
    </form>`);
  const url = `/api/assistant/outlook/events/${encodeURIComponent(item.outlook_id)}`;
  const fail = error => { $("#ol-error").textContent = error.message; $("#ol-error").hidden = false; };
  $("#ol-form").onsubmit = async submit => {
    submit.preventDefault();
    const body = { title: $("#ol-title").value.trim(), location: $("#ol-location").value.trim(), busy: $("#ol-busy").checked };
    if ($("#ol-date").value !== date) body.date = $("#ol-date").value;
    if (!item.all_day) {
      const start = fromHHMM($("#ol-start").value); let end = fromHHMM($("#ol-end").value);
      if (end === 23 * 60 + 59) end = 24 * 60;
      if (start !== item.start || end !== item.end || body.date) Object.assign(body, { start, end, date: body.date || date });
    }
    try { await api(url, { method: "PATCH", body: JSON.stringify(body) }); closeSheet(); cal.day = body.date || cal.day; await window.renderWeek(); }
    catch (error) { fail(error); }
  };
  $("#ol-delete").onclick = async () => {
    if (!confirm(`Delete “${item.title}” from your Outlook Calendar${item.series ? " (this occurrence only)" : ""}? This can't be undone from Compass.`)) return;
    try { await api(url, { method: "DELETE" }); closeSheet(); await window.renderWeek(); }
    catch (error) { fail(error); }
  };
}

function sheet(html) {
  const dialog = $("#cal-sheet");
  $("#cal-sheet-body").innerHTML = `<button class="sheet-close" type="button" aria-label="Close">${icon("close")}</button>${html}`;
  dialog.querySelector(".sheet-close").onclick = closeSheet;
  openDialog(dialog);
}
function closeSheet() { $("#cal-sheet").close(); }

/* ---------------------------------------------------------------- add / edit forms */

function openAddSheet(defaults = {}) {
  const date = defaults.date || cal.day || state.today.today;
  const start = defaults.start ?? Math.min(23 * 60, Math.ceil((new Date().getHours() * 60 + new Date().getMinutes()) / 30) * 30 + 30);
  openEventForm({ title: "", start_date: date, start, end: defaults.end ?? Math.min(start + 60, 24 * 60 - 1), all_day: false, repeat: "none", busy: true, location: "", notes: "" }, null, true);
}
window.openAddSheet = openAddSheet;

function openEventForm(event, occurrence, isNew = false) {
  const repeating = !isNew && event.repeat !== "none";
  const date = occurrence || event.start_date;
  sheet(`${isNew ? `<div class="segmented small sheet-switch"><button type="button" class="active">Event</button><button type="button" id="switch-todo">To-do</button></div>` : ""}
    <p class="kicker">${isNew ? "New event" : repeating ? `Repeats · ${esc(REPEAT_LABEL[event.repeat].toLowerCase())}` : "Your event"}</p>
    <form id="event-form" class="stack">
      <label>Title<input id="ev-title" maxlength="120" required value="${esc(event.title)}" placeholder="Dentist, dinner with Sam…" autocomplete="off"></label>
      <label>Date<input id="ev-date" type="date" required value="${esc(date)}"></label>
      <label class="toggle-row"><input id="ev-allday" type="checkbox" ${event.all_day ? "checked" : ""}><span><b>All day</b></span></label>
      <div class="form-grid" id="ev-times" ${event.all_day ? "hidden" : ""}><label>Starts<input id="ev-start" type="time" step="300" value="${hhmm(event.start)}"></label><label>Ends<input id="ev-end" type="time" step="300" value="${hhmm(event.end)}"></label></div>
      <div class="form-grid"><label>Repeat<select id="ev-repeat">${Object.entries(REPEAT_LABEL).map(([value, label]) => `<option value="${value}" ${event.repeat === value ? "selected" : ""}>${label}</option>`).join("")}</select></label>
        <label id="ev-until-label" ${event.repeat === "none" ? "hidden" : ""}>Until <span>optional</span><input id="ev-until" type="date" value="${esc(event.repeat_until || "")}"></label></div>
      <label>Location <span>optional</span><input id="ev-location" maxlength="120" value="${esc(event.location || "")}"></label>
      <label>Notes <span>optional</span><textarea id="ev-notes" rows="2" maxlength="1000">${esc(event.notes || "")}</textarea></label>
      <label class="toggle-row"><input id="ev-busy" type="checkbox" ${event.busy !== false && !(isNew && event.all_day) ? "checked" : ""}><span><b id="ev-busy-label">${event.all_day ? "Block the whole day" : "Busy"}</b><small>The planner keeps this time free. Turn off for reminders and things you can work around.</small></span></label>
      ${repeating ? `<label>Apply changes to<select id="ev-scope"><option value="one">Only this one (${esc(fmtShort(date))})</option><option value="future">This and following</option><option value="all">Every event</option></select></label>` : ""}
      <p class="note error" id="ev-error" hidden></p>
      <div class="sheet-actions">${isNew ? "" : `<button class="btn btn-danger" type="button" id="ev-delete">Delete</button>`}<button class="btn btn-primary" type="submit">${isNew ? "Add event" : "Save"}</button></div>
      <p class="faint">Goes into your Outlook Calendar when it's connected.</p>
    </form>`);
  $("#ev-allday").onchange = event => {
    $("#ev-times").hidden = event.target.checked;
    $("#ev-busy-label").textContent = event.target.checked ? "Block the whole day" : "Busy";
    if (isNew) $("#ev-busy").checked = !event.target.checked;  // a new all-day event starts as a label
  };
  $("#ev-repeat").onchange = event => { $("#ev-until-label").hidden = event.target.value === "none"; };
  const toTodo = $("#switch-todo");
  if (toTodo) toTodo.onclick = () => openTodoForm({ title: $("#ev-title").value, due_date: null });
  $("#event-form").onsubmit = async submit => {
    submit.preventDefault();
    const body = { title: $("#ev-title").value.trim(), start_date: $("#ev-date").value, all_day: $("#ev-allday").checked, repeat: $("#ev-repeat").value,
      repeat_until: $("#ev-repeat").value === "none" ? null : $("#ev-until").value || null, location: $("#ev-location").value.trim(), notes: $("#ev-notes").value.trim(), busy: $("#ev-busy").checked };
    if (!body.all_day) Object.assign(body, { start: fromHHMM($("#ev-start").value), end: fromHHMM($("#ev-end").value) });
    if (!body.all_day && body.end === 23 * 60 + 59) body.end = 24 * 60;
    try {
      if (isNew) await api("/api/assistant/events", { method: "POST", body: JSON.stringify(body) });
      else {
        const scope = repeating ? $("#ev-scope").value : "all";
        if (repeating && scope === "all" && body.start_date === date) delete body.start_date;  // keep the series' first date
        await api(`/api/assistant/events/${encodeURIComponent(event.id)}`, { method: "PATCH", body: JSON.stringify({ ...body, scope, occurrence: date }) });
      }
      closeSheet(); cal.day = body.start_date || cal.day; await window.renderWeek();
    } catch (error) { $("#ev-error").textContent = error.message; $("#ev-error").hidden = false; }
  };
  const del = $("#ev-delete");
  if (del) del.onclick = async () => {
    const scope = repeating ? $("#ev-scope").value : "all";
    const what = !repeating ? "this event" : scope === "one" ? `the ${fmtShort(date)} event` : scope === "future" ? "this and every following event" : "every event in this series";
    if (!confirm(`Delete ${what}? This can't be undone.`)) return;
    await api(`/api/assistant/events/${encodeURIComponent(event.id)}?scope=${scope}&occurrence=${date}`, { method: "DELETE" });
    closeSheet(); await window.renderWeek();
  };
  setTimeout(() => { if (isNew) $("#ev-title")?.focus(); }, 50);
}

function openTodoForm(todo = {}) {
  const isNew = !todo.id;
  const presets = [15, 30, 45, 60, 90, 120];
  const minutes = todo.minutes || 30;
  sheet(`${isNew ? `<div class="segmented small sheet-switch"><button type="button" id="switch-event">Event</button><button type="button" class="active">To-do</button></div>` : ""}
    <p class="kicker">${isNew ? "New to-do" : todo.planned_minutes ? `${todo.planned_minutes} min planned` : "Not planned yet"}</p>
    <form id="todo-form" class="stack">
      <label>To-do<input id="td-title" maxlength="160" required value="${esc(todo.title || "")}" placeholder="Renew license, email advisor…" autocomplete="off"></label>
      <label>How long? <span id="td-minutes-label">${minutes} min</span></label>
      <div class="chip-row wrap" id="td-presets">${presets.map(p => `<button type="button" class="chip${p === minutes ? " active" : ""}" data-min="${p}">${p < 60 ? `${p} min` : `${p / 60} h`.replace(".5 h", "½ h")}</button>`).join("")}</div>
      <input id="td-minutes" type="number" inputmode="numeric" min="5" max="720" step="5" value="${minutes}" aria-label="Minutes">
      <div class="form-grid"><label>Due <span>optional</span><input id="td-due" type="date" value="${esc(todo.due_date || "")}"></label><label>By <span>optional</span><input id="td-time" type="time" value="${esc(todo.due_time || "")}"></label></div>
      <label>Notes <span>optional</span><textarea id="td-notes" rows="2" maxlength="1000">${esc(todo.notes || "")}</textarea></label>
      <p class="note error" id="td-error" hidden></p>
      <div class="sheet-actions">${isNew ? "" : `<button class="btn btn-danger" type="button" id="td-delete">Delete</button>`}<button class="btn btn-primary" type="submit">${isNew ? "Add to-do" : "Save"}</button></div>
      <p class="faint">The plan finds time for it${todo.due_date ? " before it's due" : ""}; accept the suggestion to put it on your calendar.</p>
    </form>`);
  const setMinutes = value => { $("#td-minutes").value = value; $("#td-minutes-label").textContent = `${value} min`; $$("#td-presets .chip").forEach(c => c.classList.toggle("active", Number(c.dataset.min) === Number(value))); };
  $$("#td-presets .chip").forEach(button => button.onclick = () => setMinutes(button.dataset.min));
  $("#td-minutes").oninput = event => setMinutes(event.target.value);
  const toEvent = $("#switch-event");
  if (toEvent) toEvent.onclick = () => openAddSheet();
  $("#todo-form").onsubmit = async submit => {
    submit.preventDefault();
    const body = { title: $("#td-title").value.trim(), minutes: Number($("#td-minutes").value), due_date: $("#td-due").value || null, due_time: $("#td-time").value || null, notes: $("#td-notes").value.trim() };
    try {
      await api(isNew ? "/api/assistant/todos" : `/api/assistant/todos/${encodeURIComponent(todo.id)}`, { method: isNew ? "POST" : "PATCH", body: JSON.stringify(body) });
      closeSheet(); await window.renderWeek();
    } catch (error) { $("#td-error").textContent = error.message; $("#td-error").hidden = false; }
  };
  const del = $("#td-delete");
  if (del) del.onclick = async () => {
    if (!confirm(`Delete “${todo.title}”?`)) return;
    await api(`/api/assistant/todos/${encodeURIComponent(todo.id)}`, { method: "DELETE" });
    closeSheet(); await window.renderWeek();
  };
  setTimeout(() => { if (isNew) $("#td-title")?.focus(); }, 50);
}

/* ---------------------------------------------------------------- to-do list */

async function renderTodos() {
  const { todos } = await api("/api/assistant/todos");
  const today = state.today.today;
  $("#todo-hint").hidden = todos.length > 0;
  $("#todo-list").innerHTML = todos.length ? todos.map(todo => {
    const due = todo.due_date ? `due ${todo.due_date === today ? "today" : todo.due_date === addDays(today, 1) ? "tomorrow" : fmtShort(todo.due_date)}${todo.due_time ? ` ${clockLabel(fromHHMM(todo.due_time))}` : ""}` : "no deadline";
    const late = todo.due_date && todo.due_date < today;
    return `<div class="row-item todo-row"><button type="button" class="todo-check" data-todo-done="${esc(todo.id)}" aria-label="Mark done"></button>
      <button type="button" class="grow todo-open" data-todo="${esc(todo.id)}"><b>${esc(todo.title)}</b><small class="${late ? "error" : ""}">~${todo.minutes} min · ${esc(due)}${todo.planned_minutes ? ` · ${todo.planned_minutes} min planned` : ""}</small></button></div>`;
  }).join("") : `<div class="empty"><b>Nothing to do</b>Add errands, applications, chores: anything that takes time.</div>`;
  $$("[data-todo-done]").forEach(button => button.onclick = async () => {
    button.classList.add("checked");
    await api(`/api/assistant/todos/${encodeURIComponent(button.dataset.todoDone)}`, { method: "PATCH", body: JSON.stringify({ done: true }) });
    await window.renderWeek();
  });
  $$("[data-todo]").forEach(button => button.onclick = () => openTodoForm(todos.find(todo => todo.id === button.dataset.todo)));
}
window.renderTodos = renderTodos;

$$("#cal-mode [data-mode]").forEach(button => button.onclick = () => setMode(button.dataset.mode));
$("#cal-add").onclick = () => openAddSheet();
$("#todo-add").onclick = () => openTodoForm();
