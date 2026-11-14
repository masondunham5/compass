"""Private, phone-first health hub that runs locally or on a home server."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import csv
import hashlib
import io
import math
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DATABASE = DATA_DIR / "lifting_hub.db"
LEGACY_FILE = Path(os.getenv("LOCALAPPDATA", ".")) / "Training Planner" / "planner.json"
STATIC = ROOT / "static"


def load_local_env() -> None:
    """Load simple KEY=value pairs from a local .env without adding a dependency.

    The file is intentionally ignored by Git and values already provided by Windows
    or Task Scheduler always win.
    """
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_local_env()

app = FastAPI(
    title="Health Hub",
    description="A local-first training, recovery, and activity planner.",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
)


class CheckIn(BaseModel):
    available_minutes: int = Field(ge=0, le=360)
    readiness: int = Field(ge=1, le=10)
    soreness: int = Field(ge=1, le=10)
    location: str = "Full gym"


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


class BlockDraftRequest(BaseModel):
    name: str = Field(default="My next training block", min_length=2, max_length=80)
    weeks: int = Field(default=10, ge=4, le=24)
    goals: list[str] = Field(default_factory=lambda: ["Build strength", "Build muscle", "Improve endurance"])
    lifting_days: int = Field(default=5, ge=2, le=6)
    run_days: int = Field(default=3, ge=0, le=5)
    starting_miles: float = Field(default=6, ge=0, le=50)
    available_minutes: int = Field(default=60, ge=20, le=180)
    equipment: str = "Full gym"
    avoid: str = "Bulgarian split squats"
    split: str = "body-part-balanced"
    include_mobility: bool = False


class DailyHealth(BaseModel):
    sleep_hours: float = Field(ge=0, le=24)
    sleep_quality: int = Field(ge=1, le=10)
    energy: int = Field(ge=1, le=10)
    soreness: int = Field(ge=1, le=10)
    stress: int = Field(ge=1, le=10)
    weight_lbs: float | None = Field(default=None, ge=50, le=700)
    protein_goal_met: bool = False
    notes: str = Field(default="", max_length=1000)


def monday_on_or_after(day: date) -> date:
    return day + timedelta(days=(7 - day.weekday()) % 7)


def phase_for(week: int, weeks: int) -> tuple[str, str]:
    if week == 1:
        return "Re-entry", "Rebuild rhythm and leave a few reps in reserve."
    if week in {max(2, weeks // 2), weeks}:
        return "Deload", "Reduce volume and preserve technique, not momentum."
    if week <= max(2, weeks // 2 - 1):
        return "Foundation", "Build consistent volume with controlled effort."
    return "Build", "Progress working sets when form and effort support it."


def strength_workouts(split: str, avoid: str) -> list[tuple[str, list[str]]]:
    lower_single_leg = "Leg Press — 3 × 10–12" if "bulgarian" in avoid.lower() else "Bulgarian Split Squat — 3 × 8–10"
    body_part = [
        ("Chest, Triceps & Shoulders", ["Barbell Bench Press — 4 × 5–8", "Incline Dumbbell Press — 3 × 8–12", "Cable Lateral Raise — 3 × 12–15", "Triceps Pressdown — 3 × 10–15", "Overhead Cable Extension — 2 × 10–15"]),
        ("Lower · Squat Focus", ["Back Squat — 4 × 5–8", "Romanian Deadlift — 3 × 6–10", lower_single_leg, "Leg Curl — 3 × 10–15", "Standing Calf Raise — 3 × 10–15"]),
        ("Back & Biceps", ["Pull-up or Lat Pulldown — 4 × 6–10", "Chest-Supported Row — 4 × 6–10", "Rear-Delt Fly — 3 × 12–15", "Cable Curl — 3 × 8–12", "Hammer Curl — 3 × 10–15"]),
        ("Chest, Triceps & Shoulders · Volume", ["Dumbbell Bench Press — 3 × 8–12", "Overhead Press — 3 × 6–10", "Cable Fly — 3 × 10–15", "Cable Lateral Raise — 3 × 12–15", "Triceps Pressdown — 3 × 10–15"]),
        ("Lower · Hinge Focus", ["Deadlift or Trap-Bar Deadlift — 3 × 3–6", "Front Squat or Leg Press — 3 × 8–12", "Hip Thrust — 3 × 8–12", "Leg Extension — 2 × 12–15", "Seated Calf Raise — 3 × 10–15"]),
        ("Back & Biceps · Volume", ["Lat Pulldown — 3 × 8–12", "Single-Arm Cable Row — 3 × 10–12", "Face Pull — 3 × 12–15", "Incline Dumbbell Curl — 3 × 10–12", "Hammer Curl — 3 × 10–15"]),
    ]
    if split == "body-part-balanced":
        return body_part
    if split == "push-pull-legs":
        return [
            ("Push", ["Barbell Bench Press — 4 × 5–8", "Overhead Press — 3 × 6–10", "Cable Fly — 3 × 10–15", "Cable Lateral Raise — 3 × 12–15", "Triceps Pressdown — 3 × 10–15"]),
            ("Pull", ["Pull-up or Lat Pulldown — 4 × 6–10", "Chest-Supported Row — 4 × 6–10", "Rear-Delt Fly — 3 × 12–15", "Cable Curl — 3 × 8–12", "Hammer Curl — 3 × 10–15"]),
            ("Legs", ["Back Squat — 4 × 5–8", "Romanian Deadlift — 3 × 6–10", lower_single_leg, "Leg Curl — 3 × 10–15", "Standing Calf Raise — 3 × 10–15"]),
        ] * 2
    if split == "full-body":
        return [
            ("Full Body A", ["Back Squat — 3 × 5–8", "Barbell Bench Press — 3 × 5–8", "Chest-Supported Row — 3 × 8–12", "Cable Lateral Raise — 2 × 12–15"]),
            ("Full Body B", ["Romanian Deadlift — 3 × 6–10", "Overhead Press — 3 × 6–10", "Lat Pulldown — 3 × 8–12", "Cable Curl — 2 × 10–15"]),
            ("Full Body C", ["Leg Press — 3 × 10–12", "Dumbbell Bench Press — 3 × 8–12", "Single-Arm Cable Row — 3 × 10–12", "Triceps Pressdown — 2 × 10–15"]),
        ] * 2
    return [
        ("Upper A", ["Barbell Bench Press — 4 × 5–8", "Chest-Supported Row — 4 × 6–10", "Incline Dumbbell Press — 3 × 8–12", "Lat Pulldown — 3 × 8–12", "Cable Lateral Raise — 3 × 12–15"]),
        ("Lower A", ["Back Squat — 4 × 5–8", "Romanian Deadlift — 3 × 6–10", lower_single_leg, "Leg Curl — 3 × 10–15", "Standing Calf Raise — 3 × 10–15"]),
        ("Push", ["Overhead Press — 4 × 5–8", "Dumbbell Bench Press — 3 × 8–12", "Cable Fly — 3 × 10–15", "Cable Lateral Raise — 3 × 12–15", "Triceps Pressdown — 3 × 10–15"]),
        ("Pull", ["Pull-up or Lat Pulldown — 4 × 6–10", "Barbell or Chest-Supported Row — 4 × 6–10", "Rear-Delt Fly — 3 × 12–15", "Cable Curl — 3 × 8–12", "Hammer Curl — 3 × 10–15"]),
        ("Lower B", ["Deadlift or Trap-Bar Deadlift — 3 × 3–6", "Front Squat or Leg Press — 3 × 8–12", "Hip Thrust — 3 × 8–12", "Leg Extension — 2 × 12–15", "Seated Calf Raise — 3 × 10–15"]),
    ]


def build_draft(request: BlockDraftRequest) -> dict[str, Any]:
    start = monday_on_or_after(date.today())
    lifting = strength_workouts(request.split, request.avoid)[:request.lifting_days]
    week_template: list[tuple[int, str, str, int, str, list[str]]] = []
    lifting_days = [0, 1, 3, 4, 6][:len(lifting)]
    for day_offset, (title, workout) in zip(lifting_days, lifting):
        week_template.append((day_offset, "strength", title, request.available_minutes, "Strength session", workout))
    if request.run_days:
        run_offsets = [2, 5, 6][:request.run_days]
        run_names = ["Easy Run", "Easy Run", "Long Run"]
        for index, day_offset in enumerate(run_offsets):
            fraction = 0.2 if index < 2 else 0.35
            miles = max(1.5, request.starting_miles * fraction)
            week_template.append((day_offset, "run", run_names[index], max(25, int(miles * 11)), f"Easy conversational effort · about {miles:.1f} mi in week 1", ["Easy effort — conversational pace", "Record distance, time, average HR, maximum HR, and effort"] ))
    if request.include_mobility:
        week_template.append((6, "mobility", "Mobility & recovery", 20, "Optional low-intensity mobility session", ["Mobility flow — 20 minutes", "Keep the effort restorative"] ))
    sessions: list[dict[str, Any]] = []
    phases: list[dict[str, Any]] = []
    for week in range(1, request.weeks + 1):
        phase, message = phase_for(week, request.weeks)
        phases.append({"week": week, "name": phase, "message": message})
        run_multiplier = 1 if phase == "Deload" else 1 + ((week - 1) * 0.06)
        for day_offset, kind, title, minutes, description, workout in week_template:
            scheduled = start + timedelta(days=(week - 1) * 7 + day_offset)
            session_title = f"W{week} · {title}"
            session_description = f"{phase}: {description}"
            if kind == "run":
                initial = request.starting_miles * (0.2 if title != "Long Run" else 0.35)
                target_miles = max(1.5, initial * run_multiplier)
                session_description = f"{phase}: easy effort · target {target_miles:.1f} mi"
                minutes = max(25, int(target_miles * 11))
            sessions.append({"id": f"block-{uuid4().hex[:12]}", "date": scheduled.isoformat(), "kind": kind, "title": session_title, "minutes": minutes, "description": session_description, "workout": workout, "status": "planned", "locked": False})
    return {"id": f"plan-{uuid4().hex[:12]}", "name": request.name, "start_date": start.isoformat(), "goals": request.goals, "config": request.model_dump(), "phases": phases, "sessions": sessions}


def connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    return db


def initialize() -> None:
    with connection() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                session_date TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                minutes INTEGER NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                workout_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'planned',
                locked INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'training-planner'
            );
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                minutes INTEGER NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                metrics_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS checkins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                checked_at TEXT NOT NULL,
                available_minutes INTEGER NOT NULL,
                readiness INTEGER NOT NULL,
                soreness INTEGER NOT NULL,
                location TEXT NOT NULL,
                recommendation TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS set_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                exercise_index INTEGER NOT NULL,
                set_number INTEGER NOT NULL,
                weight REAL NOT NULL,
                reps INTEGER NOT NULL,
                rpe REAL,
                note TEXT NOT NULL DEFAULT '',
                completed_at TEXT NOT NULL,
                UNIQUE(session_id, exercise_index, set_number)
            );
            CREATE TABLE IF NOT EXISTS plans (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                plan_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS daily_health (
                checkin_date TEXT PRIMARY KEY,
                sleep_hours REAL NOT NULL,
                sleep_quality INTEGER NOT NULL,
                energy INTEGER NOT NULL,
                soreness INTEGER NOT NULL,
                stress INTEGER NOT NULL,
                weight_lbs REAL,
                protein_goal_met INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS imported_activities (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                activity_date TEXT NOT NULL,
                activity_type TEXT NOT NULL,
                title TEXT NOT NULL,
                distance_miles REAL,
                duration_minutes REAL,
                average_hr INTEGER,
                max_hr INTEGER,
                elevation_feet REAL,
                details_json TEXT NOT NULL DEFAULT '{}',
                imported_at TEXT NOT NULL,
                linked_session_id TEXT
            );
            CREATE TABLE IF NOT EXISTS garmin_daily_metrics (
                calendar_date TEXT PRIMARY KEY,
                steps INTEGER,
                active_calories REAL,
                total_calories REAL,
                resting_hr INTEGER,
                hrv_ms REAL,
                sleep_seconds INTEGER,
                stress_avg REAL,
                body_battery INTEGER,
                source_json TEXT NOT NULL DEFAULT '{}',
                synced_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS garmin_sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                activities_imported INTEGER NOT NULL DEFAULT 0,
                metrics_updated INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT ''
            );
            """
        )
        existing = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        if existing == 0:
            import_legacy_sessions(db)


def import_legacy_sessions(db: sqlite3.Connection) -> None:
    """One-way starter import; existing Training Planner files remain untouched."""
    if not LEGACY_FILE.exists():
        return
    try:
        payload = json.loads(LEGACY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for rows in payload.get("weeks", {}).values():
        for item in rows if isinstance(rows, list) else []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            cursor = db.execute(
                """INSERT OR IGNORE INTO sessions
                   (id, session_date, kind, title, minutes, description, workout_json, status, locked, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(item["id"]), item.get("date", date.today().isoformat()), item.get("kind", "strength"),
                    item.get("title", "Training session"), int(item.get("minutes", 45)), item.get("description", ""),
                    json.dumps(item.get("workout", [])), item.get("status", "planned"), int(bool(item.get("locked"))), "legacy-import",
                ),
            )


def row_to_session(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "date": row["session_date"], "kind": row["kind"], "title": row["title"],
        "minutes": row["minutes"], "description": row["description"], "workout": json.loads(row["workout_json"]),
        "status": row["status"], "locked": bool(row["locked"]),
    }


def normalize_exercise(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def prescription_details(prescription: str) -> tuple[int, str, str]:
    """Extract target sets/reps and a transparent no-baseline starting rule."""
    match = re.search(r"(\d+)\s*[×x]\s*(\d+)(?:\s*[–-]\s*(\d+))?", prescription)
    if not match:
        return 3, "RPE 7–8", "No baseline yet: choose a load that leaves about 2–3 reps in reserve."
    sets, low, high = int(match.group(1)), int(match.group(2)), int(match.group(3) or match.group(2))
    reps = f"{low}–{high}" if high != low else str(low)
    if high <= 6:
        guide = "~75–85% 1RM · RPE 7–8"
    elif high <= 10:
        guide = "~65–78% 1RM · RPE 7–8"
    else:
        guide = "~55–70% 1RM · RPE 7–8"
    return sets, reps, f"No baseline yet: use {guide}; stop with 2–3 reps in reserve."


def exercise_name_from_session(session: dict[str, Any], exercise_index: int) -> str | None:
    workout = session.get("workout", [])
    if exercise_index >= len(workout):
        return None
    return workout[exercise_index].partition("—")[0].strip()


def exercises_for(session: dict[str, Any], rows: list[sqlite3.Row], previous_rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Turn stable workout outline lines into a usable in-gym set logger."""
    by_exercise: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_exercise.setdefault(row["exercise_index"], []).append(dict(row))
    exercises = []
    for index, outline in enumerate(session.get("workout", []) or ["Main movement"]):
        name, _, prescription = outline.partition("—")
        logged = sorted(by_exercise.get(index, []), key=lambda item: item["set_number"])
        same_exercise = [dict(row) for row in previous_rows if normalize_exercise(row["exercise_name"]) == normalize_exercise(name)]
        previous = max(same_exercise, key=lambda item: item["completed_at"]) if same_exercise else None
        target_sets, target_reps, starting_guide = prescription_details(prescription.strip() or "Working sets")
        exercises.append({
            "index": index, "name": name.strip(), "prescription": prescription.strip() or "Working sets",
            "sets": logged, "target_sets": target_sets, "target_reps": target_reps,
            "suggested_weight": previous["weight"] if previous else None,
            "suggested_reps": previous["reps"] if previous else None,
            "starting_guide": starting_guide if not previous else f"Previous logged set: {previous['weight']:g} lb × {previous['reps']}. Start there, then adjust for RPE 7–8.",
        })
    return exercises


def recommendation(checkin: CheckIn, sessions: list[dict[str, Any]]) -> tuple[str, str | None]:
    candidates = [session for session in sessions if session["status"] == "planned"]
    candidates.sort(key=lambda item: (0 if item["kind"] == "strength" else 1, item["date"]))
    if checkin.readiness <= 3 or checkin.soreness >= 8:
        return "Recovery first: take 20–30 easy minutes of walking and mobility. No catch-up volume today.", None
    if not candidates:
        return "No planned training remains in the next two weeks. A relaxed walk or mobility is enough today.", None
    target = candidates[0]
    if checkin.available_minutes < max(20, target["minutes"] * 0.6):
        if target["kind"] == "strength" and checkin.location == "Apartment gym":
            return "Apartment backup: do a short back-and-biceps session today. Keep the main gym session for another day.", target["id"]
        return f"Short version: do the first 2–3 movements of {target['title']} in {checkin.available_minutes} minutes.", target["id"]
    return f"Do {target['title']} today. It protects your lifting consistency and weekly mileage plan.", target["id"]


def health_recommendation(record: dict[str, Any] | None) -> str:
    """A conservative coaching prompt, not medical advice or an automatic schedule change."""
    if not record:
        return "Log a quick health check-in to give today’s training recommendation some context."
    if record["sleep_hours"] < 5.5 or record["energy"] <= 3 or record["soreness"] >= 8:
        return "Recovery first: keep movement easy today. A walk, mobility, or a shortened session is more useful than forced catch-up."
    if record["sleep_hours"] < 6.5 or record["stress"] >= 8:
        return "Protect recovery today: keep intensity controlled and leave a few reps in reserve."
    if record["protein_goal_met"]:
        return "You have a solid recovery foundation today. Follow the planned session and use RPE to guide any load changes."
    return "Readiness looks workable. Train as planned, then prioritize a protein-focused meal after your session."


def mean_metric(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [float(row[key]) for row in rows if row.get(key) is not None]
    return round(sum(values) / len(values), 1) if values else None


def deterministic_training_recommendation(
    sessions: list[dict[str, Any]], daily_metrics: list[dict[str, Any]], manual_health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Explainable rule engine. It never writes, moves, or cancels a session."""
    today = date.today().isoformat()
    # Yesterday's metrics are useful as baseline history, but never presented as today's recovery state.
    current = next((row for row in daily_metrics if row.get("calendar_date") == today), None)
    baseline_rows = [row for row in daily_metrics if row.get("calendar_date") != today][:28]
    baseline_hrv = mean_metric(baseline_rows, "hrv_ms")
    baseline_rhr = mean_metric(baseline_rows, "resting_hr")
    score = 100
    reasons: list[str] = []
    if current and current.get("sleep_seconds") is not None:
        sleep_hours = float(current["sleep_seconds"]) / 3600
        if sleep_hours < 6:
            score -= 30; reasons.append(f"Sleep was {sleep_hours:.1f}h (under the 6h recovery floor).")
        elif sleep_hours < 7:
            score -= 12; reasons.append(f"Sleep was {sleep_hours:.1f}h (below the 7h target).")
    if current and baseline_hrv and current.get("hrv_ms") is not None and float(current["hrv_ms"]) <= baseline_hrv * 0.85:
        score -= 20; reasons.append(f"HRV is {round((1 - float(current['hrv_ms']) / baseline_hrv) * 100)}% below your rolling baseline.")
    if current and baseline_rhr and current.get("resting_hr") is not None and float(current["resting_hr"]) >= baseline_rhr * 1.08:
        score -= 15; reasons.append(f"Resting HR is {round((float(current['resting_hr']) / baseline_rhr - 1) * 100)}% above your rolling baseline.")
    if current and current.get("stress_avg") is not None and float(current["stress_avg"]) >= 65:
        score -= 10; reasons.append("Garmin stress average is elevated (65 or above).")
    if manual_health:
        if int(manual_health.get("soreness", 0)) >= 8:
            score -= 30; reasons.append("Manual soreness is 8/10 or higher.")
        elif int(manual_health.get("soreness", 0)) >= 6:
            score -= 8; reasons.append("Manual soreness is 6/10 or higher.")
        if int(manual_health.get("energy", 10)) <= 3:
            score -= 25; reasons.append("Manual energy is 3/10 or lower.")
        elif int(manual_health.get("energy", 10)) <= 5:
            score -= 8; reasons.append("Manual energy is 5/10 or lower.")
    score = max(0, score)
    planned = [session for session in sessions if session.get("status") == "planned"]
    planned.sort(key=lambda session: (0 if session.get("date") == today else 1, session.get("date", ""), session.get("title", "")))
    target = next((session for session in planned if session.get("locked")), None) or (planned[0] if planned else None)
    target_name = target["title"] if target else "your next training block"
    if score <= 50:
        title, mode = "Recovery-first day", "recovery"
        guidance = "Take 20–30 easy minutes of walking and mobility, or rest. Do not add catch-up volume today."
    elif score <= 75:
        title, mode = "Use the short version", "shorten"
        if target and target.get("kind") == "run":
            guidance = f"Keep {target_name} easy and cap it around {max(20, round(target.get('minutes', 30) * 0.6))} minutes. Skip intensity."
        elif target:
            guidance = f"Do the first 2–3 movements of {target_name} in about {max(25, round(target.get('minutes', 45) * 0.6))} minutes. Leave 2–3 reps in reserve."
        else:
            guidance = "Keep activity easy today and reassess after a check-in tomorrow."
    elif target:
        title, mode = "Planned session is appropriate", "train"
        guidance = f"Do {target_name} as planned. Keep the opening working sets controlled and adjust load by effort."
    else:
        title, mode = "No session needs moving", "rest"
        guidance = "You have no planned session in the next week. A relaxed walk or mobility is enough today."
    if not reasons:
        reasons.append("No recovery flag crossed a conservative threshold; recommendation is based on the planned session.")
    return {
        "engine": "deterministic-v1", "score": score, "mode": mode, "title": title, "guidance": guidance,
        "target_session": {"id": target["id"], "title": target["title"], "locked": bool(target.get("locked"))} if target else None,
        "reasons": reasons,
        "signals": {"sleep_hours": round(float(current["sleep_seconds"]) / 3600, 1) if current and current.get("sleep_seconds") is not None else None, "hrv": current.get("hrv_ms") if current else None, "hrv_baseline": baseline_hrv, "resting_hr": current.get("resting_hr") if current else None, "resting_hr_baseline": baseline_rhr, "manual_energy": manual_health.get("energy") if manual_health else None, "manual_soreness": manual_health.get("soreness") if manual_health else None},
    }


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    match = re.search(r"-?[\d,.]+", str(value))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_duration_minutes(value: Any) -> float | None:
    if value is None:
        return None
    numeric = parse_number(value)
    text = str(value).strip()
    if ":" not in text:
        return numeric
    try:
        pieces = [float(part) for part in text.split(":")]
        if len(pieces) == 3:
            return pieces[0] * 60 + pieces[1] + pieces[2] / 60
        if len(pieces) == 2:
            return pieces[0] + pieces[1] / 60
    except ValueError:
        return numeric
    return numeric


def parse_iso_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def activity_id(source: str, activity: dict[str, Any]) -> str:
    signature = "|".join(str(activity.get(key, "")) for key in ("activity_date", "activity_type", "title", "distance_miles", "duration_minutes"))
    return f"activity-{hashlib.sha256(f'{source}|{signature}'.encode()).hexdigest()[:20]}"


def point_distance_miles(one: tuple[float, float], two: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, one)
    lat2, lon2 = map(math.radians, two)
    delta_lat, delta_lon = lat2 - lat1, lon2 - lon1
    a = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(a))


def normalize_activity(payload: dict[str, Any], source: str) -> dict[str, Any] | None:
    activity_type = str(payload.get("activity_type") or "Activity").strip()
    title = str(payload.get("title") or activity_type).strip()
    activity_date = str(payload.get("activity_date") or "").strip()[:10]
    if not activity_date:
        return None
    item = {
        "activity_date": activity_date,
        "activity_type": activity_type,
        "title": title,
        "distance_miles": payload.get("distance_miles"),
        "duration_minutes": payload.get("duration_minutes"),
        "average_hr": payload.get("average_hr"),
        "max_hr": payload.get("max_hr"),
        "elevation_feet": payload.get("elevation_feet"),
        "details": payload.get("details", {}),
    }
    item["id"] = activity_id(source, item)
    return item


def parse_garmin_csv(text: str) -> list[dict[str, Any]]:
    rows = list(csv.DictReader(io.StringIO(text)))
    activities = []
    for row in rows:
        lowered = {str(key).lower().strip(): value for key, value in row.items()}
        raw_type = lowered.get("activity type") or lowered.get("type") or "Activity"
        if "run" not in str(raw_type).lower():
            continue
        raw_date = lowered.get("date") or lowered.get("activity date") or lowered.get("start time")
        parsed = parse_iso_time(raw_date)
        date_value = parsed.date().isoformat() if parsed else (str(raw_date or "")[:10])
        distance = parse_number(lowered.get("distance"))
        if distance and "km" in str(lowered.get("distance", "")).lower():
            distance *= 0.621371
        activities.append({
            "activity_date": date_value,
            "activity_type": "Run",
            "title": lowered.get("title") or lowered.get("activity name") or "Garmin run",
            "distance_miles": distance,
            "duration_minutes": parse_duration_minutes(lowered.get("time") or lowered.get("duration") or lowered.get("moving time")),
            "average_hr": parse_number(lowered.get("avg hr") or lowered.get("average heart rate")),
            "max_hr": parse_number(lowered.get("max hr") or lowered.get("maximum heart rate")),
            "elevation_feet": parse_number(lowered.get("elev gain") or lowered.get("elevation gain")),
            "details": {"source_row": row},
        })
    return activities


def parse_xml_activity(content: bytes, source: str) -> list[dict[str, Any]]:
    root = ET.fromstring(content)
    tag = root.tag.lower()
    points = []
    times, heart_rates = [], []
    distances = []
    for node in root.iter():
        local = node.tag.rsplit("}", 1)[-1]
        if local == "Trackpoint":
            lat = node.findtext(".//{*}LatitudeDegrees")
            lon = node.findtext(".//{*}LongitudeDegrees")
            if lat and lon:
                points.append((float(lat), float(lon)))
            timestamp = node.findtext(".//{*}Time")
            if timestamp:
                times.append(parse_iso_time(timestamp))
            distance = parse_number(node.findtext(".//{*}DistanceMeters"))
            if distance is not None:
                distances.append(distance)
            heart_rate = parse_number(node.findtext(".//{*}HeartRateBpm/{*}Value"))
            if heart_rate is not None:
                heart_rates.append(int(heart_rate))
    if not times:
        return []
    distance_miles = (max(distances) / 1609.344) if distances else sum(point_distance_miles(points[index - 1], point) for index, point in enumerate(points) if index)
    duration = (times[-1] - times[0]).total_seconds() / 60 if len(times) > 1 and times[0] and times[-1] else None
    return [{
        "activity_date": times[0].date().isoformat(), "activity_type": "Run", "title": f"Imported {source.upper()} run",
        "distance_miles": round(distance_miles, 2) if distance_miles else None,
        "duration_minutes": round(duration, 1) if duration else None,
        "average_hr": round(sum(heart_rates) / len(heart_rates)) if heart_rates else None,
        "max_hr": max(heart_rates) if heart_rates else None,
        "elevation_feet": None, "details": {"track_points": len(points)},
    }]


@app.on_event("startup")
def startup() -> None:
    initialize()


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
    records = [dict(row) for row in rows]
    latest = records[0] if records else None
    average = lambda key: round(sum(row[key] or 0 for row in records if row[key] is not None) / max(1, sum(row[key] is not None for row in records)), 1)
    return {
        "status": garmin_status(),
        "days": days,
        "latest": latest,
        "records": records,
        "average_sleep_hours": round(average("sleep_seconds") / 3600, 1) if any(row["sleep_seconds"] for row in records) else None,
        "average_steps": round(average("steps")) if any(row["steps"] is not None for row in records) else None,
        "average_hrv": average("hrv_ms") if any(row["hrv_ms"] is not None for row in records) else None,
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
    return deterministic_training_recommendation(
        [row_to_session(row) for row in session_rows], [dict(row) for row in metric_rows], dict(manual) if manual else None
    )


@app.post("/api/garmin/sync")
def sync_garmin_now() -> dict[str, Any]:
    if not os.getenv("GARMIN_PERSONAL_API_KEY", "").strip():
        raise HTTPException(status_code=400, detail="Garmin is not connected yet. Add your key to the local .env file first.")
    try:
        from garmin_sync import GarminSyncError, run_sync
        return run_sync(days=2)
    except GarminSyncError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/health/checkin")
def save_daily_health(checkin: DailyHealth) -> dict[str, Any]:
    today = date.today().isoformat()
    record = {**checkin.model_dump(), "checkin_date": today}
    with connection() as db:
        db.execute(
            """INSERT INTO daily_health (checkin_date, sleep_hours, sleep_quality, energy, soreness, stress, weight_lbs, protein_goal_met, notes, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(checkin_date) DO UPDATE SET sleep_hours=excluded.sleep_hours, sleep_quality=excluded.sleep_quality,
               energy=excluded.energy, soreness=excluded.soreness, stress=excluded.stress, weight_lbs=excluded.weight_lbs,
               protein_goal_met=excluded.protein_goal_met, notes=excluded.notes, updated_at=excluded.updated_at""",
            (today, checkin.sleep_hours, checkin.sleep_quality, checkin.energy, checkin.soreness, checkin.stress, checkin.weight_lbs, int(checkin.protein_goal_met), checkin.notes, datetime.now().astimezone().isoformat(timespec="seconds")),
        )
    return {"status": "saved", "record": record, "recommendation": health_recommendation(record)}


@app.get("/api/activities")
def activities() -> dict[str, Any]:
    with connection() as db:
        rows = db.execute("SELECT * FROM imported_activities ORDER BY activity_date DESC, imported_at DESC LIMIT 60").fetchall()
    return {"activities": [{**dict(row), "details": json.loads(row["details_json"])} for row in rows]}


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
        for session in plan.get("sessions", []):
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
        for session in replacement["sessions"]:
            if session["date"] < today:
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
            """SELECT sl.*, s.workout_json FROM set_logs sl
               JOIN sessions s ON s.id = sl.session_id
               WHERE sl.session_id != ? ORDER BY sl.completed_at DESC""", (session_id,)
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
    set_counts = {row["session_id"]: row["sets_logged"] for row in log_rows}
    metrics_by_session: dict[str, dict[str, Any]] = {}
    for row in completion_rows:
        metrics_by_session.setdefault(row["session_id"], json.loads(row["metrics_json"]))
    sessions = []
    for row in rows:
        session = row_to_session(row)
        session["sets_logged"] = set_counts.get(session["id"], 0)
        session["metrics"] = metrics_by_session.get(session["id"], {})
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
    exercises: dict[str, dict[str, Any]] = {}
    for row in rows:
        workout = json.loads(row["workout_json"])
        if row["exercise_index"] >= len(workout):
            continue
        name = workout[row["exercise_index"]].partition("—")[0].strip()
        key = normalize_exercise(name)
        estimated_1rm = round(row["weight"] * (1 + row["reps"] / 30), 1) if row["reps"] else 0
        point = {"date": row["session_date"], "weight": row["weight"], "reps": row["reps"], "rpe": row["rpe"], "e1rm": estimated_1rm, "session": row["title"]}
        item = exercises.setdefault(key, {"name": name, "history": []})
        item["history"].append(point)
    result = []
    for item in exercises.values():
        history = item["history"]
        best = max(history, key=lambda point: (point["e1rm"], point["weight"], point["reps"]))
        item["best_set"] = best
        item["latest"] = history[-1]
        result.append(item)
    return {"exercises": sorted(result, key=lambda item: item["name"].lower())}


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


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    # The HTML document changes alongside its JavaScript.  Do not let a browser
    # keep an old document that references controls from a different release.
    return FileResponse(
        STATIC / "index.html",
        headers={"Cache-Control": "no-store, max-age=0, must-revalidate"},
    )
