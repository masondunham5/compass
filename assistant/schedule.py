"""Rescheduling: move one workout, skip it, swap two, or push the plan back — with a preview and one-step undo.

Rules (shared by every operation):
- Only planned sessions move. Completed, cancelled, and sessions with logged sets stay put.
- Locked sessions never move (that's what locking is for).
- Race day never moves; race-plan runs can shift but never onto or past race day.
- Every applied change records the previous dates so it can be undone in one step.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta
from typing import Any

UNDO_KEY = "schedule_undo"


def _movable(db: sqlite3.Connection, row: sqlite3.Row) -> str | None:
    """Return why a session can't move, or None if it can."""
    if row["status"] != "planned":
        return f"already {row['status']}"
    if row["locked"]:
        return "locked"
    if str(row["title"]).startswith("Race:"):
        return "race day"
    if db.execute("SELECT 1 FROM set_logs WHERE session_id = ? LIMIT 1", (row["id"],)).fetchone():
        return "has logged sets"
    return None


def _race_limit(db: sqlite3.Connection, row: sqlite3.Row) -> str | None:
    """Race-plan runs may not land on or after their race day."""
    source = row["source"] or ""
    if not source.startswith("race:"):
        return None
    race = db.execute("SELECT race_date FROM races WHERE id = ?", (source.split(":", 1)[1],)).fetchone()
    return race[0] if race else None


def plan_moves(db: sqlite3.Connection, moves: list[tuple[str, str]]) -> dict[str, Any]:
    """Validate (session_id, new_date) pairs. Returns what would change and what was skipped, and why."""
    changes, skipped = [], []
    for session_id, new_date in moves:
        row = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            skipped.append({"id": session_id, "reason": "not found"})
            continue
        reason = _movable(db, row)
        race_day = _race_limit(db, row)
        if not reason and race_day and new_date >= race_day:
            reason = "would land on or after race day"
        item = {"id": row["id"], "title": row["title"], "kind": row["kind"], "from": row["session_date"], "to": new_date}
        if reason:
            skipped.append({**item, "reason": reason})
        elif row["session_date"] != new_date:
            changes.append(item)
    changes.sort(key=lambda change: (change["from"], change["title"]))
    from assistant.availability import day_notes  # calendar heads-ups for the days sessions land on
    busy = day_notes(db, [change["to"] for change in changes if change["kind"] in ("strength", "run")])
    from assistant.runprep import spacing_issues  # long run after legs, hard days back to back, …
    return {"changes": changes, "skipped": skipped, "conflicts": _conflicts(db, changes), "busy_days": busy,
            "spacing": spacing_issues(db, changes)}


def _conflicts(db: sqlite3.Connection, changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Days that would end up with two or more strength sessions (a heads-up, not a blocker)."""
    moving = {change["id"]: change["to"] for change in changes}
    counts: dict[str, list[str]] = {}
    days = {change["to"] for change in changes}
    if not days:
        return []
    placeholders = ",".join("?" for _ in days)
    for row in db.execute(f"SELECT id, title, session_date, kind FROM sessions WHERE status = 'planned' AND kind = 'strength' AND (session_date IN ({placeholders}) OR id IN ({','.join('?' for _ in moving)}))",
                          (*days, *moving)):
        day = moving.get(row["id"], row["session_date"])
        if day in days:
            counts.setdefault(day, []).append(row["title"])
    return [{"date": day, "sessions": titles} for day, titles in sorted(counts.items()) if len(titles) > 1]


def apply_moves(db: sqlite3.Connection, plan: dict[str, Any]) -> dict[str, Any]:
    for change in plan["changes"]:
        db.execute("UPDATE sessions SET session_date = ? WHERE id = ?", (change["to"], change["id"]))
    undo = [{"id": change["id"], "date": change["from"]} for change in plan["changes"]]
    db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
               (UNDO_KEY, json.dumps({"changes": undo, "summary": summarize(plan)})))
    return {**plan, "summary": summarize(plan)}


def summarize(plan: dict[str, Any]) -> str:
    count = len(plan["changes"])
    if not count:
        return "Nothing to move."
    kept = f" {len(plan['skipped'])} stayed put." if plan["skipped"] else ""
    return f"Moved {count} session{'s' if count != 1 else ''}.{kept}"


def move_session(db: sqlite3.Connection, session_id: str, new_date: str) -> dict[str, Any]:
    return plan_moves(db, [(session_id, new_date)])


def push_back(db: sqlite3.Connection, from_date: str, days: int, include_runs: bool = True) -> dict[str, Any]:
    """Shift every movable planned session on/after `from_date` later by `days`, keeping their spacing."""
    kinds = ("strength", "run", "mobility") if include_runs else ("strength", "mobility")
    rows = db.execute(
        f"SELECT id, session_date FROM sessions WHERE status = 'planned' AND session_date >= ? AND kind IN ({','.join('?' for _ in kinds)}) ORDER BY session_date",
        (from_date, *kinds),
    ).fetchall()
    moves = [(row["id"], (date.fromisoformat(row["session_date"]) + timedelta(days=days)).isoformat()) for row in rows]
    return plan_moves(db, moves)


def skip_session(db: sqlite3.Connection, session_id: str, mode: str) -> dict[str, Any]:
    """mode: 'tomorrow' (move just this one), 'push' (this one and everything after shift one day)."""
    row = db.execute("SELECT session_date FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row:
        return {"changes": [], "skipped": [{"id": session_id, "reason": "not found"}], "conflicts": []}
    next_day = (date.fromisoformat(row[0]) + timedelta(days=1)).isoformat()
    if mode == "push":
        return push_back(db, row[0], 1)
    return move_session(db, session_id, next_day)


def workout_type(title: str) -> str:
    """'W5 · Chest, Triceps & Shoulders · Volume' -> 'chest, triceps & shoulders' (week tag and variant dropped)."""
    parts = [part.strip() for part in str(title).split("·") if part.strip() and not re.fullmatch(r"W\d+", part.strip())]
    return (parts[0] if parts else str(title)).lower()


def swap_options(db: sqlite3.Connection, session_id: str, days: int = 10) -> list[dict[str, Any]]:
    """Planned workouts within `days` of this one that it could trade days with."""
    row = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row:
        return []
    around = date.fromisoformat(row["session_date"])
    first = max(date.today(), around - timedelta(days=days)).isoformat()
    rows = db.execute(
        "SELECT id, session_date, kind, title FROM sessions WHERE status = 'planned' AND id != ? AND session_date BETWEEN ? AND ? "
        "AND session_date != ? AND kind IN ('strength', 'run', 'mobility') ORDER BY session_date, kind",
        (session_id, first, (around + timedelta(days=days)).isoformat(), row["session_date"]),
    ).fetchall()
    return [dict(r) for r in rows]


def swap_sessions(db: sqlite3.Connection, session_id: str, other_id: str, repeat: int = 0) -> dict[str, Any]:
    """Trade the days of two workouts. With `repeat`, also trade the next `repeat` pairs of the same
    two workout types (e.g. the following Chest / Back days), so the spacing stays consistent."""
    first = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    second = db.execute("SELECT * FROM sessions WHERE id = ?", (other_id,)).fetchone()
    if not first or not second:
        return {"changes": [], "skipped": [{"id": session_id if not first else other_id, "reason": "not found"}], "conflicts": [], "busy_days": []}
    pairs, used = [(first, second)], {first["id"], second["id"]}
    types = (workout_type(first["title"]), workout_type(second["title"]))
    after = max(first["session_date"], second["session_date"])
    for _ in range(max(0, min(repeat, 8))):
        found = []
        for wanted in types:
            row = next((r for r in db.execute("SELECT * FROM sessions WHERE status = 'planned' AND session_date > ? ORDER BY session_date", (after,))
                        if r["id"] not in used and workout_type(r["title"]) == wanted), None)
            found.append(row)
        if not all(found):
            break
        pairs.append((found[0], found[1]))
        used.update(row["id"] for row in found)
        after = max(found[0]["session_date"], found[1]["session_date"])
    moves, blocked = [], []
    for a, b in pairs:
        reason = _movable(db, a) or _movable(db, b)
        if reason:  # a swap is all or nothing for each pair
            blocked += [{"id": r["id"], "title": r["title"], "kind": r["kind"], "from": r["session_date"], "to": r["session_date"], "reason": reason} for r in (a, b)]
            continue
        moves += [(a["id"], b["session_date"]), (b["id"], a["session_date"])]
    plan = plan_moves(db, moves)
    plan["skipped"] = blocked + plan["skipped"]
    return plan


def undo_last(db: sqlite3.Connection) -> dict[str, Any]:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (UNDO_KEY,)).fetchone()
    if not row:
        return {"restored": 0, "summary": "Nothing to undo."}
    saved = json.loads(row[0])
    restored = 0
    for change in saved["changes"]:
        restored += db.execute("UPDATE sessions SET session_date = ? WHERE id = ? AND status = 'planned'", (change["date"], change["id"])).rowcount
    db.execute("DELETE FROM settings WHERE key = ?", (UNDO_KEY,))
    return {"restored": restored, "summary": f"Undid: {saved.get('summary', 'last change')}"}


def last_change(db: sqlite3.Connection) -> str | None:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (UNDO_KEY,)).fetchone()
    return json.loads(row[0]).get("summary") if row else None
