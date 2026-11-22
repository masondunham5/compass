"""HTTP routes for the health features (load, illness, sleep, trends, insights, body,
pain, records, goals, habits, reminders, reports). Mounted by app.py."""

from __future__ import annotations

import html
import json
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import health
from core import connection

router = APIRouter(prefix="/api/health")


def _today() -> date:
    return date.today()


@router.get("/hub")
def hub() -> dict[str, Any]:
    """Everything the Recovery screen's health sections need, in one call."""
    with connection() as db:
        return {
            "alerts": health.alerts(db), "load": health.training_load(db), "illness": health.illness_signal(db),
            "sleep": health.sleep_coach(db), "body": health.body_summary(db), "pain": health.active_pain(db),
            "pain_areas": list(health.PAIN_AREAS), "tags": health.LIFESTYLE_TAGS,
        }


@router.get("/alerts")
def alerts() -> dict[str, Any]:
    with connection() as db:
        return {"alerts": health.alerts(db)}


@router.get("/trends")
def trends(days: int = 180) -> dict[str, Any]:
    with connection() as db:
        return health.trends(db, max(28, min(days, 730)))


@router.get("/insights")
def insights() -> dict[str, Any]:
    with connection() as db:
        return health.insights(db)


# ---------------------------------------------------------------- body

class Measurement(BaseModel):
    measured_on: str | None = None
    weight_lbs: float | None = Field(default=None, ge=50, le=700)
    waist_in: float | None = Field(default=None, ge=15, le=80)
    body_fat_pct: float | None = Field(default=None, ge=2, le=70)
    notes: str = Field(default="", max_length=300)


@router.post("/body")
def add_measurement(payload: Measurement) -> dict[str, Any]:
    if payload.weight_lbs is None and payload.waist_in is None and payload.body_fat_pct is None:
        raise HTTPException(status_code=400, detail="Enter at least one measurement.")
    day = payload.measured_on or _today().isoformat()
    with connection() as db:
        db.execute("INSERT INTO body_measurements (measured_on, weight_lbs, waist_in, body_fat_pct, notes) VALUES (?, ?, ?, ?, ?)",
                   (day, payload.weight_lbs, payload.waist_in, payload.body_fat_pct, payload.notes))
        return health.body_summary(db)


# ---------------------------------------------------------------- pain

class PainEntry(BaseModel):
    area: str
    severity: int = Field(ge=1, le=10)
    notes: str = Field(default="", max_length=300)


@router.post("/pain")
def log_pain(payload: PainEntry) -> dict[str, Any]:
    if payload.area not in health.PAIN_AREAS:
        raise HTTPException(status_code=400, detail="Pick an area from the list.")
    with connection() as db:
        existing = db.execute("SELECT id FROM pain_log WHERE area = ? AND resolved_on IS NULL", (payload.area,)).fetchone()
        if existing:  # update the open entry instead of stacking duplicates
            db.execute("UPDATE pain_log SET severity = ?, notes = ?, logged_on = ? WHERE id = ?", (payload.severity, payload.notes, _today().isoformat(), existing[0]))
        else:
            db.execute("INSERT INTO pain_log (logged_on, area, severity, notes) VALUES (?, ?, ?, ?)", (_today().isoformat(), payload.area, payload.severity, payload.notes))
        return {"pain": health.active_pain(db)}


@router.post("/pain/{pain_id}/resolve")
def resolve_pain(pain_id: int) -> dict[str, Any]:
    with connection() as db:
        if not db.execute("UPDATE pain_log SET resolved_on = ? WHERE id = ? AND resolved_on IS NULL", (_today().isoformat(), pain_id)).rowcount:
            raise HTTPException(status_code=404, detail="That pain entry is already resolved or doesn't exist.")
        return {"pain": health.active_pain(db)}


# ---------------------------------------------------------------- records

RECORD_KINDS = {"blood_pressure", "lab", "supplement", "medication", "visit"}


class RecordEntry(BaseModel):
    kind: str
    name: str = Field(min_length=1, max_length=120)
    value: str = Field(default="", max_length=60)
    unit: str = Field(default="", max_length=30)
    reference: str = Field(default="", max_length=60)
    notes: str = Field(default="", max_length=1000)
    recorded_on: str | None = None


@router.get("/records")
def list_records() -> dict[str, Any]:
    with connection() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM health_records ORDER BY recorded_on DESC, id DESC")]
    return {"records": rows, "kinds": sorted(RECORD_KINDS)}


@router.post("/records")
def add_record(payload: RecordEntry) -> dict[str, Any]:
    if payload.kind not in RECORD_KINDS:
        raise HTTPException(status_code=400, detail="Unknown record type.")
    if payload.kind == "blood_pressure" and "/" not in payload.value:
        raise HTTPException(status_code=400, detail="Enter blood pressure as systolic/diastolic, e.g. 120/80.")
    with connection() as db:
        db.execute("INSERT INTO health_records (recorded_on, kind, name, value, unit, reference, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (payload.recorded_on or _today().isoformat(), payload.kind, payload.name.strip(), payload.value.strip(), payload.unit.strip(), payload.reference.strip(), payload.notes))
    return list_records()


@router.post("/records/{record_id}/toggle")
def toggle_record(record_id: int) -> dict[str, Any]:
    """Mark a supplement or medication as stopped (or active again)."""
    with connection() as db:
        if not db.execute("UPDATE health_records SET active = 1 - active WHERE id = ?", (record_id,)).rowcount:
            raise HTTPException(status_code=404, detail="Record not found.")
    return list_records()


@router.delete("/records/{record_id}")
def delete_record(record_id: int) -> dict[str, Any]:
    with connection() as db:
        if not db.execute("DELETE FROM health_records WHERE id = ?", (record_id,)).rowcount:
            raise HTTPException(status_code=404, detail="Record not found.")
    return list_records()


# ---------------------------------------------------------------- goals and habits

GOAL_KINDS = {"lift", "bodyweight", "weekly_miles", "vo2_max"}


class GoalEntry(BaseModel):
    kind: str
    title: str = Field(min_length=2, max_length=80)
    target: float = Field(gt=0, le=2000)
    exercise: str | None = Field(default=None, max_length=80)
    deadline: str | None = None


@router.get("/goals")
def list_goals() -> dict[str, Any]:
    with connection() as db:
        goals = [health.goal_progress(db, dict(row)) for row in db.execute("SELECT * FROM goals ORDER BY achieved_on IS NOT NULL, id")]
        for goal in goals:
            if goal["achieved"] and not goal["achieved_on"]:
                db.execute("UPDATE goals SET achieved_on = ? WHERE id = ?", (_today().isoformat(), goal["id"]))
                goal["achieved_on"] = _today().isoformat()
    return {"goals": goals}


@router.post("/goals")
def add_goal(payload: GoalEntry) -> dict[str, Any]:
    if payload.kind not in GOAL_KINDS:
        raise HTTPException(status_code=400, detail="Unknown goal type.")
    if payload.kind == "lift" and not payload.exercise:
        raise HTTPException(status_code=400, detail="Pick the lift for a strength goal.")
    with connection() as db:
        start = health.goal_progress(db, {"kind": payload.kind, "exercise": payload.exercise, "target": payload.target, "start_value": None})["current"]
        db.execute("INSERT INTO goals (kind, title, exercise, target, start_value, deadline, created_on) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (payload.kind, payload.title, payload.exercise, payload.target, start, payload.deadline, _today().isoformat()))
    return list_goals()


@router.delete("/goals/{goal_id}")
def delete_goal(goal_id: int) -> dict[str, Any]:
    with connection() as db:
        if not db.execute("DELETE FROM goals WHERE id = ?", (goal_id,)).rowcount:
            raise HTTPException(status_code=404, detail="Goal not found.")
    return list_goals()


class HabitEntry(BaseModel):
    name: str = Field(min_length=2, max_length=60)


@router.get("/habits")
def list_habits() -> dict[str, Any]:
    with connection() as db:
        return {"habits": health.habit_status(db)}


@router.post("/habits")
def add_habit(payload: HabitEntry) -> dict[str, Any]:
    with connection() as db:
        db.execute("INSERT INTO habits (name, created_on) VALUES (?, ?)", (payload.name.strip(), _today().isoformat()))
    return list_habits()


@router.post("/habits/{habit_id}/toggle")
def toggle_habit(habit_id: int) -> dict[str, Any]:
    """Check or uncheck a manual habit for today (automatic habits come from your data)."""
    today = _today().isoformat()
    with connection() as db:
        habit = db.execute("SELECT auto_key FROM habits WHERE id = ? AND active = 1", (habit_id,)).fetchone()
        if not habit:
            raise HTTPException(status_code=404, detail="Habit not found.")
        if habit[0]:
            raise HTTPException(status_code=400, detail="This habit is tracked automatically from your data.")
        if not db.execute("DELETE FROM habit_checks WHERE habit_id = ? AND checked_on = ?", (habit_id, today)).rowcount:
            db.execute("INSERT INTO habit_checks (habit_id, checked_on) VALUES (?, ?)", (habit_id, today))
    return list_habits()


@router.delete("/habits/{habit_id}")
def archive_habit(habit_id: int) -> dict[str, Any]:
    with connection() as db:
        if not db.execute("UPDATE habits SET active = 0 WHERE id = ?", (habit_id,)).rowcount:
            raise HTTPException(status_code=404, detail="Habit not found.")
    return list_habits()


# ---------------------------------------------------------------- reminders

class Reminders(BaseModel):
    checkin: str | None = Field(default="08:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    workout: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    bedtime: bool = True
    enabled: bool = False


@router.get("/reminders")
def get_reminders() -> dict[str, Any]:
    with connection() as db:
        row = db.execute("SELECT value FROM settings WHERE key = 'reminders'").fetchone()
        sleep = health.sleep_coach(db)
    saved = Reminders(**json.loads(row[0])) if row else Reminders()
    return {**saved.model_dump(), "bedtime_window": sleep.get("tonight")}


@router.put("/reminders")
def save_reminders(payload: Reminders) -> dict[str, Any]:
    with connection() as db:
        db.execute("INSERT INTO settings (key, value) VALUES ('reminders', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (payload.model_dump_json(),))
    return get_reminders()


# ---------------------------------------------------------------- printable reports

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>
body{{font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;color:#1a1a1a;max-width:760px;margin:24px auto;padding:0 20px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:15px;margin:22px 0 8px;border-bottom:1px solid #ddd;padding-bottom:4px}}
table{{width:100%;border-collapse:collapse}} td,th{{text-align:left;padding:4px 6px;border-bottom:1px solid #eee;font-size:13px}}
.muted{{color:#666}} .grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}} .grid div{{border:1px solid #ddd;border-radius:8px;padding:8px}}
.grid b{{display:block;font-size:18px}} button{{margin-top:16px}} @media print{{button{{display:none}}}}
</style></head><body>{body}<button onclick="window.print()">Print / save as PDF</button></body></html>"""


def _cell(value: Any) -> str:
    return html.escape("—" if value in (None, "") else str(value))


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return '<p class="muted">None recorded.</p>'
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{_cell(c)}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


@router.get("/report/doctor", response_class=HTMLResponse)
def doctor_summary() -> str:
    """One page for an appointment: vitals trends, BP, labs, medications/supplements, pain, notes."""
    today = _today()
    with connection() as db:
        records = [dict(row) for row in db.execute("SELECT * FROM health_records ORDER BY recorded_on DESC")]
        trend = health.trends(db, 90)
        pains = health.active_pain(db)
        body = health.body_summary(db)
    weeks = trend["weeks"]
    avg = lambda key: round(sum(w[key] for w in weeks if w[key] is not None) / max(1, sum(w[key] is not None for w in weeks)), 1) if any(w[key] is not None for w in weeks) else None
    by_kind = lambda kind: [r for r in records if r["kind"] == kind]
    body_html = f"""<h1>Health summary</h1><p class="muted">Prepared {today.strftime('%B %d, %Y')} from Health Hub (Garmin data and self-logged records). Not a diagnosis.</p>
<h2>Last 90 days (weekly averages)</h2><div class="grid"><div>Resting HR<b>{_cell(avg('resting_hr'))} bpm</b></div><div>HRV<b>{_cell(avg('hrv'))} ms</b></div>
<div>Sleep<b>{_cell(avg('sleep_hours'))} h</b></div><div>Weight<b>{_cell(body['weight_7day_avg'] or body['latest_weight'])} lb</b></div></div>
<h2>Blood pressure</h2>{_table(['Date', 'Reading', 'Notes'], [[r['recorded_on'], r['value'], r['notes']] for r in by_kind('blood_pressure')])}
<h2>Lab results</h2>{_table(['Date', 'Test', 'Result', 'Reference', 'Notes'], [[r['recorded_on'], r['name'], f"{r['value']} {r['unit']}".strip(), r['reference'], r['notes']] for r in by_kind('lab')])}
<h2>Medications</h2>{_table(['Name', 'Dose', 'Since', 'Status'], [[r['name'], f"{r['value']} {r['unit']}".strip(), r['recorded_on'], 'active' if r['active'] else 'stopped'] for r in by_kind('medication')])}
<h2>Supplements</h2>{_table(['Name', 'Dose', 'Since', 'Status'], [[r['name'], f"{r['value']} {r['unit']}".strip(), r['recorded_on'], 'active' if r['active'] else 'stopped'] for r in by_kind('supplement')])}
<h2>Current pain or injuries</h2>{_table(['Area', 'Severity', 'Since', 'Notes'], [[p['area'], f"{p['severity']}/10", p['logged_on'], p['notes']] for p in pains])}
<h2>Visit notes</h2>{_table(['Date', 'Visit', 'Notes'], [[r['recorded_on'], r['name'], r['notes']] for r in by_kind('visit')])}"""
    return PAGE.format(title="Health summary", body=body_html)


@router.get("/report/monthly", response_class=HTMLResponse)
def monthly_report(month: str | None = None) -> str:
    """Printable month summary: training, running, recovery, body, records and habits."""
    today = _today()
    try:
        first = date.fromisoformat(f"{month}-01") if month else today.replace(day=1)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Use YYYY-MM for the month.") from error
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    with connection() as db:
        sessions = [dict(row) for row in db.execute("SELECT kind, COUNT(*) AS n FROM sessions WHERE status = 'completed' AND session_date BETWEEN ? AND ? GROUP BY kind", (first.isoformat(), last.isoformat()))]
        sets = db.execute("SELECT COUNT(*), COALESCE(SUM(sl.weight * sl.reps), 0) FROM set_logs sl JOIN sessions s ON s.id = sl.session_id WHERE s.session_date BETWEEN ? AND ?", (first.isoformat(), last.isoformat())).fetchone()
        runs = db.execute("SELECT COUNT(*), COALESCE(SUM(distance_miles), 0), COALESCE(MAX(distance_miles), 0) FROM imported_activities WHERE lower(activity_type) LIKE '%run%' AND activity_date BETWEEN ? AND ?", (first.isoformat(), last.isoformat())).fetchone()
        recovery = db.execute("SELECT AVG(sleep_seconds) / 3600.0, AVG(hrv_ms), AVG(resting_hr), AVG(steps), MAX(vo2_max) FROM garmin_daily_metrics WHERE calendar_date BETWEEN ? AND ?", (first.isoformat(), last.isoformat())).fetchone()
        trend = health.trends(db, (last - first).days + 1, last)
        habits = health.habit_status(db, last, (last - first).days + 1)
        goals = [health.goal_progress(db, dict(row)) for row in db.execute("SELECT * FROM goals")]
    lift_rows = []
    for name in trend["lifts"]:
        values = [w["lifts"].get(name) for w in trend["weeks"] if w["lifts"].get(name)]
        if values:
            lift_rows.append([name, values[0], values[-1], round(values[-1] - values[0], 1)])
    fmt = lambda value, digits=1: "—" if value is None else round(value, digits)
    body_html = f"""<h1>{first.strftime('%B %Y')}</h1><p class="muted">Health Hub monthly report · {first.isoformat()} to {last.isoformat()}</p>
<h2>Training</h2><div class="grid"><div>Sessions<b>{sum(s['n'] for s in sessions)}</b></div><div>Sets logged<b>{sets[0]}</b></div>
<div>Volume<b>{round(sets[1] / 1000)}k lb</b></div><div>Runs<b>{runs[0]} · {round(runs[1], 1)} mi</b></div></div>
<h2>Strength (best estimated 1RM, first vs last week)</h2>{_table(['Lift', 'Start', 'End', 'Change'], lift_rows)}
<h2>Recovery (averages)</h2><div class="grid"><div>Sleep<b>{fmt(recovery[0])} h</b></div><div>HRV<b>{fmt(recovery[1])} ms</b></div>
<div>Resting HR<b>{fmt(recovery[2])} bpm</b></div><div>Steps<b>{fmt(recovery[3], 0)}</b></div></div>
<p class="muted">VO₂ max (best this month): {fmt(recovery[4])} · Longest run: {round(runs[2], 1)} mi</p>
<h2>Habits</h2>{_table(['Habit', 'Days done', 'Current streak'], [[h['name'], sum(1 for done in h['history'] if done), h['streak']] for h in habits])}
<h2>Goals</h2>{_table(['Goal', 'Current', 'Target', 'Progress'], [[g['title'], g['current'], g['target'], f"{g['percent']}%" if g['percent'] is not None else '—'] for g in goals])}"""
    return PAGE.format(title=f"Health Hub · {first.strftime('%B %Y')}", body=body_html)
