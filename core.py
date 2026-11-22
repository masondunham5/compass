"""Paths, local .env loading, SQLite connection/schema, settings, and automatic backups."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

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


def monday_on_or_after(day: date) -> date:
    return day + timedelta(days=(7 - day.weekday()) % 7)


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
        from planning import apply_exercise_aliases  # local imports: these modules depend on core
        from garmin_sync import ensure_schema
        from health import ensure_health_schema
        ensure_schema(db)  # Garmin metric columns + raw table exist even before the first sync
        ensure_health_schema(db)
        from assistant.calendars import ensure_schema as ensure_calendar_schema
        ensure_calendar_schema(db)
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
