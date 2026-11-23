"""Compass web app: FastAPI routes and request models.

Logic lives in focused modules — core (database, settings, backups), exercises
(workout lines, progression), planning (training blocks), readiness (rules engine),
activities (file imports, Garmin matching), catalog, and race_plan. Names are
re-exported here so scripts and tests can keep using `app.<name>`.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import core
import health
from activities import (  # noqa: F401  (re-exported)
    activity_id, garmin_activity_kind, match_garmin_activities, normalize_activity, parse_garmin_csv, parse_xml_activity,
)
from core import STATIC, auto_backup, connection, current_bodyweight, get_setting, initialize, monday_on_or_after, row_to_session  # noqa: F401
from exercises import (  # noqa: F401
    canonical_exercise, canonical_workout_line, estimate_1rm, exercise_name_from_session, exercises_for, format_workout_line,
    is_bodyweight, line_target_weight, normalize_exercise, prescription_details, progression_suggestion, workout_lines,
)
from planning import (  # noqa: F401
    ROUTINE_BY_DAYS, ROUTINE_DAYS, BlockDraftRequest, apply_exercise_aliases, build_draft, logged_routine_workouts, phase_lines,
    retemplate_future_strength,
)
from readiness import CheckIn, deterministic_training_recommendation, health_recommendation, recommendation  # noqa: F401


async def _refresh_calendars_forever() -> None:
    """Every 30 min: refresh calendar links that are 3+ hours old, then push the plan to Outlook (if connected)."""
    import asyncio

    from assistant import calendars, outlook

    def refresh() -> None:
        with connection() as db:
            calendars.sync_stale(db)
        outlook.sync_quietly()

    await asyncio.sleep(5)
    while True:
        try:
            await asyncio.to_thread(refresh)
        except Exception:  # noqa: BLE001 — failures are recorded per feed / in Outlook status; never crash the app
            pass
        await asyncio.sleep(30 * 60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    import asyncio

    initialize()
    try:
        auto_backup()
    except (OSError, sqlite3.Error):
        pass  # a failed backup must never stop the app from starting
    refresher = asyncio.create_task(_refresh_calendars_forever())
    yield
    refresher.cancel()


app = FastAPI(
    title="Compass",
    description="A local-first training, recovery, and activity planner.",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

import health_api  # noqa: E402  (routes for the health features)

app.include_router(health_api.router)

from assistant import routes as assistant_routes  # noqa: E402  (calendars, agenda)

app.include_router(assistant_routes.router)



class Completion(BaseModel):
    minutes: int = Field(ge=0, le=360)
    notes: str = ""
    metrics: dict[str, Any] = Field(default_factory=dict)


class RunCompletion(BaseModel):
    distance_miles: float = Field(gt=0, le=100)
    duration_minutes: float = Field(gt=0, le=1440)
    average_hr: int | None = Field(default=None, ge=30, le=260)
    max_hr: int | None = Field(default=None, ge=30, le=260)
    effort: int = Field(ge=1, le=10)
    notes: str = Field(default="", max_length=1000)


class SetLog(BaseModel):
    exercise_index: int = Field(ge=0)
    set_number: int = Field(ge=1, le=12)
    weight: float = Field(ge=0)
    reps: int = Field(ge=0, le=100)
    rpe: float | None = Field(default=None, ge=1, le=10)
    note: str = ""


class SessionUpdate(BaseModel):
    session_date: str | None = None
    title: str | None = Field(default=None, min_length=2, max_length=120)
    minutes: int | None = Field(default=None, ge=10, le=360)
    description: str | None = Field(default=None, max_length=500)
    workout: list[str] | None = None
    status: str | None = None


class SessionCreate(BaseModel):
    session_date: str
    kind: str = Field(default="strength", pattern="^(strength|run|mobility)$")
    title: str = Field(min_length=2, max_length=120)
    minutes: int = Field(default=45, ge=10, le=360)
    description: str = Field(default="", max_length=500)
    workout: list[str] = Field(default_factory=list)


class ExerciseInput(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    sets: int = Field(default=3, ge=1, le=12)
    reps: str = Field(default="8–12", max_length=12)
    weight: float | None = Field(default=None, ge=0, le=2000)
    replace_index: int | None = Field(default=None, ge=0)


class ExerciseUpdate(BaseModel):
    sets: int | None = Field(default=None, ge=1, le=12)
    reps: str | None = Field(default=None, max_length=12)
    weight: float | None = Field(default=None, ge=0, le=2000)
    clear_weight: bool = False




class DailyHealth(BaseModel):
    sleep_hours: float = Field(ge=0, le=24)
    sleep_quality: int = Field(ge=1, le=10)
    energy: int = Field(ge=1, le=10)
    soreness: int = Field(ge=1, le=10)
    stress: int = Field(ge=1, le=10)
    weight_lbs: float | None = Field(default=None, ge=50, le=700)
    protein_goal_met: bool = False
    notes: str = Field(default="", max_length=1000)
    tags: list[str] = Field(default_factory=list, max_length=12)
    protein_g: float | None = Field(default=None, ge=0, le=600)
    water_l: float | None = Field(default=None, ge=0, le=15)



































































































@app.get("/api/overview")
def overview() -> dict[str, Any]:
    today = date.today()
    end = today + timedelta(days=13)
    with connection() as db:
        rows = db.execute(
            "SELECT * FROM sessions WHERE session_date BETWEEN ? AND ? ORDER BY session_date, title",
            (today.isoformat(), end.isoformat()),
        ).fetchall()
        latest_checkin = db.execute("SELECT * FROM checkins ORDER BY id DESC LIMIT 1").fetchone()
    sessions = [row_to_session(row) for row in rows]
    completed = sum(item["status"] == "completed" for item in sessions)
    return {"today": today.isoformat(), "sessions": sessions, "completed": completed, "latest_checkin": dict(latest_checkin) if latest_checkin else None}


@app.get("/api/health/summary")
def health_summary() -> dict[str, Any]:
    today = date.today()
    since = (today - timedelta(days=6)).isoformat()
    with connection() as db:
        rows = db.execute("SELECT * FROM daily_health WHERE checkin_date >= ? ORDER BY checkin_date DESC", (since,)).fetchall()
        all_weights = db.execute("SELECT checkin_date, weight_lbs FROM daily_health WHERE weight_lbs IS NOT NULL ORDER BY checkin_date DESC LIMIT 2").fetchall()
        activities = db.execute("SELECT * FROM imported_activities WHERE activity_date >= ? ORDER BY activity_date DESC", (since,)).fetchall()
    records = [dict(row) for row in rows]
    latest = records[0] if records else None
    average_sleep = round(sum(row["sleep_hours"] for row in records) / len(records), 1) if records else None
    weight = all_weights[0]["weight_lbs"] if all_weights else None
    weight_delta = round(all_weights[0]["weight_lbs"] - all_weights[1]["weight_lbs"], 1) if len(all_weights) > 1 else None
    run_miles = round(sum(row["distance_miles"] or 0 for row in activities if "run" in row["activity_type"].lower()), 1)
    return {
        "latest": latest,
        "records": records,
        "checkins_this_week": len(records),
        "average_sleep": average_sleep,
        "weight_lbs": weight,
        "weight_delta": weight_delta,
        "imported_run_miles": run_miles,
        "recommendation": health_recommendation(latest),
    }


def garmin_status() -> dict[str, Any]:
    """Return connection metadata only; the Personal API key never reaches the UI."""
    configured = bool(os.getenv("GARMIN_PERSONAL_API_KEY", "").strip())
    expires_on = os.getenv("GARMIN_KEY_EXPIRES_ON", "").strip() or None
    with connection() as db:
        latest = db.execute(
            "SELECT started_at, finished_at, status, activities_imported, metrics_updated, message "
            "FROM garmin_sync_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    warning = None
    if expires_on:
        try:
            remaining = (date.fromisoformat(expires_on) - date.today()).days
            if remaining <= 0:
                warning = "Your Garmin key appears to be expired. Create a new key in Garmin Connect."
            elif remaining <= 14:
                warning = f"Your Garmin key expires in {remaining} day{'s' if remaining != 1 else ''}."
        except ValueError:
            warning = "GARMIN_KEY_EXPIRES_ON should use YYYY-MM-DD."
    return {"configured": configured, "expires_on": expires_on, "warning": warning, "last_sync": dict(latest) if latest else None}


@app.get("/api/garmin/status")
def get_garmin_status() -> dict[str, Any]:
    return garmin_status()


@app.get("/api/garmin/dashboard")
def garmin_dashboard(days: int = 7) -> dict[str, Any]:
    days = 30 if days >= 30 else 7
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    with connection() as db:
        rows = db.execute(
            "SELECT * FROM garmin_daily_metrics WHERE calendar_date >= ? ORDER BY calendar_date DESC", (since,)
        ).fetchall()
        activity_rows = db.execute(
            """SELECT activity_date, activity_type, distance_miles, details_json
               FROM imported_activities
               WHERE activity_date >= ? AND lower(activity_type) LIKE '%run%'
               ORDER BY activity_date DESC""",
            ((date.today() - timedelta(days=27)).isoformat(),),
        ).fetchall()
        history_days = db.execute("SELECT COUNT(*) FROM garmin_daily_metrics").fetchone()[0]
    records = [dict(row) for row in rows]
    latest = records[0] if records else None
    average = lambda key: round(sum(row[key] or 0 for row in records if row[key] is not None) / max(1, sum(row[key] is not None for row in records)), 1)
    run_rows = [dict(row) for row in activity_rows]
    recent_cutoff = (date.today() - timedelta(days=6)).isoformat()
    recent_miles = round(sum(float(row["distance_miles"] or 0) for row in run_rows if row["activity_date"] >= recent_cutoff), 1)
    baseline_miles = sum(float(row["distance_miles"] or 0) for row in run_rows) / 4
    endurance_score = None
    if baseline_miles > 0:
        endurance_score = round(max(0, min(100, (recent_miles / baseline_miles) * 100)))
    vo2_values: list[tuple[str, float]] = []
    for row in run_rows:
        try:
            details = json.loads(row["details_json"] or "{}")
        except (TypeError, ValueError):
            details = {}
        value = details.get("vo2_max") if isinstance(details, dict) else None
        if isinstance(value, (int, float)) and value > 0:
            vo2_values.append((row["activity_date"], float(value)))
    # Garmin publishes VO2 max through its metrics service (stored per day by the sync);
    # the per-activity value is only a fallback for older databases.
    with connection() as db:
        vo2_row = db.execute("SELECT calendar_date, vo2_max FROM garmin_daily_metrics WHERE vo2_max IS NOT NULL ORDER BY calendar_date DESC LIMIT 1").fetchone() if "vo2_max" in {r[1] for r in db.execute("PRAGMA table_info(garmin_daily_metrics)")} else None
    vo2_latest = (vo2_row["calendar_date"], float(vo2_row["vo2_max"])) if vo2_row else (vo2_values[0] if vo2_values else None)
    return {
        "status": garmin_status(),
        "days": days,
        "history_days": history_days,
        "latest": latest,
        "records": records,
        "average_sleep_hours": round(average("sleep_seconds") / 3600, 1) if any(row["sleep_seconds"] for row in records) else None,
        "average_steps": round(average("steps")) if any(row["steps"] is not None for row in records) else None,
        "average_hrv": average("hrv_ms") if any(row["hrv_ms"] is not None for row in records) else None,
        "performance": {
            "vo2_max": round(vo2_latest[1], 1) if vo2_latest else None,
            "vo2_max_date": vo2_latest[0] if vo2_latest else None,
            "run_miles_7d": recent_miles,
            "run_miles_baseline": round(baseline_miles, 1) if baseline_miles else None,
            "endurance_score": endurance_score,
            "run_data_days": len({row["activity_date"] for row in run_rows}),
        },
    }


@app.get("/api/garmin/sync-history")
def garmin_sync_history() -> dict[str, Any]:
    with connection() as db:
        rows = db.execute(
            "SELECT started_at, finished_at, status, activities_imported, metrics_updated, message "
            "FROM garmin_sync_log ORDER BY id DESC LIMIT 12"
        ).fetchall()
    return {"syncs": [dict(row) for row in rows]}


@app.get("/api/recommendations/training")
def training_recommendation() -> dict[str, Any]:
    """Return a recommendation only. Schedule changes always remain user actions."""
    today = date.today()
    with connection() as db:
        session_rows = db.execute(
            "SELECT * FROM sessions WHERE session_date BETWEEN ? AND ? ORDER BY session_date, title",
            (today.isoformat(), (today + timedelta(days=7)).isoformat()),
        ).fetchall()
        metric_rows = db.execute(
            "SELECT * FROM garmin_daily_metrics WHERE calendar_date >= ? ORDER BY calendar_date DESC",
            ((today - timedelta(days=28)).isoformat(),),
        ).fetchall()
        manual = db.execute("SELECT * FROM daily_health WHERE checkin_date = ?", (today.isoformat(),)).fetchone()
        # Days (of the previous three) with a completed session or a Garmin activity.
        window = ((today - timedelta(days=3)).isoformat(), (today - timedelta(days=1)).isoformat())
        trained = {row[0] for row in db.execute("SELECT session_date FROM sessions WHERE status = 'completed' AND session_date BETWEEN ? AND ?", window)}
        trained |= {row[0] for row in db.execute("SELECT activity_date FROM imported_activities WHERE activity_date BETWEEN ? AND ?", window)}
        illness = health.illness_signal(db, today)["warning"]
        load_ratio = health.training_load(db, today)["ratio"]
    return deterministic_training_recommendation(
        [row_to_session(row) for row in session_rows], [dict(row) for row in metric_rows], dict(manual) if manual else None,
        recent_training_days=len(trained), illness_warning=illness, load_ratio=load_ratio,
    )




def plan_position(today: date) -> dict[str, Any] | None:
    with connection() as db:
        row = db.execute("SELECT name, plan_json FROM plans WHERE status = 'active' ORDER BY created_at DESC LIMIT 1").fetchone()
    if not row:
        return None
    plan = json.loads(row["plan_json"])
    weeks = plan.get("config", {}).get("weeks") or len(plan.get("phases", []))
    try:
        week = (today - date.fromisoformat(plan["start_date"])).days // 7 + 1
    except (KeyError, ValueError):
        return None
    phase = next((item for item in plan.get("phases", []) if item.get("week") == week), None)
    return {"name": row["name"], "week": week, "weeks": weeks, "phase": phase.get("name") if phase else None, "phase_message": phase.get("message") if phase else None, "in_block": 1 <= week <= weeks}


@app.get("/api/today")
def today_view() -> dict[str, Any]:
    """Everything the Today screen needs in one call. Read-only."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    with connection() as db:
        week_rows = db.execute(
            "SELECT * FROM sessions WHERE session_date BETWEEN ? AND ? ORDER BY session_date, title",
            (monday.isoformat(), (monday + timedelta(days=6)).isoformat()),
        ).fetchall()
        activity_rows = db.execute(
            "SELECT activity_date, activity_type, title, distance_miles, duration_minutes FROM imported_activities WHERE activity_date BETWEEN ? AND ?",
            (monday.isoformat(), (monday + timedelta(days=6)).isoformat()),
        ).fetchall()
        next_row = db.execute(
            "SELECT * FROM sessions WHERE session_date > ? AND status = 'planned' ORDER BY session_date, title LIMIT 1", (today.isoformat(),)
        ).fetchone()
        recovery_row = db.execute(
            "SELECT * FROM garmin_daily_metrics WHERE sleep_seconds IS NOT NULL OR hrv_ms IS NOT NULL ORDER BY calendar_date DESC LIMIT 1"
        ).fetchone()
        manual = db.execute("SELECT * FROM daily_health WHERE checkin_date = ?", (today.isoformat(),)).fetchone()
    week_sessions = [row_to_session(row) for row in week_rows]
    activities = [dict(row) for row in activity_rows]
    days = []
    for offset in range(7):
        day = (monday + timedelta(days=offset)).isoformat()
        sessions = [s for s in week_sessions if s["date"] == day and s["status"] != "cancelled"]
        day_activities = [a for a in activities if a["activity_date"] == day]
        trained = any(s["status"] == "completed" for s in sessions) or bool(day_activities)
        days.append({
            "date": day,
            "state": "done" if trained else ("planned" if sessions else "rest"),
            "sessions": [{**s, "lines": workout_lines(s)} for s in sessions],
            "activities": day_activities,
        })
    recommendation = training_recommendation()
    target = recommendation.get("target_session")
    today_sessions = [s for s in week_sessions if s["date"] == today.isoformat() and s["status"] != "cancelled"]
    focus = next((s for s in today_sessions if target and s["id"] == target["id"]), None) or (today_sessions[0] if today_sessions else None)
    focus_payload = None
    if focus:
        cap = recommendation.get("adjusted_sets") if focus["kind"] == "strength" and target and focus["id"] == target["id"] else None
        focus_payload = {**focus, "lines": workout_lines(focus, cap), "full_lines": workout_lines(focus), "adjusted_sets": cap,
                         "adjusted_minutes": recommendation.get("adjusted_minutes") if target and focus["id"] == target["id"] else None}
    return {
        "today": today.isoformat(),
        "plan": plan_position(today),
        "week": days,
        "focus": focus_payload,
        "other_today": [{**s, "lines": workout_lines(s)} for s in today_sessions if not focus or s["id"] != focus["id"]],
        "recommendation": recommendation,
        "recovery": dict(recovery_row) if recovery_row else None,
        "checked_in_today": manual is not None,
        "up_next": row_to_session(next_row) if next_row else None,
    }


@app.post("/api/garmin/sync")
def sync_garmin_now() -> dict[str, Any]:
    if not os.getenv("GARMIN_PERSONAL_API_KEY", "").strip():
        raise HTTPException(status_code=400, detail="Garmin is not connected yet. Add your key to the local .env file first.")
    try:
        from garmin_sync import GarminSyncError, run_sync
        with connection() as db:
            has_history = db.execute("SELECT 1 FROM garmin_daily_metrics LIMIT 1").fetchone() is not None
        raw_days = os.getenv("GARMIN_SYNC_DAYS", "2")
        try:
            regular_days = max(1, min(int(raw_days), 7))
        except ValueError:
            regular_days = 2
        # First successful connection gets enough context for useful trends.
        result = run_sync(days=regular_days if has_history else 14)
        result["days_synced"] = regular_days if has_history else 14
        result["initial_backfill"] = not has_history
        return result
    except GarminSyncError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/garmin/backfill")
def backfill_garmin_history() -> dict[str, Any]:
    """Fetch a bounded 30-day history only when the user explicitly requests it."""
    if not os.getenv("GARMIN_PERSONAL_API_KEY", "").strip():
        raise HTTPException(status_code=400, detail="Garmin is not connected yet. Add your key to the local .env file first.")
    try:
        from garmin_sync import GarminSyncError, run_sync
        result = run_sync(days=30)
        result["days_synced"] = 30
        result["initial_backfill"] = False
        return result
    except GarminSyncError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/health/checkin")
def save_daily_health(checkin: DailyHealth) -> dict[str, Any]:
    today = date.today().isoformat()
    record = {**checkin.model_dump(), "checkin_date": today}
    with connection() as db:
        target = health.protein_target(db)
        protein_met = checkin.protein_goal_met or bool(checkin.protein_g and target and checkin.protein_g >= target)
        tags = sorted({tag.strip().lower() for tag in checkin.tags if tag.strip()})
        db.execute(
            """INSERT INTO daily_health (checkin_date, sleep_hours, sleep_quality, energy, soreness, stress, weight_lbs, protein_goal_met, notes, updated_at, tags_json, protein_g, water_l)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(checkin_date) DO UPDATE SET sleep_hours=excluded.sleep_hours, sleep_quality=excluded.sleep_quality,
               energy=excluded.energy, soreness=excluded.soreness, stress=excluded.stress, weight_lbs=excluded.weight_lbs,
               protein_goal_met=excluded.protein_goal_met, notes=excluded.notes, updated_at=excluded.updated_at,
               tags_json=excluded.tags_json, protein_g=excluded.protein_g, water_l=excluded.water_l""",
            (today, checkin.sleep_hours, checkin.sleep_quality, checkin.energy, checkin.soreness, checkin.stress, checkin.weight_lbs, int(protein_met),
             checkin.notes, datetime.now().astimezone().isoformat(timespec="seconds"), json.dumps(tags), checkin.protein_g, checkin.water_l),
        )
    return {"status": "saved", "record": record, "recommendation": health_recommendation(record)}


@app.get("/api/activities")
def activities() -> dict[str, Any]:
    with connection() as db:
        rows = db.execute("SELECT * FROM imported_activities ORDER BY activity_date DESC, imported_at DESC LIMIT 60").fetchall()
    # raw_json (full Garmin payload) stays server-side; the list view does not need it.
    return {"activities": [{**{k: row[k] for k in row.keys() if k != "raw_json"}, "details": json.loads(row["details_json"])} for row in rows]}


@app.post("/api/activities/import")
async def import_activity_file(file: UploadFile = File(...)) -> dict[str, Any]:
    extension = Path(file.filename or "").suffix.lower()
    if extension not in {".csv", ".gpx", ".tcx"}:
        raise HTTPException(status_code=400, detail="Use a Garmin CSV export, TCX, or GPX file. FIT import is planned next.")
    content = await file.read()
    if len(content) > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="That export is too large. Import a smaller activity export (under 8 MB).")
    try:
        parsed = parse_garmin_csv(content.decode("utf-8-sig", errors="replace")) if extension == ".csv" else parse_xml_activity(content, extension[1:])
    except (csv.Error, ET.ParseError, UnicodeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="The file could not be read as a Garmin activity export.") from error
    saved = []
    with connection() as db:
        for candidate in parsed:
            activity = normalize_activity(candidate, f"garmin-{extension[1:]}")
            if not activity:
                continue
            cursor = db.execute(
                """INSERT OR IGNORE INTO imported_activities
                   (id, source, activity_date, activity_type, title, distance_miles, duration_minutes, average_hr, max_hr, elevation_feet, details_json, imported_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (activity["id"], f"Garmin {extension[1:].upper()}", activity["activity_date"], activity["activity_type"], activity["title"], activity["distance_miles"], activity["duration_minutes"], activity["average_hr"], activity["max_hr"], activity["elevation_feet"], json.dumps(activity["details"]), datetime.now().astimezone().isoformat(timespec="seconds")),
            )
            if cursor.rowcount:
                saved.append(activity)
    return {"imported": len(saved), "activities": saved}








@app.post("/api/activities/{activity_id}/link")
def link_activity_to_run(activity_id: str) -> dict[str, Any]:
    with connection() as db:
        activity = db.execute("SELECT * FROM imported_activities WHERE id = ?", (activity_id,)).fetchone()
        if not activity:
            raise HTTPException(status_code=404, detail="Imported activity not found.")
        if activity["linked_session_id"]:
            return {"status": "already-linked", "session_id": activity["linked_session_id"]}
        session = db.execute(
            """SELECT * FROM sessions WHERE kind = 'run' AND status = 'planned'
               AND session_date BETWEEN ? AND ? ORDER BY ABS(julianday(session_date) - julianday(?)), session_date LIMIT 1""",
            ((date.fromisoformat(activity["activity_date"]) - timedelta(days=1)).isoformat(), (date.fromisoformat(activity["activity_date"]) + timedelta(days=1)).isoformat(), activity["activity_date"]),
        ).fetchone()
        if not session:
            raise HTTPException(status_code=409, detail="No planned run is within one day of this import. Keep it as history or move a run first.")
        metrics = {"distance_miles": activity["distance_miles"], "duration_minutes": activity["duration_minutes"], "average_hr": activity["average_hr"], "max_hr": activity["max_hr"], "source": activity["source"]}
        if activity["distance_miles"] and activity["duration_minutes"]:
            metrics["pace_seconds_per_mile"] = round(activity["duration_minutes"] * 60 / activity["distance_miles"])
        db.execute("UPDATE sessions SET status = 'completed' WHERE id = ?", (session["id"],))
        db.execute("INSERT INTO logs (session_id, completed_at, minutes, notes, metrics_json) VALUES (?, ?, ?, ?, ?)", (session["id"], datetime.now().astimezone().isoformat(timespec="seconds"), round(activity["duration_minutes"] or session["minutes"]), "Imported from Garmin; linked with your approval.", json.dumps(metrics)))
        db.execute("UPDATE imported_activities SET linked_session_id = ? WHERE id = ?", (session["id"], activity_id))
    return {"status": "linked", "session_id": session["id"], "session_title": session["title"]}


@app.get("/api/plans")
def plans() -> dict[str, Any]:
    with connection() as db:
        rows = db.execute("SELECT id, name, created_at, status, plan_json FROM plans ORDER BY created_at DESC").fetchall()
    result = []
    for row in rows:
        plan = json.loads(row["plan_json"])
        result.append({"id": row["id"], "name": row["name"], "created_at": row["created_at"], "status": row["status"], "goals": plan.get("goals", []), "weeks": plan.get("config", {}).get("weeks"), "phases": plan.get("phases", []), "config": plan.get("config", {})})
    return {"plans": result}


@app.post("/api/plans/draft")
def create_plan_draft(request: BlockDraftRequest) -> dict[str, Any]:
    plan = build_draft(request)
    with connection() as db:
        db.execute("INSERT INTO plans (id, name, created_at, status, plan_json) VALUES (?, ?, ?, 'draft', ?)", (plan["id"], plan["name"], datetime.now().astimezone().isoformat(timespec="seconds"), json.dumps(plan)))
    return plan


@app.post("/api/plans/{plan_id}/revision-preview")
def revision_preview(plan_id: str, request: BlockDraftRequest) -> dict[str, Any]:
    with connection() as db:
        row = db.execute("SELECT id FROM plans WHERE id = ? AND status = 'active'", (plan_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Active plan not found.")
    preview = build_draft(request)
    preview["revision_of"] = plan_id
    return preview


@app.get("/api/plans/{plan_id}")
def get_plan(plan_id: str) -> dict[str, Any]:
    with connection() as db:
        row = db.execute("SELECT plan_json, status FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Plan not found.")
    plan = json.loads(row["plan_json"])
    plan["status"] = row["status"]
    return plan


@app.post("/api/plans/{plan_id}/activate")
def activate_plan(plan_id: str) -> dict[str, Any]:
    with connection() as db:
        row = db.execute("SELECT plan_json FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Plan not found.")
        plan = json.loads(row["plan_json"])
        cutoff = race_run_cutoff(db)
        for session in plan.get("sessions", []):
            if cutoff and session["kind"] == "run" and session["date"] <= cutoff:
                continue  # an applied race plan owns these run days
            db.execute(
                """INSERT OR IGNORE INTO sessions
                   (id, session_date, kind, title, minutes, description, workout_json, status, locked, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session["id"], session["date"], session["kind"], session["title"], session["minutes"], session["description"], json.dumps(session["workout"]), "planned", 0, f"plan:{plan_id}"),
            )
        db.execute("UPDATE plans SET status = 'active' WHERE id = ?", (plan_id,))
    return {"status": "active", "sessions_created": len(plan.get("sessions", []))}


@app.post("/api/plans/{plan_id}/revise")
def revise_plan(plan_id: str, request: BlockDraftRequest) -> dict[str, Any]:
    replacement = build_draft(request)
    today = date.today().isoformat()
    with connection() as db:
        row = db.execute("SELECT plan_json FROM plans WHERE id = ? AND status = 'active'", (plan_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Active plan not found.")
        old_plan = json.loads(row["plan_json"])
        preserved = db.execute(
            """SELECT COUNT(*) FROM sessions s WHERE source = ? AND session_date >= ?
               AND (locked = 1 OR status != 'planned' OR EXISTS (SELECT 1 FROM set_logs sl WHERE sl.session_id = s.id))""",
            (f"plan:{plan_id}", today),
        ).fetchone()[0]
        db.execute(
            """DELETE FROM sessions WHERE source = ? AND session_date >= ? AND locked = 0 AND status = 'planned'
               AND NOT EXISTS (SELECT 1 FROM set_logs sl WHERE sl.session_id = sessions.id)""",
            (f"plan:{plan_id}", today),
        )
        cutoff = race_run_cutoff(db)
        for session in replacement["sessions"]:
            if session["date"] < today or (cutoff and session["kind"] == "run" and session["date"] <= cutoff):
                continue
            db.execute(
                """INSERT OR IGNORE INTO sessions
                   (id, session_date, kind, title, minutes, description, workout_json, status, locked, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'planned', 0, ?)""",
                (session["id"], session["date"], session["kind"], session["title"], session["minutes"], session["description"], json.dumps(session["workout"]), f"plan:{plan_id}"),
            )
        replacement["id"] = plan_id
        replacement["created_at"] = old_plan.get("created_at")
        db.execute("UPDATE plans SET name = ?, plan_json = ? WHERE id = ?", (replacement["name"], json.dumps(replacement), plan_id))
    return {"status": "revised", "preserved_sessions": preserved, "future_sessions": sum(session["date"] >= today for session in replacement["sessions"])}


@app.get("/api/sessions/{session_id}")
def session_detail(session_id: str) -> dict[str, Any]:
    with connection() as db:
        row = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Training session not found.")
        set_rows = db.execute("SELECT * FROM set_logs WHERE session_id = ? ORDER BY exercise_index, set_number", (session_id,)).fetchall()
        history_rows = db.execute(
            """SELECT sl.*, s.workout_json, s.session_date, s.description AS session_description FROM set_logs sl
               JOIN sessions s ON s.id = sl.session_id
               WHERE sl.session_id != ? AND s.session_date <= ? ORDER BY sl.completed_at DESC""", (session_id, row["session_date"])
        ).fetchall()
    session = row_to_session(row)
    previous_rows = []
    for history in history_rows:
        prior = {"workout": json.loads(history["workout_json"])}
        exercise_name = exercise_name_from_session(prior, history["exercise_index"])
        if exercise_name:
            item = dict(history)
            item["exercise_name"] = exercise_name
            previous_rows.append(item)
    session["exercises"] = exercises_for(session, set_rows, previous_rows)
    # Flag exercises that load an area with logged, unresolved pain, and suggest safer swaps.
    with connection() as db:
        pains = health.active_pain(db)
    if pains:
        from catalog import EXERCISES, guess_group
        groups = {name.lower(): group for name, group, _, _ in EXERCISES}
        for exercise in session["exercises"]:
            warning = health.pain_warning(exercise["name"], pains)
            if warning:
                group = groups.get(exercise["name"].lower()) or guess_group(exercise["name"])
                exercise["pain_warning"] = {**warning, "swaps": health.safe_swaps(exercise["name"], group, pains)}
    return session


@app.put("/api/sessions/{session_id}/sets")
def save_set(session_id: str, set_log: SetLog) -> dict[str, Any]:
    with connection() as db:
        row = db.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Training session not found.")
        db.execute(
            """INSERT INTO set_logs (session_id, exercise_index, set_number, weight, reps, rpe, note, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id, exercise_index, set_number) DO UPDATE SET
                  weight=excluded.weight, reps=excluded.reps, rpe=excluded.rpe, note=excluded.note, completed_at=excluded.completed_at""",
            (session_id, set_log.exercise_index, set_log.set_number, set_log.weight, set_log.reps, set_log.rpe, set_log.note, datetime.now().astimezone().isoformat(timespec="seconds")),
        )
    suggestion = "Hold the same load for the next set."
    if set_log.rpe is not None and set_log.rpe <= 7 and set_log.reps >= 8:
        suggestion = "Strong set. Consider +5 lb on a later set only if form stays sharp."
    elif set_log.rpe is not None and set_log.rpe >= 9:
        suggestion = "High effort. Hold or reduce the load slightly for the next set."
    return {"status": "saved", "suggestion": suggestion}


@app.patch("/api/sessions/{session_id}")
def update_session(session_id: str, update: SessionUpdate) -> dict[str, Any]:
    values = update.model_dump(exclude_none=True)
    allowed_statuses = {"planned", "cancelled"}
    if "status" in values and values["status"] not in allowed_statuses:
        raise HTTPException(status_code=400, detail="A workout can be planned or cancelled.")
    if "session_date" in values:
        try:
            date.fromisoformat(values["session_date"])
        except ValueError as error:
            raise HTTPException(status_code=400, detail="Use a valid workout date.") from error
    if "workout" in values:
        cleaned = [line.strip() for line in values["workout"] if line.strip()]
        if not cleaned:
            raise HTTPException(status_code=400, detail="Keep at least one exercise in the workout.")
        values["workout_json"] = json.dumps(cleaned)
        del values["workout"]
    if "session_date" in values:
        values["session_date"] = values.pop("session_date")
    if not values:
        raise HTTPException(status_code=400, detail="No changes were provided.")
    with connection() as db:
        if not db.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Training session not found.")
        fields = ", ".join(f"{column} = ?" for column in values)
        db.execute(f"UPDATE sessions SET {fields} WHERE id = ?", (*values.values(), session_id))
        row = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return row_to_session(row)


@app.post("/api/sessions/{session_id}/log-run")
def log_run(session_id: str, run: RunCompletion) -> dict[str, Any]:
    if run.max_hr is not None and run.average_hr is not None and run.max_hr < run.average_hr:
        raise HTTPException(status_code=400, detail="Maximum heart rate cannot be lower than average heart rate.")
    with connection() as db:
        row = db.execute("SELECT kind FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Training session not found.")
        if row["kind"] != "run":
            raise HTTPException(status_code=400, detail="This is not a running session.")
        metrics = run.model_dump()
        metrics["pace_seconds_per_mile"] = round(run.duration_minutes * 60 / run.distance_miles)
        db.execute("UPDATE sessions SET status = 'completed' WHERE id = ?", (session_id,))
        db.execute(
            "INSERT INTO logs (session_id, completed_at, minutes, notes, metrics_json) VALUES (?, ?, ?, ?, ?)",
            (session_id, datetime.now().astimezone().isoformat(timespec="seconds"), round(run.duration_minutes), run.notes, json.dumps(metrics)),
        )
    return {"status": "completed", "pace_seconds_per_mile": metrics["pace_seconds_per_mile"]}


@app.get("/api/insights")
def insights() -> dict[str, Any]:
    with connection() as db:
        rows = db.execute("SELECT kind, status, COUNT(*) AS count FROM sessions GROUP BY kind, status").fetchall()
        volume = db.execute("SELECT COALESCE(SUM(weight * reps), 0) AS value FROM set_logs").fetchone()["value"]
    return {"sessions": [dict(row) for row in rows], "volume": round(volume, 0)}


@app.get("/api/history")
def workout_history() -> dict[str, Any]:
    with connection() as db:
        rows = db.execute(
            "SELECT * FROM sessions WHERE status = 'completed' ORDER BY session_date DESC, title"
        ).fetchall()
        log_rows = db.execute("SELECT session_id, COUNT(*) AS sets_logged FROM set_logs GROUP BY session_id").fetchall()
        completion_rows = db.execute("SELECT session_id, metrics_json FROM logs ORDER BY id DESC").fetchall()
        garmin_rows = db.execute(
            "SELECT linked_session_id, title, activity_type, duration_minutes, average_hr, distance_miles FROM imported_activities WHERE linked_session_id IS NOT NULL"
        ).fetchall()
    garmin_by_session: dict[str, list[dict[str, Any]]] = {}
    for row in garmin_rows:
        garmin_by_session.setdefault(row["linked_session_id"], []).append({k: row[k] for k in row.keys() if k != "linked_session_id"})
    set_counts = {row["session_id"]: row["sets_logged"] for row in log_rows}
    metrics_by_session: dict[str, dict[str, Any]] = {}
    for row in completion_rows:
        metrics_by_session.setdefault(row["session_id"], json.loads(row["metrics_json"]))
    sessions = []
    for row in rows:
        session = row_to_session(row)
        session["sets_logged"] = set_counts.get(session["id"], 0)
        session["metrics"] = metrics_by_session.get(session["id"], {})
        session["garmin"] = garmin_by_session.get(session["id"], [])
        sessions.append(session)
    return {"sessions": sessions}


@app.get("/api/progress")
def progress() -> dict[str, Any]:
    with connection() as db:
        rows = db.execute(
            """SELECT sl.exercise_index, sl.weight, sl.reps, sl.rpe, sl.completed_at,
                      s.session_date, s.title, s.workout_json
               FROM set_logs sl JOIN sessions s ON s.id = sl.session_id
               ORDER BY s.session_date, sl.completed_at"""
        ).fetchall()
        bodyweight = current_bodyweight(db)
    exercises: dict[str, dict[str, Any]] = {}
    for row in rows:
        workout = json.loads(row["workout_json"])
        if row["exercise_index"] >= len(workout):
            continue
        name = canonical_exercise(workout[row["exercise_index"]].partition("—")[0])
        key = normalize_exercise(name)
        estimated_1rm = estimate_1rm(name, row["weight"], row["reps"], bodyweight)
        point = {"date": row["session_date"], "weight": row["weight"], "reps": row["reps"], "rpe": row["rpe"], "e1rm": estimated_1rm, "session": row["title"]}
        item = exercises.setdefault(key, {"name": name, "history": [], "bodyweight": is_bodyweight(name)})
        item["history"].append(point)
    result = []
    for item in exercises.values():
        history = item["history"]
        best = max(history, key=lambda point: (point["e1rm"], point["weight"], point["reps"]))
        item["best_set"] = best
        item["latest"] = history[-1]
        result.append(item)
    return {"exercises": sorted(result, key=lambda item: item["name"].lower()), "bodyweight": bodyweight}


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    with connection() as db:
        saved = get_setting(db, "bodyweight_lbs")
        return {"bodyweight_lbs": float(saved) if saved else None, "effective_bodyweight": current_bodyweight(db)}


class SettingsUpdate(BaseModel):
    bodyweight_lbs: float | None = Field(default=None, ge=60, le=600)


@app.put("/api/settings")
def update_settings(payload: SettingsUpdate) -> dict[str, Any]:
    with connection() as db:
        if payload.bodyweight_lbs is None:
            db.execute("DELETE FROM settings WHERE key = 'bodyweight_lbs'")
        else:
            db.execute("INSERT INTO settings (key, value) VALUES ('bodyweight_lbs', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(payload.bodyweight_lbs),))
    return get_settings()


def _week_metrics(db: sqlite3.Connection, start: date, end: date) -> dict[str, Any]:
    row = db.execute(
        "SELECT AVG(sleep_seconds) / 3600.0, AVG(hrv_ms), AVG(resting_hr), AVG(sleep_score) FROM garmin_daily_metrics WHERE calendar_date BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    return {key: (round(value, 1) if value is not None else None) for key, value in zip(["sleep_hours", "hrv", "resting_hr", "sleep_score"], row)}


@app.get("/api/review/week")
def weekly_review(offset: int = 0) -> dict[str, Any]:
    """One week (Mon–Sun) at a glance. offset=0 is this week, -1 last week, and so on."""
    from catalog import EXERCISES, guess_group
    groups = {name.lower(): group for name, group, _, _ in EXERCISES}
    today = date.today()
    start = today - timedelta(days=today.weekday()) + timedelta(weeks=min(0, offset))
    end = start + timedelta(days=6)
    with connection() as db:
        rows = db.execute(
            """SELECT sl.exercise_index, sl.weight, sl.reps, s.session_date, s.workout_json, s.id
               FROM set_logs sl JOIN sessions s ON s.id = sl.session_id WHERE s.session_date <= ?""",
            (end.isoformat(),),
        ).fetchall()
        sessions_done = db.execute(
            "SELECT kind, COUNT(*) FROM sessions WHERE status = 'completed' AND session_date BETWEEN ? AND ? GROUP BY kind", (start.isoformat(), end.isoformat())
        ).fetchall()
        planned_runs = db.execute(
            "SELECT description FROM sessions WHERE kind = 'run' AND status != 'cancelled' AND session_date BETWEEN ? AND ?", (start.isoformat(), end.isoformat())
        ).fetchall()
        runs = db.execute(
            "SELECT distance_miles FROM imported_activities WHERE lower(activity_type) LIKE '%run%' AND activity_date BETWEEN ? AND ?", (start.isoformat(), end.isoformat())
        ).fetchall()
        recovery = _week_metrics(db, start, end)
        previous_recovery = _week_metrics(db, start - timedelta(days=7), start - timedelta(days=1))
        bodyweight = current_bodyweight(db)
    sets_by_group: dict[str, int] = {}
    volume = 0.0
    best_before: dict[str, float] = {}
    best_this_week: dict[str, tuple[float, float, int, str]] = {}
    for row in rows:
        workout = json.loads(row["workout_json"] or "[]")
        if row["exercise_index"] >= len(workout):
            continue
        name = canonical_exercise(workout[row["exercise_index"]].partition("—")[0])
        e1rm = estimate_1rm(name, row["weight"], row["reps"], bodyweight)
        if row["session_date"] < start.isoformat():
            best_before[name] = max(best_before.get(name, 0), e1rm)
            continue
        group = groups.get(name.lower()) or guess_group(name)
        sets_by_group[group] = sets_by_group.get(group, 0) + 1
        volume += row["weight"] * row["reps"]
        if e1rm > best_this_week.get(name, (0,))[0]:
            best_this_week[name] = (e1rm, row["weight"], row["reps"], row["session_date"])
    records = [
        {"name": name, "e1rm": round(best[0], 1), "weight": best[1], "reps": best[2], "date": best[3], "previous": round(best_before[name], 1)}
        for name, best in best_this_week.items() if name in best_before and best[0] > best_before[name] + 0.05
    ]
    planned_miles = sum(float(m.group(1)) for desc in planned_runs if (m := re.search(r"([\d.]+)\s*mi\b", desc["description"] or "")))
    run_miles = [row["distance_miles"] or 0 for row in runs]
    return {
        "start": start.isoformat(), "end": end.isoformat(), "offset": min(0, offset),
        "lifting": {"sets_by_group": dict(sorted(sets_by_group.items(), key=lambda item: -item[1])), "total_sets": sum(sets_by_group.values()),
                    "volume": round(volume), "records": sorted(records, key=lambda r: -(r["e1rm"] - r["previous"]))},
        "sessions": {kind: count for kind, count in sessions_done},
        "running": {"miles": round(sum(run_miles), 1), "planned": round(planned_miles, 1), "runs": len(run_miles), "longest": round(max(run_miles, default=0), 1)},
        "recovery": recovery, "previous_recovery": previous_recovery,
    }


@app.post("/api/checkins")
def create_checkin(checkin: CheckIn) -> dict[str, Any]:
    with connection() as db:
        rows = db.execute("SELECT * FROM sessions WHERE session_date >= ? AND status = 'planned' ORDER BY session_date, title", (date.today().isoformat(),)).fetchall()
        sessions = [row_to_session(row) for row in rows]
        message, session_id = recommendation(checkin, sessions)
        db.execute(
            "INSERT INTO checkins (checked_at, available_minutes, readiness, soreness, location, recommendation) VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now().astimezone().isoformat(timespec="seconds"), checkin.available_minutes, checkin.readiness, checkin.soreness, checkin.location, message),
        )
    return {"recommendation": message, "session_id": session_id}


@app.post("/api/sessions/{session_id}/complete")
def complete_session(session_id: str, completion: Completion) -> dict[str, str]:
    with connection() as db:
        row = db.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Training session not found.")
        db.execute("UPDATE sessions SET status = 'completed' WHERE id = ?", (session_id,))
        db.execute(
            "INSERT INTO logs (session_id, completed_at, minutes, notes, metrics_json) VALUES (?, ?, ?, ?, ?)",
            (session_id, datetime.now().astimezone().isoformat(timespec="seconds"), completion.minutes, completion.notes, json.dumps(completion.metrics)),
        )
    return {"status": "completed"}


@app.post("/api/sessions/{session_id}/lock")
def toggle_lock(session_id: str) -> dict[str, bool]:
    with connection() as db:
        row = db.execute("SELECT locked FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Training session not found.")
        locked = not bool(row["locked"])
        db.execute("UPDATE sessions SET locked = ? WHERE id = ?", (int(locked), session_id))
    return {"locked": locked}


# ---------------------------------------------------------------- exercise catalog + workout editing


def exercise_history_stats() -> dict[str, dict[str, Any]]:
    """Per logged exercise: how many workouts, and the top set of the most recent one."""
    with connection() as db:
        rows = db.execute(
            """SELECT sl.session_id, sl.exercise_index, sl.weight, sl.reps, s.session_date, s.workout_json
               FROM set_logs sl JOIN sessions s ON s.id = sl.session_id"""
        ).fetchall()
    stats: dict[str, dict[str, Any]] = {}
    workouts: dict[str, list[str]] = {}
    for row in rows:
        lines = workouts.setdefault(row["session_id"], json.loads(row["workout_json"] or "[]"))
        if row["exercise_index"] >= len(lines):
            continue
        name = canonical_exercise(lines[row["exercise_index"]].partition("—")[0])
        item = stats.setdefault(name.lower(), {"name": name, "sessions": set(), "last_date": "", "last_weight": None, "last_reps": None})
        item["sessions"].add(row["session_id"])
        if (row["session_date"], row["weight"], row["reps"]) > (item["last_date"], item["last_weight"] or -1, item["last_reps"] or -1):
            item.update(last_date=row["session_date"], last_weight=row["weight"], last_reps=row["reps"])
    for item in stats.values():
        item["times"] = len(item.pop("sessions"))
    return stats


@app.get("/api/exercises/catalog")
def exercise_catalog(q: str = "", group: str = "", limit: int = 40) -> dict[str, Any]:
    from catalog import build_catalog, search_catalog
    items = build_catalog(exercise_history_stats())
    groups = sorted({item["group"] for item in items}, key=lambda g: (g == "Other", g))
    return {"groups": groups, "results": search_catalog(items, q, group, max(1, min(limit, 120)))}


@app.get("/api/templates")
def workout_templates() -> dict[str, Any]:
    from catalog import RUN_TYPES
    return {
        "lifts": ROUTINE_DAYS,
        "runs": [{"key": key, "label": value["label"], "structure": value["structure"]} for key, value in RUN_TYPES.items() if key != "race"],
    }


@app.post("/api/sessions")
def create_session(payload: SessionCreate) -> dict[str, Any]:
    try:
        date.fromisoformat(payload.session_date)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Use a valid date.") from error
    session_id = f"custom-{uuid4().hex[:12]}"
    workout = [canonical_workout_line(line) for line in payload.workout if line.strip()]
    with connection() as db:
        db.execute(
            """INSERT INTO sessions (id, session_date, kind, title, minutes, description, workout_json, status, locked, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'planned', 0, 'custom')""",
            (session_id, payload.session_date, payload.kind, payload.title.strip(), payload.minutes, payload.description, json.dumps(workout)),
        )
    return session_detail(session_id)


def _session_workout(db: sqlite3.Connection, session_id: str) -> list[str]:
    row = db.execute("SELECT workout_json FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Training session not found.")
    return json.loads(row["workout_json"] or "[]")


@app.post("/api/sessions/{session_id}/exercises")
def add_or_swap_exercise(session_id: str, payload: ExerciseInput) -> dict[str, Any]:
    with connection() as db:
        workout = _session_workout(db, session_id)
        line = format_workout_line(payload.name, payload.sets, payload.reps, payload.weight)
        if payload.replace_index is None:
            workout.append(line)
        else:
            if payload.replace_index >= len(workout):
                raise HTTPException(status_code=404, detail="That exercise is no longer in this workout.")
            if db.execute("SELECT 1 FROM set_logs WHERE session_id = ? AND exercise_index = ?", (session_id, payload.replace_index)).fetchone():
                raise HTTPException(status_code=409, detail="Sets are already logged for this exercise. Add the new one instead of swapping.")
            workout[payload.replace_index] = line
        db.execute("UPDATE sessions SET workout_json = ? WHERE id = ?", (json.dumps(workout), session_id))
    return session_detail(session_id)


@app.patch("/api/sessions/{session_id}/exercises/{index}")
def update_exercise(session_id: str, index: int, payload: ExerciseUpdate) -> dict[str, Any]:
    with connection() as db:
        workout = _session_workout(db, session_id)
        if index < 0 or index >= len(workout):
            raise HTTPException(status_code=404, detail="That exercise is no longer in this workout.")
        name, _, prescription = workout[index].partition("—")
        sets, reps, _ = prescription_details(prescription)
        weight = None if payload.clear_weight else (payload.weight if payload.weight is not None else line_target_weight(prescription))
        workout[index] = format_workout_line(name, payload.sets or sets, payload.reps or reps, weight)
        db.execute("UPDATE sessions SET workout_json = ? WHERE id = ?", (json.dumps(workout), session_id))
    return session_detail(session_id)


@app.delete("/api/sessions/{session_id}/exercises/{index}")
def remove_exercise(session_id: str, index: int) -> dict[str, Any]:
    """Remove one exercise and its logged sets; later exercises' sets shift down one index."""
    with connection() as db:
        workout = _session_workout(db, session_id)
        if index < 0 or index >= len(workout):
            raise HTTPException(status_code=404, detail="That exercise is no longer in this workout.")
        if len(workout) == 1:
            raise HTTPException(status_code=400, detail="Keep at least one exercise in the workout.")
        del workout[index]
        db.execute("DELETE FROM set_logs WHERE session_id = ? AND exercise_index = ?", (session_id, index))
        for later in range(index + 1, len(workout) + 1):
            db.execute("UPDATE set_logs SET exercise_index = ? WHERE session_id = ? AND exercise_index = ?", (later - 1, session_id, later))
        db.execute("UPDATE sessions SET workout_json = ? WHERE id = ?", (json.dumps(workout), session_id))
    return session_detail(session_id)


# ---------------------------------------------------------------- running + races


def recent_easy_pace() -> float | None:
    """Median pace (s/mi) of the last 28 days of Garmin runs, a stand-in for easy pace."""
    since = (date.today() - timedelta(days=28)).isoformat()
    with connection() as db:
        rows = db.execute(
            "SELECT distance_miles, duration_minutes FROM imported_activities WHERE activity_date >= ? AND lower(activity_type) LIKE '%run%' AND distance_miles > 1",
            (since,),
        ).fetchall()
    paces = sorted(row["duration_minutes"] * 60 / row["distance_miles"] for row in rows if row["duration_minutes"])
    return paces[len(paces) // 2] if paces else None


def recent_weekly_miles() -> float:
    since = (date.today() - timedelta(days=27)).isoformat()
    with connection() as db:
        total = db.execute(
            "SELECT COALESCE(SUM(distance_miles), 0) FROM imported_activities WHERE activity_date >= ? AND lower(activity_type) LIKE '%run%'", (since,)
        ).fetchone()[0]
    return round(total / 4, 1)


@app.get("/api/run-types")
def run_types() -> dict[str, Any]:
    from catalog import PACE_FACTORS, RUN_TYPES
    from race_plan import format_pace
    easy = recent_easy_pace()
    marathon = easy / PACE_FACTORS["easy"] if easy else None
    return {
        "types": [{"key": key, **value, "pace_text": format_pace(marathon * PACE_FACTORS[value["pace"]]) if marathon else None} for key, value in RUN_TYPES.items()],
        "easy_pace": format_pace(easy) if easy else None,
        "weekly_miles": recent_weekly_miles(),
    }


class RaceRequest(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    race_date: str
    distance: str = Field(pattern="^(5k|10k|half|marathon)$")
    goal_time: str | None = Field(default=None, max_length=10)
    run_days: list[int] = Field(default_factory=lambda: [1, 2, 5, 6])
    long_run_day: int = Field(default=6, ge=0, le=6)
    current_weekly_miles: float | None = Field(default=None, ge=0, le=150)
    peak_miles: float | None = Field(default=None, ge=10, le=120)


def _build_race(payload: RaceRequest) -> tuple[dict[str, Any], int | None]:
    from race_plan import generate_plan, parse_goal
    try:
        race_date = date.fromisoformat(payload.race_date)
        goal = parse_goal(payload.goal_time, payload.distance)
        plan = generate_plan(
            race_date, payload.distance, payload.current_weekly_miles if payload.current_weekly_miles is not None else recent_weekly_miles(),
            payload.run_days, payload.long_run_day, goal_seconds=goal, peak_miles=payload.peak_miles, easy_pace=recent_easy_pace(), race_name=payload.name,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return plan, goal


def actual_run_miles(start: date, end: date) -> float:
    with connection() as db:
        total = db.execute(
            "SELECT COALESCE(SUM(distance_miles), 0) FROM imported_activities WHERE activity_date BETWEEN ? AND ? AND lower(activity_type) LIKE '%run%'",
            (start.isoformat(), end.isoformat()),
        ).fetchone()[0]
    return round(total, 1)


def race_payload(row: sqlite3.Row) -> dict[str, Any]:
    plan = json.loads(row["plan_json"] or "{}")
    today = date.today()
    current = next((week for week in plan.get("weeks", []) if date.fromisoformat(week["start"]) <= today < date.fromisoformat(week["start"]) + timedelta(days=7)), None)
    # Planned vs actual (Garmin) for finished weeks, most recent last.
    history = []
    for week in plan.get("weeks", []):
        start = date.fromisoformat(week["start"])
        if start + timedelta(days=6) >= today:
            break
        history.append({"index": week["index"], "start": week["start"], "phase": week["phase"], "planned": week["miles"], "actual": actual_run_miles(start, start + timedelta(days=6))})
    recent = history[-2:]
    advice = None
    if recent:
        planned = sum(week["planned"] for week in recent)
        actual = sum(week["actual"] for week in recent)
        if planned and actual < planned * 0.7:
            advice = f"You ran {actual:g} of {planned:g} planned miles over the last {len(recent)} week{'s' if len(recent) > 1 else ''}. Recalibrate to rebuild from where you are instead of catching up."
        elif planned and actual > planned * 1.3:
            advice = f"You ran {actual:g} miles against {planned:g} planned over the last {len(recent)} week{'s' if len(recent) > 1 else ''}. Recalibrate if that's your new normal."
    return {
        "id": row["id"], "name": row["name"], "race_date": row["race_date"], "distance": row["distance"],
        "settings": json.loads(row["settings_json"] or "{}"), "applied_at": row["applied_at"],
        "days_to_go": (date.fromisoformat(row["race_date"]) - today).days, "current_week": current, "plan": plan,
        "history": history[-8:], "advice": advice,
    }


@app.post("/api/races/{race_id}/recalibrate")
def recalibrate_race(race_id: str) -> dict[str, Any]:
    """Rebuild the remaining weeks from your actual recent mileage (last 2 weeks of Garmin runs)."""
    with connection() as db:
        row = db.execute("SELECT * FROM races WHERE id = ?", (race_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Race not found.")
    settings = json.loads(row["settings_json"] or "{}")
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    recent = actual_run_miles(monday - timedelta(days=14), monday - timedelta(days=1)) / 2
    settings["current_weekly_miles"] = round(recent, 1)
    return update_race(race_id, RaceRequest(**settings))


@app.get("/api/races")
def list_races() -> dict[str, Any]:
    with connection() as db:
        rows = db.execute("SELECT * FROM races ORDER BY race_date").fetchall()
    return {"races": [race_payload(row) for row in rows], "weekly_miles": recent_weekly_miles(), "easy_pace": recent_easy_pace()}


@app.post("/api/races/preview")
def preview_race(payload: RaceRequest) -> dict[str, Any]:
    plan, _ = _build_race(payload)
    return {"plan": plan}


@app.post("/api/races")
def create_race(payload: RaceRequest) -> dict[str, Any]:
    plan, goal = _build_race(payload)
    race_id = f"race-{uuid4().hex[:10]}"
    with connection() as db:
        db.execute(
            "INSERT INTO races (id, name, race_date, distance, goal_seconds, settings_json, plan_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (race_id, payload.name, payload.race_date, payload.distance, goal, json.dumps(payload.model_dump()), json.dumps(plan), datetime.now().astimezone().isoformat(timespec="seconds")),
        )
        row = db.execute("SELECT * FROM races WHERE id = ?", (race_id,)).fetchone()
    return race_payload(row)


@app.put("/api/races/{race_id}")
def update_race(race_id: str, payload: RaceRequest) -> dict[str, Any]:
    plan, goal = _build_race(payload)
    with connection() as db:
        if not db.execute("SELECT 1 FROM races WHERE id = ?", (race_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Race not found.")
        db.execute(
            "UPDATE races SET name = ?, race_date = ?, distance = ?, goal_seconds = ?, settings_json = ?, plan_json = ? WHERE id = ?",
            (payload.name, payload.race_date, payload.distance, goal, json.dumps(payload.model_dump()), json.dumps(plan), race_id),
        )
        row = db.execute("SELECT * FROM races WHERE id = ?", (race_id,)).fetchone()
        applied = bool(row["applied_at"])
    if applied:  # keep the calendar in sync with the edited plan
        apply_race(race_id)
        with connection() as db:
            row = db.execute("SELECT * FROM races WHERE id = ?", (race_id,)).fetchone()
    return race_payload(row)


def _clear_race_sessions(db: sqlite3.Connection, race_id: str) -> None:
    db.execute(
        """DELETE FROM sessions WHERE source = ? AND status = 'planned' AND locked = 0
           AND NOT EXISTS (SELECT 1 FROM set_logs sl WHERE sl.session_id = sessions.id)""",
        (f"race:{race_id}",),
    )


@app.post("/api/races/{race_id}/apply")
def apply_race(race_id: str) -> dict[str, Any]:
    """Put the race plan's runs on the calendar from today to race day.

    Planned, unlocked run sessions in that window (e.g. the lifting block's easy runs)
    are replaced; completed, locked, and logged sessions are never touched.
    """
    today = date.today().isoformat()
    with connection() as db:
        row = db.execute("SELECT * FROM races WHERE id = ?", (race_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Race not found.")
        plan = json.loads(row["plan_json"])
        _clear_race_sessions(db, race_id)
        # Only replace runs on dates the race plan covers (it starts on a Monday, maybe next week).
        window_start = max(today, plan["weeks"][0]["start"]) if plan.get("weeks") else today
        replaced = db.execute(
            """DELETE FROM sessions WHERE kind = 'run' AND status = 'planned' AND locked = 0 AND session_date BETWEEN ? AND ?
               AND source NOT LIKE 'race:%' AND NOT EXISTS (SELECT 1 FROM logs l WHERE l.session_id = sessions.id)""",
            (window_start, row["race_date"]),
        ).rowcount
        created = 0
        for week in plan.get("weeks", []):
            for index, run in enumerate(week.get("runs", [])):
                if run["date"] < today:
                    continue
                db.execute(
                    """INSERT OR IGNORE INTO sessions (id, session_date, kind, title, minutes, description, workout_json, status, locked, source)
                       VALUES (?, ?, 'run', ?, ?, ?, ?, 'planned', 0, ?)""",
                    (f"{race_id}-{run['date']}-{index}", run["date"], run["title"], run["minutes"], run["description"], json.dumps(run["workout"]), f"race:{race_id}"),
                )
                created += 1
        db.execute("UPDATE races SET applied_at = ? WHERE id = ?", (datetime.now().astimezone().isoformat(timespec="seconds"), race_id))
    return {"status": "applied", "runs_created": created, "runs_replaced": replaced}


@app.delete("/api/races/{race_id}")
def delete_race(race_id: str) -> dict[str, Any]:
    with connection() as db:
        if not db.execute("SELECT 1 FROM races WHERE id = ?", (race_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Race not found.")
        _clear_race_sessions(db, race_id)
        db.execute("DELETE FROM races WHERE id = ?", (race_id,))
    return {"status": "deleted"}


# ---------------------------------------------------------------- rescheduling


class ScheduleRequest(BaseModel):
    action: str = Field(pattern="^(move|push|skip|swap)$")
    session_id: str | None = None
    other_id: str | None = None   # swap: the workout to trade days with
    repeat: int = Field(default=0, ge=0, le=8)  # swap: also trade the next N pairs of the same two workouts
    to_date: str | None = None
    from_date: str | None = None
    days: int = Field(default=1, ge=1, le=60)
    include_runs: bool = True
    mode: str = Field(default="tomorrow", pattern="^(tomorrow|push)$")


def _schedule_plan(db: sqlite3.Connection, request: ScheduleRequest) -> dict[str, Any]:
    from assistant import schedule
    try:
        if request.action == "move":
            if not request.session_id or not request.to_date:
                raise HTTPException(status_code=400, detail="Pick a workout and a new date.")
            date.fromisoformat(request.to_date)
            return schedule.move_session(db, request.session_id, request.to_date)
        if request.action == "swap":
            if not request.session_id or not request.other_id:
                raise HTTPException(status_code=400, detail="Pick the workout to swap with.")
            return schedule.swap_sessions(db, request.session_id, request.other_id, request.repeat)
        if request.action == "push":
            start = request.from_date or date.today().isoformat()
            date.fromisoformat(start)
            return schedule.push_back(db, start, request.days, request.include_runs)
        if not request.session_id:
            raise HTTPException(status_code=400, detail="Pick a workout to skip.")
        return schedule.skip_session(db, request.session_id, request.mode)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Use a valid date.") from error


@app.post("/api/schedule/preview")
def preview_schedule(request: ScheduleRequest) -> dict[str, Any]:
    """What a move/push/skip would change, without changing anything."""
    with connection() as db:
        return _schedule_plan(db, request)


@app.post("/api/schedule/apply")
def apply_schedule(request: ScheduleRequest) -> dict[str, Any]:
    from assistant import schedule
    with connection() as db:
        result = schedule.apply_moves(db, _schedule_plan(db, request))
    from assistant import outlook, planner
    with connection() as db:
        planner.cleanup(db)  # drop accepted workout blocks whose session moved
    outlook.sync_soon()
    return result


@app.post("/api/schedule/undo")
def undo_schedule() -> dict[str, Any]:
    from assistant import outlook, schedule
    with connection() as db:
        result = schedule.undo_last(db)
    outlook.sync_soon()
    return result


@app.get("/api/schedule/swap-options/{session_id}")
def swap_options(session_id: str) -> dict[str, Any]:
    from assistant import schedule
    with connection() as db:
        return {"sessions": schedule.swap_options(db, session_id)}


@app.get("/api/schedule/last")
def last_schedule_change() -> dict[str, Any]:
    from assistant import schedule
    with connection() as db:
        return {"summary": schedule.last_change(db)}


def race_run_cutoff(db: sqlite3.Connection) -> str | None:
    """Latest race date with an applied plan; block runs up to it are owned by the race plan."""
    row = db.execute("SELECT MAX(race_date) FROM races WHERE applied_at IS NOT NULL AND race_date >= ?", (date.today().isoformat(),)).fetchone()
    return row[0] if row and row[0] else None


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    # The HTML document changes alongside its JavaScript.  Do not let a browser
    # keep an old document that references controls from a different release.
    return FileResponse(
        STATIC / "index.html",
        headers={"Cache-Control": "no-store, max-age=0, must-revalidate"},
    )
