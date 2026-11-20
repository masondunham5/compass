"""Import past lifting sets from a CSV into Health Hub.

CSV columns: date,workout,exercise,set,weight_lbs,reps,rpe,notes  (one row per set)

    ..\\.venv\\Scripts\\python.exe scripts\\import_lifting_history.py path\\to\\lifting_history.csv --dry-run
    ..\\.venv\\Scripts\\python.exe scripts\\import_lifting_history.py path\\to\\lifting_history.csv

Each date becomes one completed workout. If that date already has a planned or
completed strength session (from the active block, or completed from Garmin),
the sets go into that session and it is renamed to what was actually done, so
nothing appears twice. Otherwise a new completed session is created. Garmin
activities are then matched to the new sessions. Safe to re-run: sessions this
script wrote are replaced, and sessions with sets logged in the app are skipped.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402

IMPORT_MARKER = "Imported from lifting history"


def _num(value: str) -> float | None:
    try:
        return float(value) if value.strip() else None
    except ValueError:
        return None


def _clean_note(note: str) -> str:
    parts = [part.strip() for part in (note or "").split(";")]
    return "; ".join(part for part in parts if part and part.lower() != "date approximate")


def read_workouts(path: Path) -> list[dict[str, Any]]:
    """Group CSV rows into one workout per date, exercises in first-seen order."""
    by_date: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for line, row in enumerate(csv.DictReader(handle), start=2):
            day = (row.get("date") or "").strip()
            exercise = (row.get("exercise") or "").strip()
            weight, reps = _num(row.get("weight_lbs") or ""), _num(row.get("reps") or "")
            if not day or not exercise or weight is None or reps is None:
                print(f"  skipped line {line}: missing date, exercise, weight, or reps")
                continue
            workout = by_date.setdefault(day, {"date": day, "names": Counter(), "exercises": OrderedDict(), "approximate": False})
            workout["names"][(row.get("workout") or "Workout").strip()] += 1
            workout["approximate"] |= "approximate" in (row.get("notes") or "").lower()
            workout["exercises"].setdefault(exercise, []).append({
                "set_number": int(_num(row.get("set") or "") or len(workout["exercises"].get(exercise, [])) + 1),
                "weight": weight, "reps": int(reps), "rpe": _num(row.get("rpe") or ""), "note": _clean_note(row.get("notes") or ""),
            })
    workouts = []
    for workout in by_date.values():
        title = workout["names"].most_common(1)[0][0]
        lines = []
        for name, sets in workout["exercises"].items():
            low, high = min(s["reps"] for s in sets), max(s["reps"] for s in sets)
            lines.append(f"{name} — {len(sets)} × {low}–{high}" if low != high else f"{name} — {len(sets)} × {low}")
        workouts.append({**workout, "title": title, "workout_lines": lines, "set_count": sum(len(s) for s in workout["exercises"].values())})
    return workouts


def import_workouts(workouts: list[dict[str, Any]], dry_run: bool = False) -> dict[str, Any]:
    app.initialize()
    summary: dict[str, Any] = {"merged": [], "created": [], "skipped": [], "sets": 0}
    with app.connection() as db:
        for workout in workouts:
            day = workout["date"]
            existing = db.execute(
                """SELECT * FROM sessions WHERE session_date = ? AND kind = 'strength' AND status IN ('planned', 'completed')
                   ORDER BY CASE WHEN description LIKE ? THEN 0 WHEN status = 'completed' THEN 1 ELSE 2 END, title LIMIT 1""",
                (day, f"{IMPORT_MARKER}%"),
            ).fetchone()
            if existing:
                already_imported = (existing["description"] or "").startswith(IMPORT_MARKER)
                has_app_sets = db.execute("SELECT 1 FROM set_logs WHERE session_id = ? LIMIT 1", (existing["id"],)).fetchone()
                if has_app_sets and not already_imported:
                    summary["skipped"].append(f"{day} {existing['title']} (already has sets logged in the app)")
                    continue
                session_id = existing["id"]
                if already_imported:
                    found = re.search(r"\(planned: (.*?)\)", existing["description"] or "")
                    original = found.group(1) if found else None
                else:
                    original = existing["title"]
                description = f"{IMPORT_MARKER} (planned: {original})" if original and original != workout["title"] else IMPORT_MARKER
                summary["merged"].append(f"{day} {workout['title']}" + (f" → replaces planned '{original}'" if original else ""))
            else:
                session_id = f"history-{day}"
                description = IMPORT_MARKER
                summary["created"].append(f"{day} {workout['title']}")
            if workout["approximate"]:
                description += " · date approximate"
            summary["sets"] += workout["set_count"]
            if dry_run:
                continue
            garmin_minutes = db.execute(
                "SELECT SUM(duration_minutes) FROM imported_activities WHERE activity_date = ? AND lower(activity_type) LIKE '%strength%'", (day,)
            ).fetchone()[0]
            minutes = max(10, round(garmin_minutes or workout["set_count"] * 3))
            db.execute(
                """INSERT INTO sessions (id, session_date, kind, title, minutes, description, workout_json, status, locked, source)
                   VALUES (?, ?, 'strength', ?, ?, ?, ?, 'completed', 0, 'history-import')
                   ON CONFLICT(id) DO UPDATE SET title=excluded.title, minutes=excluded.minutes, description=excluded.description,
                     workout_json=excluded.workout_json, status='completed'""",
                (session_id, day, workout["title"], minutes, description, json.dumps(workout["workout_lines"])),
            )
            db.execute("DELETE FROM set_logs WHERE session_id = ?", (session_id,))
            for index, sets in enumerate(workout["exercises"].values()):
                for entry in sets:
                    db.execute(
                        """INSERT INTO set_logs (session_id, exercise_index, set_number, weight, reps, rpe, note, completed_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(session_id, exercise_index, set_number) DO UPDATE SET weight=excluded.weight, reps=excluded.reps,
                             rpe=excluded.rpe, note=excluded.note, completed_at=excluded.completed_at""",
                        (session_id, index, entry["set_number"], entry["weight"], entry["reps"], entry["rpe"], entry["note"], f"{day}T12:00:00"),
                    )
            if not db.execute("SELECT 1 FROM logs WHERE session_id = ? LIMIT 1", (session_id,)).fetchone():
                db.execute(
                    "INSERT INTO logs (session_id, completed_at, minutes, notes, metrics_json) VALUES (?, ?, ?, ?, ?)",
                    (session_id, f"{day}T12:00:00", minutes, IMPORT_MARKER, json.dumps({"source": "history-import"})),
                )
        if not dry_run:
            summary["garmin_matched"] = app.match_garmin_activities(db)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Import past lifting sets from CSV.")
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing anything.")
    args = parser.parse_args()
    workouts = read_workouts(args.csv_path)
    summary = import_workouts(workouts, dry_run=args.dry_run)
    print(f"{'DRY RUN — nothing written. ' if args.dry_run else ''}{len(workouts)} workouts, {summary['sets']} sets "
          f"({workouts[0]['date'] if workouts else '—'} to {workouts[-1]['date'] if workouts else '—'}).")
    print(f"Merged into existing sessions: {len(summary['merged'])}")
    for line in summary["merged"]:
        print("  ", line)
    print(f"New completed sessions: {len(summary['created'])}")
    if summary["skipped"]:
        print(f"Skipped: {len(summary['skipped'])}")
        for line in summary["skipped"]:
            print("  ", line)
    if "garmin_matched" in summary:
        print(f"Garmin activities newly matched: {summary['garmin_matched']}")


if __name__ == "__main__":
    main()
