"""Work schedule: recurring weekly shifts (usually school terms) and one-off shifts (often breaks).

Terms say whether a date is a school term or a break. A recurring shift applies when its
`applies` matches the date's term kind ("always" matches both). With no terms defined at all,
every date counts as a school term. One-off shifts apply on their own date regardless.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from typing import Any

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
CLOCK = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class WorkError(ValueError):
    pass


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS terms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, kind TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS work_shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            weekday INTEGER, shift_date TEXT, start TEXT NOT NULL, end TEXT NOT NULL,
            applies TEXT NOT NULL DEFAULT 'school', note TEXT NOT NULL DEFAULT ''
        );
        """
    )


def term_kind(db: sqlite3.Connection, day: date) -> str:
    if not db.execute("SELECT 1 FROM terms LIMIT 1").fetchone():
        return "school"
    row = db.execute("SELECT kind FROM terms WHERE start_date <= ? AND end_date >= ? ORDER BY start_date DESC LIMIT 1",
                     (day.isoformat(), day.isoformat())).fetchone()
    return row[0] if row else "break"


def shifts_on(db: sqlite3.Connection, day: date) -> list[dict[str, Any]]:
    kind = term_kind(db, day)
    rows = db.execute(
        """SELECT * FROM work_shifts WHERE shift_date = ?
           OR (shift_date IS NULL AND weekday = ? AND applies IN (?, 'always')) ORDER BY start""",
        (day.isoformat(), day.weekday(), kind),
    ).fetchall()
    return [{"id": row["id"], "start": row["start"], "end": row["end"], "title": row["note"] or "Work", "recurring": row["shift_date"] is None} for row in rows]


def validate_shift(start: str, end: str) -> None:
    if not (CLOCK.match(start or "") and CLOCK.match(end or "")):
        raise WorkError("Use 24-hour times like 09:00 and 17:30.")
    if end <= start:
        raise WorkError("A shift has to end after it starts (overnight shifts aren't supported yet).")


def add_shift(db: sqlite3.Connection, start: str, end: str, weekday: int | None = None, shift_date: str | None = None,
              applies: str = "school", note: str = "") -> int:
    validate_shift(start, end)
    if (weekday is None) == (shift_date is None):
        raise WorkError("Pick either a weekday (repeats) or a single date.")
    if weekday is not None and not 0 <= weekday <= 6:
        raise WorkError("Weekday must be Monday–Sunday.")
    if shift_date:
        date.fromisoformat(shift_date)
    if applies not in {"school", "break", "always"}:
        raise WorkError("Choose school terms, breaks, or always.")
    return db.execute("INSERT INTO work_shifts (weekday, shift_date, start, end, applies, note) VALUES (?, ?, ?, ?, ?, ?)",
                      (weekday, shift_date, start, end, applies, note.strip())).lastrowid


def add_term(db: sqlite3.Connection, name: str, kind: str, start_date: str, end_date: str) -> int:
    if kind not in {"school", "break"}:
        raise WorkError("A term is either a school term or a break.")
    if date.fromisoformat(end_date) < date.fromisoformat(start_date):
        raise WorkError("The term ends before it starts.")
    return db.execute("INSERT INTO terms (name, kind, start_date, end_date) VALUES (?, ?, ?, ?)", (name.strip() or kind.title(), kind, start_date, end_date)).lastrowid


def schedule(db: sqlite3.Connection) -> dict[str, Any]:
    shifts = [dict(row) for row in db.execute("SELECT * FROM work_shifts ORDER BY shift_date IS NOT NULL, weekday, shift_date, start")]
    for shift in shifts:
        shift["weekday_name"] = WEEKDAYS[shift["weekday"]] if shift["weekday"] is not None else None
    return {"terms": [dict(row) for row in db.execute("SELECT * FROM terms ORDER BY start_date")], "shifts": shifts,
            "today_kind": term_kind(db, date.today())}
