/* Rescheduling sheet: move one workout or push the plan back, with a live preview and undo. */

const reschedule = { session: null, mode: "move", toDate: null, days: 1, fromDate: null, request: null, timer: null };

function addDays(iso, days) { const d = asDate(iso); d.setDate(d.getDate() + days); return d.toISOString().slice(0, 10); }

function openReschedule(session = null, mode = session ? "move" : "push") {
  const today = state.today.today;
  Object.assign(reschedule, { session, mode, toDate: null, days: 1, fromDate: session?.date && session.date > today ? session.date : today });
  $("#reschedule-title").textContent = session ? cleanTitle(session.title) : "Push the plan back";
  $("#reschedule-kicker").textContent = session ? `${fmtLong(session.date)} · reschedule` : "Reschedule";
  $("#reschedule-mode").hidden = !session;
  const base = session?.date && session.date >= today ? session.date : today;
  const moveOptions = [["Tomorrow", addDays(base, 1)], ["+2 days", addDays(base, 2)], ["+3 days", addDays(base, 3)], ["Next week", addDays(base, 7)]];
  $("#move-chips").innerHTML = moveOptions.map(([label, iso]) => `<button type="button" class="chip" data-date="${iso}">${label} · ${esc(fmtDow(iso))}</button>`).join("");
  $$("#move-chips .chip").forEach(chip => chip.onclick = () => { reschedule.toDate = chip.dataset.date; $("#move-date").value = chip.dataset.date; renderRescheduleMode(); });
  $("#move-date").value = ""; $("#move-date").min = today;
  $("#push-chips").innerHTML = [1, 2, 3, 7].map(days => `<button type="button" class="chip${days === 1 ? " active" : ""}" data-days="${days}">${days === 7 ? "1 week" : `${days} day${days > 1 ? "s" : ""}`}</button>`).join("");
  $$("#push-chips .chip").forEach(chip => chip.onclick = () => { reschedule.days = Number(chip.dataset.days); renderRescheduleMode(); });
  $("#push-from").value = reschedule.fromDate; $("#push-from").min = today;
  renderRescheduleMode();
  openDialog($("#reschedule-sheet"));
}
window.openReschedule = openReschedule;

function renderRescheduleMode() {
  const mode = reschedule.mode;
  $$("#reschedule-mode [data-mode]").forEach(button => button.classList.toggle("active", button.dataset.mode === mode));
  $("#reschedule-move").hidden = mode !== "move";
  $("#reschedule-push").hidden = mode !== "push";
  $$("#move-chips .chip").forEach(chip => chip.classList.toggle("active", chip.dataset.date === reschedule.toDate));
  $$("#push-chips .chip").forEach(chip => chip.classList.toggle("active", Number(chip.dataset.days) === reschedule.days));
  reschedule.request = mode === "move"
    ? (reschedule.toDate && reschedule.session ? { action: "move", session_id: reschedule.session.id, to_date: reschedule.toDate } : null)
    : { action: "push", from_date: $("#push-from").value || state.today.today, days: reschedule.days, include_runs: $("#push-runs").checked };
  clearTimeout(reschedule.timer);
  reschedule.timer = setTimeout(previewReschedule, 120);
}

async function previewReschedule() {
  const box = $("#reschedule-preview");
  const apply = $("#reschedule-apply");
  if (!reschedule.request) { box.innerHTML = '<p class="muted">Pick a new day.</p>'; apply.disabled = true; return; }
  try {
    const plan = await api("/api/schedule/preview", { method: "POST", body: JSON.stringify(reschedule.request) });
    const list = items => items.map(item => `<li><span>${esc(cleanTitle(item.title || item.id))}</span><span>${item.reason ? esc(item.reason) : `${esc(fmtDow(item.from))} ${asDate(item.from).getDate()} → ${esc(fmtDow(item.to))} ${asDate(item.to).getDate()}`}</span></li>`).join("");
    const shown = plan.changes.slice(0, 12);
    box.innerHTML = (plan.changes.length
      ? `<p class="kicker">${plan.changes.length} session${plan.changes.length === 1 ? "" : "s"} will move</p><ul class="move-list">${list(shown)}</ul>${plan.changes.length > shown.length ? `<p class="faint">…and ${plan.changes.length - shown.length} more, keeping the same spacing.</p>` : ""}`
      : `<p class="muted">Nothing would move.</p>`)
      + (plan.skipped.length ? `<p class="kicker" style="margin-top:8px">Staying put</p><ul class="move-list kept">${list(plan.skipped.slice(0, 6))}</ul>` : "")
      + (plan.conflicts.length ? `<p class="note error">Heads-up: ${plan.conflicts.map(c => `${esc(fmtShort(c.date))} would have ${c.sessions.length} lifting sessions`).join("; ")}. You can still apply, then move one.</p>` : "")
      + (plan.busy_days?.length ? `<p class="note error">Busy on your calendar: ${plan.busy_days.map(d => `${esc(fmtShort(d.date))} (${d.exams.length ? esc(d.exams.join(", ")) : `${d.busy_hours} h of events`})`).join("; ")}.</p>` : "");
    apply.disabled = !plan.changes.length;
    apply.textContent = plan.changes.length ? `Move ${plan.changes.length} session${plan.changes.length === 1 ? "" : "s"}` : "Apply";
  } catch (error) { box.innerHTML = `<p class="note error">${esc(error.message)}</p>`; apply.disabled = true; }
}

function showUndo(text) {
  $("#undo-text").textContent = text;
  $("#undo-bar").hidden = false;
  clearTimeout(showUndo.timer);
  showUndo.timer = setTimeout(() => { $("#undo-bar").hidden = true; }, 10000);
}

$$("#reschedule-mode [data-mode]").forEach(button => button.onclick = () => { reschedule.mode = button.dataset.mode; renderRescheduleMode(); });
$("#move-date").onchange = event => { reschedule.toDate = event.target.value || null; renderRescheduleMode(); };
$("#push-from").onchange = () => renderRescheduleMode();
$("#push-runs").onchange = () => renderRescheduleMode();
$("#reschedule-sheet .sheet-close").onclick = () => $("#reschedule-sheet").close();
$("#reschedule-apply").onclick = async () => {
  const button = $("#reschedule-apply"); button.disabled = true;
  try {
    const result = await api("/api/schedule/apply", { method: "POST", body: JSON.stringify(reschedule.request) });
    $("#reschedule-sheet").close();
    await load();
    showUndo(result.summary);
  } catch (error) { alert(error.message); button.disabled = false; }
};
$("#undo-button").onclick = async () => {
  const result = await api("/api/schedule/undo", { method: "POST" });
  $("#undo-bar").hidden = true;
  await load();
  showUndo(result.summary);
  clearTimeout(showUndo.timer); showUndo.timer = setTimeout(() => { $("#undo-bar").hidden = true; }, 4000);
};
