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
    split: str = "logged-routine"
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


# Built from the lifting log (Jun–Nov 2026): chest/triceps/shoulders twice a week,
# back/biceps once or twice, legs about once, using the exercises actually logged.
ROUTINE_DAYS: dict[str, list[str]] = {
    "Chest, Triceps & Shoulders": ["Barbell Bench Press — 3 × 4–6", "Incline Dumbbell Press — 3 × 6–8", "Seated Dumbbell Shoulder Press — 2 × 8–10", "Pec Dec Fly — 3 × 8–10", "Rope Pushdown — 3 × 8–12"],
    "Back & Biceps": ["Lat Pulldown — 3 × 6–10", "Seated Row — 3 × 8–10", "Face Pull — 2 × 10–12", "Hammer Curl — 2 × 6–10", "Reverse Dumbbell Curl — 2 × 8–12"],
    "Chest, Triceps & Shoulders · Volume": ["Barbell Bench Press — 3 × 6–8", "Incline Dumbbell Press — 3 × 6–8", "Cable Fly — 2 × 10–12", "Low To High Cable Fly — 2 × 10–12", "Rope Pushdown — 2 × 8–10", "Tricep Extension — 2 × 8–12", "Cable Lateral Raise — 2 × 10–12"],
    "Back & Biceps · Volume": ["Lat Pulldown — 3 × 8–10", "Seated Row — 3 × 8–10", "Face Pull — 2 × 10–12", "Dumbbell Curl — 2 × 8–10", "Reverse Dumbbell Curl — 2 × 10–12"],
    "Legs": ["Pendulum Squat — 3 × 6–8", "Leg Extension — 3 × 8–10", "Lying Hamstring Curl — 3 × 8–10", "Hip Thrust — 3 × 8–12", "Cable Crunch — 2 × 12–15", "Russian Twist — 2 × 15"],
    "Legs · Mobility": ["Goblet Squat — 2 × 10", "Side Squat — 2 × 10", "Dumbbell Romanian Deadlift — 2 × 10"],
}
# Which days to use for each lifting frequency, in weekly order (Mon, Tue, Thu, Fri, Sun, Sat).
ROUTINE_BY_DAYS = {
    2: ["Chest, Triceps & Shoulders", "Back & Biceps"],
    3: ["Chest, Triceps & Shoulders", "Back & Biceps", "Legs"],
    4: ["Chest, Triceps & Shoulders", "Back & Biceps", "Chest, Triceps & Shoulders · Volume", "Legs"],
    5: ["Chest, Triceps & Shoulders", "Back & Biceps", "Chest, Triceps & Shoulders · Volume", "Back & Biceps · Volume", "Legs"],
    6: ["Chest, Triceps & Shoulders", "Back & Biceps", "Chest, Triceps & Shoulders · Volume", "Back & Biceps · Volume", "Legs", "Legs · Mobility"],
}


LIFT_DAY_OFFSETS = [0, 1, 3, 4, 6, 5]  # Mon, Tue, Thu, Fri, Sun, Sat


def logged_routine_workouts(days: int = 6) -> list[tuple[str, list[str]]]:
    return [(title, ROUTINE_DAYS[title]) for title in ROUTINE_BY_DAYS[max(2, min(6, days))]]


def cap_sets(line: str, cap: int) -> str:
    return re.sub(r"(\d+)(\s*[×x]\s*)", lambda m: f"{min(int(m.group(1)), cap)}{m.group(2)}", line, count=1)


def add_set(line: str, maximum: int = 4) -> str:
    return re.sub(r"(\d+)(\s*[×x]\s*)", lambda m: f"{min(int(m.group(1)) + 1, max(maximum, int(m.group(1))))}{m.group(2)}", line, count=1)


def phase_lines(lines: list[str], phase: str) -> list[str]:
    """Volume across the block: Deload caps every lift at 2 sets; Build adds a set to the
    first two (main) lifts; Re-entry and Foundation use the template as written."""
    if phase == "Deload":
        return [cap_sets(line, 2) for line in lines]
    if phase == "Build":
        return [add_set(line) if index < 2 else line for index, line in enumerate(lines)]
    return list(lines)


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
    if split == "logged-routine":
        return logged_routine_workouts()
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
    if request.split == "logged-routine":
        lifting = logged_routine_workouts(request.lifting_days)
    else:
        lifting = strength_workouts(request.split, request.avoid)[:request.lifting_days]
    week_template: list[tuple[int, str, str, int, str, list[str]]] = []
    lifting_days = LIFT_DAY_OFFSETS[:len(lifting)]
    for day_offset, (title, workout) in zip(lifting_days, lifting):
        week_template.append((day_offset, "strength", title, request.available_minutes, "Strength session", [canonical_workout_line(line) for line in workout]))
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
            lines = phase_lines(workout, phase) if kind == "strength" else list(workout)
            sessions.append({"id": f"block-{uuid4().hex[:12]}", "date": scheduled.isoformat(), "kind": kind, "title": session_title, "minutes": minutes, "description": session_description, "workout": lines, "status": "planned", "locked": False})
    return {"id": f"plan-{uuid4().hex[:12]}", "name": request.name, "start_date": start.isoformat(), "goals": request.goals, "config": request.model_dump(), "phases": phases, "sessions": sessions}


BACKUP_DIR = ROOT / "backups"


def auto_backup(min_hours: float = 20, keep: int = 14) -> Path | None:
    """Daily safety copy: snapshot the database if the newest automatic backup is older than
    `min_hours`, and keep only the newest `keep` automatic ones. Manual backups are never pruned."""
    if not DATABASE.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    existing = sorted(BACKUP_DIR.glob("auto_health_hub_*.db"))
    if existing and (datetime.now().timestamp() - existing[-1].stat().st_mtime) < min_hours * 3600:
        return None
    destination = BACKUP_DIR / f"auto_health_hub_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.db"
    with sqlite3.connect(DATABASE) as source, sqlite3.connect(destination) as target:
        source.backup(target)
    for old in sorted(BACKUP_DIR.glob("auto_health_hub_*.db"))[:-keep]:
        old.unlink(missing_ok=True)
    return destination


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
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS races (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                race_date TEXT NOT NULL,
                distance TEXT NOT NULL,
                goal_seconds INTEGER,
                settings_json TEXT NOT NULL DEFAULT '{}',
                plan_json TEXT NOT NULL DEFAULT '{}',
                applied_at TEXT,
                created_at TEXT NOT NULL
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
        apply_exercise_aliases(db)


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


# Different names for the same exercise, mapped to the name used in the lifting log.
# Progress, last-weight suggestions, and planned workouts all use the right-hand name.
EXERCISE_ALIASES = {
    "triceps pressdown": "Rope Pushdown",
    "overhead cable extension": "Overhead Cable Tricep Extension",
    "overhead press": "Barbell Overhead Press",
    "pull-up or lat pulldown": "Lat Pulldown",
    "cable chest fly": "Cable Fly",
}


def canonical_exercise(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", name.strip())
    return EXERCISE_ALIASES.get(cleaned.lower(), cleaned)


def canonical_workout_line(line: str) -> str:
    name, separator, prescription = line.partition("—")
    canonical = canonical_exercise(name)
    return f"{canonical} —{prescription}" if separator else canonical


def normalize_exercise(name: str) -> str:
    return canonical_exercise(name).lower()


def retemplate_future_strength(db: sqlite3.Connection, plan_id: str) -> int:
    """Switch an active block's future lift days to the logged routine, keeping its dates and phases.

    Only planned, unlocked sessions with no logged sets change; the weekday decides the workout.
    """
    row = db.execute("SELECT plan_json FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if not row:
        return 0
    plan = json.loads(row["plan_json"])
    days = int(plan.get("config", {}).get("lifting_days") or 5)
    titles = ROUTINE_BY_DAYS[max(2, min(6, days))]
    by_weekday = {offset: titles[index] for index, offset in enumerate(LIFT_DAY_OFFSETS[:len(titles)])}
    changed = 0
    for session in db.execute(
        """SELECT * FROM sessions s WHERE source = ? AND kind = 'strength' AND status = 'planned' AND locked = 0 AND session_date >= ?
           AND NOT EXISTS (SELECT 1 FROM set_logs sl WHERE sl.session_id = s.id)""",
        (f"plan:{plan_id}", date.today().isoformat()),
    ).fetchall():
        title = by_weekday.get(date.fromisoformat(session["session_date"]).weekday())
        if not title:
            continue
        week = re.match(r"^(W\d+)\s*·", session["title"] or "")
        phase = (session["description"] or "").split(":", 1)[0].strip()
        lines = phase_lines(ROUTINE_DAYS[title], phase)
        db.execute("UPDATE sessions SET title = ?, workout_json = ? WHERE id = ?",
                   (f"{week.group(1)} · {title}" if week else title, json.dumps(lines), session["id"]))
        changed += 1
    plan.setdefault("config", {})["split"] = "logged-routine"
    db.execute("UPDATE plans SET plan_json = ? WHERE id = ?", (json.dumps(plan), plan_id))
    return changed


def apply_exercise_aliases(db: sqlite3.Connection) -> int:
    """Rename aliased exercises in planned sessions (idempotent). Logged history keeps its original text."""
    changed = 0
    for row in db.execute("SELECT id, workout_json FROM sessions WHERE status = 'planned'").fetchall():
        lines = json.loads(row["workout_json"] or "[]")
        renamed = [canonical_workout_line(line) for line in lines]
        if renamed != lines:
            db.execute("UPDATE sessions SET workout_json = ? WHERE id = ?", (json.dumps(renamed), row["id"]))
            changed += 1
    return changed


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


def line_target_weight(prescription: str) -> float | None:
    match = re.search(r"@\s*(\d+(?:\.\d+)?)\s*lb", prescription or "")
    return float(match.group(1)) if match else None


def format_workout_line(name: str, sets: int, reps: str, weight: float | None = None) -> str:
    reps = re.sub(r"\s+", "", str(reps or "8–12")).replace("-", "–")
    line = f"{canonical_exercise(name)} — {int(sets)} × {reps}"
    return f"{line} @ {weight:g} lb" if weight else line


def exercise_name_from_session(session: dict[str, Any], exercise_index: int) -> str | None:
    workout = session.get("workout", [])
    if exercise_index >= len(workout):
        return None
    return workout[exercise_index].partition("—")[0].strip()


# Lifts where the logged weight is *added* to bodyweight (0 = bodyweight only).
BODYWEIGHT_EXERCISES = {"pull-up", "chin-up", "weighted dips", "dips", "push-up"}


def is_bodyweight(name: str) -> bool:
    return canonical_exercise(name).lower() in BODYWEIGHT_EXERCISES


def estimate_1rm(name: str, weight: float, reps: int, bodyweight: float | None = None) -> float:
    """Epley estimate; bodyweight lifts count bodyweight + added load when bodyweight is known."""
    load = weight + (bodyweight or 0) if is_bodyweight(name) else weight
    return round(load * (1 + reps / 30), 1) if reps and load else 0.0


def get_setting(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def current_bodyweight(db: sqlite3.Connection) -> float | None:
    """Latest check-in weight, falling back to the bodyweight saved in Settings."""
    row = db.execute("SELECT weight_lbs FROM daily_health WHERE weight_lbs IS NOT NULL ORDER BY checkin_date DESC LIMIT 1").fetchone()
    if row:
        return float(row[0])
    saved = get_setting(db, "bodyweight_lbs")
    return float(saved) if saved else None


def rest_seconds(name: str, target_reps: str) -> int:
    """Rest between sets: heavy compounds 3 min, other compounds 2.5 min, isolation 90 s, core 60 s."""
    from catalog import EXERCISES
    kinds = {exercise.lower(): kind for exercise, _, _, kind in EXERCISES}
    kind = kinds.get(canonical_exercise(name).lower())
    if kind == "core":
        return 60
    if kind == "isolation":
        return 90
    if kind == "compound":
        return 180 if rep_bounds(target_reps)[1] <= 6 else 150
    return 120


def rep_bounds(target_reps: str) -> tuple[int, int]:
    numbers = [int(n) for n in re.findall(r"\d+", target_reps or "")]
    return (numbers[0], numbers[-1]) if numbers else (8, 12)


def weight_step(weight: float) -> float:
    return 5.0 if weight >= 50 else 2.5


def progression_suggestion(history: list[dict[str, Any]], target_reps: str) -> dict[str, Any] | None:
    """Double progression from the most recent full (non-deload) session of this exercise.

    Work at the heaviest weight used last time. If every set there reached the top of the
    rep range, add a small step; if any fell below the bottom, hold (or drop a step if
    well short); otherwise keep the weight and aim for more reps.
    """
    if not history:
        return None
    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in history:
        by_day.setdefault(row["day"], []).append(row)
    days = sorted(by_day, reverse=True)
    latest = days[0]
    is_deload = lambda day: any(r.get("deload") for r in by_day[day])
    source_day = next((day for day in days if not is_deload(day)), latest)
    sets = by_day[source_day]
    top = max(row["weight"] for row in sets)
    reps_at_top = [row["reps"] for row in sets if row["weight"] == top]
    low, high = rep_bounds(target_reps)
    last = f"{top:g} × {', '.join(str(r) for r in reps_at_top)}"
    prefix = "Back from deload. " if is_deload(latest) and source_day != latest else ""
    if top <= 0:  # bodyweight-only: progress reps
        return {"weight": 0, "reps": min(high, max(reps_at_top) + 1), "change": 0, "note": f"{prefix}Bodyweight last time ({', '.join(map(str, reps_at_top))} reps). Add a rep or some weight."}
    step = weight_step(top)
    if all(r >= high for r in reps_at_top):
        return {"weight": top + step, "reps": low, "change": step, "note": f"{prefix}Hit {high}+ reps on every set at {top:g} lb ({last}). Go up to {top + step:g} lb."}
    if min(reps_at_top) <= low - 3:
        return {"weight": max(step, top - step), "reps": low, "change": -step, "note": f"{prefix}Well short of {low} reps at {top:g} lb ({last}). Drop to {max(step, top - step):g} lb and rebuild."}
    if min(reps_at_top) < low:
        return {"weight": top, "reps": low, "change": 0, "note": f"{prefix}Below {low} reps at {top:g} lb ({last}). Stay here and get every set to {low}."}
    return {"weight": top, "reps": min(high, max(reps_at_top) + 1), "change": 0, "note": f"{prefix}Stay at {top:g} lb ({last}). Aim for {high} reps on every set, then add weight."}


def exercises_for(session: dict[str, Any], rows: list[sqlite3.Row], previous_rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Turn stable workout outline lines into a usable in-gym set logger."""
    by_exercise: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_exercise.setdefault(row["exercise_index"], []).append(dict(row))
    exercises = []
    for index, outline in enumerate(session.get("workout", [])):
        name, _, prescription = outline.partition("—")
        logged = sorted(by_exercise.get(index, []), key=lambda item: item["set_number"])
        same_exercise = [dict(row) for row in previous_rows if normalize_exercise(row["exercise_name"]) == normalize_exercise(name)]
        target_sets, target_reps, starting_guide = prescription_details(prescription.strip() or "Working sets")
        target_weight = line_target_weight(prescription)
        history = [{"day": row.get("session_date") or row["completed_at"][:10], "weight": row["weight"], "reps": row["reps"],
                    "deload": "deload" in (row.get("note") or "").lower() or (row.get("session_description") or "").startswith("Deload")}
                   for row in same_exercise]
        suggestion = progression_suggestion(history, target_reps)
        exercises.append({
            "rest_seconds": rest_seconds(name, target_reps),
            "index": index, "name": name.strip(), "prescription": prescription.strip() or "Working sets",
            "sets": logged, "target_sets": target_sets, "target_reps": target_reps, "target_weight": target_weight,
            "suggested_weight": target_weight if target_weight is not None else (suggestion["weight"] if suggestion else None),
            "suggested_reps": suggestion["reps"] if suggestion else None,
            "progression": suggestion,
            "starting_guide": suggestion["note"] if suggestion else starting_guide,
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
    recent_training_days: int = 0,
) -> dict[str, Any]:
    """Explainable rule engine. It never writes, moves, or cancels a session."""
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    has_night = lambda row: row and (row.get("sleep_seconds") is not None or row.get("hrv_ms") is not None)
    # Garmin files last night's sleep under today's date. Until it syncs, fall back to the
    # previous night (never older) and say so; anything older is only baseline history.
    current = next((row for row in daily_metrics if row.get("calendar_date") == today), None)
    stale_note = None
    if not has_night(current):
        previous = next((row for row in daily_metrics if row.get("calendar_date") == yesterday), None)
        if has_night(previous):
            current = previous
            stale_note = f"Today's sleep hasn't synced from Garmin yet, so this uses the previous night ({yesterday})."
    used_date = current.get("calendar_date") if current else None
    baseline_rows = [row for row in daily_metrics if row.get("calendar_date") not in {today, used_date}][:28]
    baseline_hrv = mean_metric(baseline_rows, "hrv_ms")
    baseline_rhr = mean_metric(baseline_rows, "resting_hr")
    score = 100
    reasons: list[str] = []
    headlines: list[str] = []  # Short, plain-language version of each triggered rule for the Today banner.
    if current and current.get("sleep_seconds") is not None:
        sleep_hours = float(current["sleep_seconds"]) / 3600
        slept = f"You slept {int(sleep_hours)}h {round((sleep_hours % 1) * 60):02d}m."
        if sleep_hours < 6:
            score -= 30; reasons.append(f"Sleep was {sleep_hours:.1f}h (under the 6h recovery floor)."); headlines.append(slept)
        elif sleep_hours < 7:
            score -= 12; reasons.append(f"Sleep was {sleep_hours:.1f}h (below the 7h target)."); headlines.append(slept)
        sleep_score = current.get("sleep_score")
        if sleep_hours >= 6 and sleep_score is not None and float(sleep_score) < 50:
            score -= 8; reasons.append(f"Garmin sleep score was {round(float(sleep_score))} (poor quality)."); headlines.append("Sleep quality was poor.")
    hrv_flagged = False
    if current and baseline_hrv and current.get("hrv_ms") is not None and float(current["hrv_ms"]) <= baseline_hrv * 0.85:
        drop = round((1 - float(current["hrv_ms"]) / baseline_hrv) * 100)
        score -= 20; reasons.append(f"HRV is {drop}% below your rolling baseline."); headlines.append(f"HRV is {drop}% below baseline.")
        hrv_flagged = True
    hrv_status = str((current or {}).get("hrv_status") or "").upper()
    if not hrv_flagged and hrv_status in {"LOW", "POOR"}:
        score -= 15; reasons.append("Garmin HRV status is low."); headlines.append("HRV status is low.")
    elif not hrv_flagged and hrv_status == "UNBALANCED":
        score -= 8; reasons.append("Garmin HRV status is unbalanced."); headlines.append("HRV status is unbalanced.")
    wake_battery = (current or {}).get("body_battery_at_wake")
    if wake_battery is not None and float(wake_battery) < 25:
        score -= 12; reasons.append(f"Body Battery was only {round(float(wake_battery))} at wake."); headlines.append(f"Body Battery woke at {round(float(wake_battery))}.")
    elif wake_battery is not None and float(wake_battery) < 40:
        score -= 6; reasons.append(f"Body Battery was {round(float(wake_battery))} at wake (below 40).")
    if recent_training_days >= 3:
        score -= 5; reasons.append("You trained each of the last 3 days."); headlines.append("Three training days in a row.")
    if current and baseline_rhr and current.get("resting_hr") is not None and float(current["resting_hr"]) >= baseline_rhr * 1.08:
        rise = round((float(current["resting_hr"]) / baseline_rhr - 1) * 100)
        score -= 15; reasons.append(f"Resting HR is {rise}% above your rolling baseline."); headlines.append(f"Resting HR is {rise}% above baseline.")
    if current and current.get("stress_avg") is not None and float(current["stress_avg"]) >= 65:
        score -= 10; reasons.append("Garmin stress average is elevated (65 or above)."); headlines.append("Stress is running high.")
    if manual_health:
        if int(manual_health.get("soreness", 0)) >= 8:
            score -= 30; reasons.append("Manual soreness is 8/10 or higher."); headlines.append("You logged high soreness.")
        elif int(manual_health.get("soreness", 0)) >= 6:
            score -= 8; reasons.append("Manual soreness is 6/10 or higher."); headlines.append("You logged some soreness.")
        if int(manual_health.get("energy", 10)) <= 3:
            score -= 25; reasons.append("Manual energy is 3/10 or lower."); headlines.append("You logged low energy.")
        elif int(manual_health.get("energy", 10)) <= 5:
            score -= 8; reasons.append("Manual energy is 5/10 or lower."); headlines.append("You logged lowish energy.")
    score = max(0, score)
    planned = [session for session in sessions if session.get("status") == "planned"]
    planned.sort(key=lambda session: (0 if session.get("date") == today else 1, session.get("date", ""), session.get("title", "")))
    target = next((session for session in planned if session.get("locked")), None) or (planned[0] if planned else None)
    target_name = re.sub(r"^W\d+\s*·\s*", "", target["title"]) if target else "your next training block"
    adjusted_sets: int | None = None
    adjusted_minutes: int | None = None
    if score <= 50:
        title, mode = "Recovery-first day", "recovery"
        guidance = "Take 20–30 easy minutes of walking and mobility, or rest. Do not add catch-up volume today."
        action = "Take an easy walk or mobility today, or rest."
    elif score <= 75:
        title, mode = "Use the short version", "shorten"
        if target and target.get("kind") == "run":
            adjusted_minutes = max(20, round(target.get("minutes", 30) * 0.6))
            guidance = f"Keep {target_name} easy and cap it around {adjusted_minutes} minutes. Skip intensity."
            action = f"Keep the run easy and cap it around {adjusted_minutes} min."
        elif target:
            # Keep every exercise for consistency; trim volume by capping each at two working sets.
            adjusted_sets = 2
            adjusted_minutes = max(25, round(target.get("minutes", 45) * 0.6))
            guidance = f"Keep every lift in {target_name} but cap each at 2 working sets (about {adjusted_minutes} minutes). Leave 2–3 reps in reserve."
            action = f"Keep the lifts, cut each to 2 sets (about {adjusted_minutes} min)."
        else:
            guidance = "Keep activity easy today and reassess after a check-in tomorrow."
            action = "Keep activity easy today."
    elif target:
        title, mode = "Planned session is appropriate", "train"
        guidance = f"Do {target_name} as planned. Keep the opening working sets controlled and adjust load by effort."
        action = "Train as planned."
    else:
        title, mode = "No session needs moving", "rest"
        guidance = "You have no planned session in the next week. A relaxed walk or mobility is enough today."
        action = "No session planned. An easy walk is plenty."
    if not reasons:
        reasons.append("No recovery flag crossed a conservative threshold; recommendation is based on the planned session.")
    if stale_note:
        reasons.insert(0, stale_note)
    elif not current:
        reasons.insert(0, "No Garmin sleep or HRV for last night yet; the score uses only your check-in.")
    return {
        "engine": "deterministic-v2", "score": score, "mode": mode, "title": title, "guidance": guidance,
        "data_date": used_date, "data_is_stale": bool(stale_note),
        "headline": " ".join([*headlines[:1], action]),
        "adjusted_sets": adjusted_sets, "adjusted_minutes": adjusted_minutes,
        "target_session": {"id": target["id"], "title": target["title"], "date": target.get("date"), "kind": target.get("kind"), "minutes": target.get("minutes"), "locked": bool(target.get("locked"))} if target else None,
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
    try:
        auto_backup()
    except (OSError, sqlite3.Error):
        pass  # a failed backup must never stop the app from starting


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
    return deterministic_training_recommendation(
        [row_to_session(row) for row in session_rows], [dict(row) for row in metric_rows], dict(manual) if manual else None,
        recent_training_days=len(trained),
    )


def workout_lines(session: dict[str, Any], cap_sets: int | None = None) -> list[dict[str, Any]]:
    """Split 'Exercise — 3 × 8–12' lines into display rows, optionally capping sets."""
    rows = []
    for line in session.get("workout", []):
        name, _, prescription = line.partition("—")
        match = re.search(r"(\d+)\s*[×x]\s*(\d+(?:\s*[–-]\s*\d+)?)", prescription)
        sets = int(match.group(1)) if match else None
        reps = re.sub(r"\s+", "", match.group(2)).replace("-", "–") if match else None
        shown_sets = min(sets, cap_sets) if sets and cap_sets else sets
        rows.append({"name": name.strip(), "sets": shown_sets, "planned_sets": sets, "reps": reps, "note": None if match else prescription.strip() or None})
    return rows


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


def garmin_activity_kind(activity_type: str) -> str | None:
    lowered = (activity_type or "").lower()
    if "run" in lowered:
        return "run"
    if "strength" in lowered:
        return "strength"
    return None


def match_garmin_activities(db: sqlite3.Connection) -> int:
    """Pair each unlinked Garmin activity with the same-day session of the same kind.

    A still-planned session is marked completed using Garmin's duration/HR, so a
    workout recorded on the watch and planned here appears once, not twice. A
    session already completed in Health Hub is only linked. Activities with no
    same-day, same-kind session stay as standalone history.
    """
    today = date.today().isoformat()
    rows = db.execute(
        "SELECT * FROM imported_activities WHERE linked_session_id IS NULL AND source = 'Garmin API' AND activity_date <= ? ORDER BY activity_date, imported_at",
        (today,),
    ).fetchall()
    matched = 0
    # Pass 1 links same-day pairs for every activity; pass 2 lets leftovers take a planned
    # session shifted by one day, so a shifted activity never steals another day's match.
    leftovers = []
    for shifted, candidates in ((False, rows), (True, leftovers)):
        for activity in list(candidates):
            matched += _link_activity(db, activity, shifted, leftovers)
    return matched


def _link_activity(db: sqlite3.Connection, activity: sqlite3.Row, shifted: bool, leftovers: list) -> int:
    kind = garmin_activity_kind(activity["activity_type"])
    if not kind:
        return 0
    if not shifted:
        session = db.execute(
            """SELECT * FROM sessions WHERE session_date = ? AND kind = ? AND status IN ('planned', 'completed')
               ORDER BY CASE status WHEN 'completed' THEN 0 ELSE 1 END, title LIMIT 1""",
            (activity["activity_date"], kind),
        ).fetchone()
        if not session:
            leftovers.append(activity)
            return 0
    else:
        day = date.fromisoformat(activity["activity_date"])
        session = db.execute(
            """SELECT * FROM sessions s WHERE kind = ? AND status = 'planned' AND session_date IN (?, ?)
               AND NOT EXISTS (SELECT 1 FROM imported_activities a WHERE a.linked_session_id = s.id)
               ORDER BY session_date, title LIMIT 1""",
            (kind, (day - timedelta(days=1)).isoformat(), (day + timedelta(days=1)).isoformat()),
        ).fetchone()
        if not session:
            return 0
    if session["status"] == "planned":
        metrics: dict[str, Any] = {
            "source": "Garmin (matched automatically)", "garmin_activity_id": activity["id"],
            "duration_minutes": activity["duration_minutes"], "average_hr": activity["average_hr"], "max_hr": activity["max_hr"],
        }
        if kind == "run" and activity["distance_miles"]:
            metrics["distance_miles"] = activity["distance_miles"]
            if activity["duration_minutes"]:
                metrics["pace_seconds_per_mile"] = round(activity["duration_minutes"] * 60 / activity["distance_miles"])
        note = f"Completed from a Garmin activity on {activity['activity_date']}." if shifted else "Completed from a Garmin activity on the same day."
        db.execute("UPDATE sessions SET status = 'completed' WHERE id = ?", (session["id"],))
        db.execute(
            "INSERT INTO logs (session_id, completed_at, minutes, notes, metrics_json) VALUES (?, ?, ?, ?, ?)",
            (session["id"], datetime.now().astimezone().isoformat(timespec="seconds"), round(activity["duration_minutes"] or session["minutes"]), note, json.dumps(metrics)),
        )
    db.execute("UPDATE imported_activities SET linked_session_id = ? WHERE id = ?", (session["id"], activity["id"]))
    return 1


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
