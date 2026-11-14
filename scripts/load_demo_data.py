"""Create an anonymous, portfolio-safe Health Hub demo dataset.

Run from the project root:
    python scripts/load_demo_data.py

The script refuses to touch an existing database. Delete only the demo database
yourself if you deliberately want to recreate it.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402


def main() -> None:
    if app.DATABASE.exists():
        print(f"Stopped: {app.DATABASE} already exists. Your local data was not changed.")
        return

    app.initialize()
    today = date.today()
    sessions = [
        (
            "demo-push", today.isoformat(), "strength", "Push · Strength", 60,
            "A controlled push session with room to adjust loading.",
            [
                "Barbell Bench Press — 4 × 5–8",
                "Incline Dumbbell Press — 3 × 8–12",
                "Cable Lateral Raise — 3 × 12–15",
                "Triceps Pressdown — 3 × 10–15",
            ],
        ),
        (
            "demo-run", (today + timedelta(days=1)).isoformat(), "run", "Easy aerobic run", 38,
            "Easy conversational effort. Keep the pace relaxed.", [],
        ),
        (
            "demo-pull", (today + timedelta(days=2)).isoformat(), "strength", "Back & Biceps", 55,
            "Pull-focused session with enough volume for progress.",
            ["Lat Pulldown — 4 × 6–10", "Chest-Supported Row — 3 × 8–12", "Cable Curl — 3 × 10–15"],
        ),
    ]
    with app.connection() as db:
        for session_id, session_date, kind, title, minutes, description, workout in sessions:
            db.execute(
                """INSERT INTO sessions
                   (id, session_date, kind, title, minutes, description, workout_json, status, locked, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'planned', 0, 'portfolio-demo')""",
                (session_id, session_date, kind, title, minutes, description, json.dumps(workout)),
            )
        db.execute(
            """INSERT INTO daily_health
               (checkin_date, sleep_hours, sleep_quality, energy, soreness, stress, weight_lbs, protein_goal_met, notes, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                today.isoformat(), 7.5, 8, 7, 4, 4, 175.0, 1, "Demo check-in",
                datetime.now().astimezone().isoformat(timespec="seconds"),
            ),
        )
        for offset in range(7):
            day = today - timedelta(days=offset)
            db.execute(
                """INSERT INTO garmin_daily_metrics
                   (calendar_date, steps, active_calories, total_calories, resting_hr, hrv_ms, sleep_seconds, stress_avg, body_battery, source_json, synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)""",
                (
                    day.isoformat(), 6200 + offset * 430, 430 + offset * 15, 2400 + offset * 20,
                    52 + (offset % 3), 54 + (offset * 1.5), 7 * 3600 + offset * 180,
                    28 + offset, 72 - offset, datetime.now().astimezone().isoformat(timespec="seconds"),
                ),
            )
        db.execute(
            """INSERT INTO garmin_sync_log (started_at, finished_at, status, activities_imported, metrics_updated, message)
               VALUES (?, ?, 'success', 2, 7, 'Portfolio demo sync.')""",
            (datetime.now().astimezone().isoformat(timespec="seconds"), datetime.now().astimezone().isoformat(timespec="seconds")),
        )
    print("Created anonymous demo data. Start the app and open http://127.0.0.1:8765.")


if __name__ == "__main__":
    main()
