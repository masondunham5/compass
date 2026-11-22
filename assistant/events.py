"""Your own calendar events (one-off or repeating) and personal to-dos.

Events are fixed time like classes or shifts: the planner works around them and they go to
the Compass calendar in Outlook. To-dos have a time estimate (and optionally a deadline);
the planner finds time for them like schoolwork.

Times are local wall-clock minutes after midnight, like plan blocks.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from typing import Any

REPEATS = ("none", "daily", "weekdays", "weekly", "biweekly", "monthly")
MAX_OCCURRENCE_SCAN = 800  # days; guards the expansion loop


class EventError(ValueError):
    """A readable problem with an event or to-do (shown to the user)."""


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS my_events (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            start_date TEXT NOT NULL,
            start INTEGER NOT NULL DEFAULT 0,
            end INTEGER NOT NULL DEFAULT 0,
            all_day INTEGER NOT NULL DEFAULT 0,
            repeat TEXT NOT NULL DEFAULT 'none',
            repeat_until TEXT,
            skip_dates TEXT NOT NULL DEFAULT '[]',
            location TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            busy INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS todos (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            minutes INTEGER NOT NULL DEFAULT 30,
            due_date TEXT,
            due_time TEXT,
            notes TEXT NOT NULL DEFAULT '',
            done INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            done_at TEXT
        );
        """
    )


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _iso_at(day: date, minutes: int) -> str:
    moment = datetime.combine(day, datetime.min.time()) + timedelta(minutes=int(minutes))
    return moment.isoformat(timespec="minutes")


# ---------------------------------------------------------------- events

def _validate_event(fields: dict[str, Any]) -> dict[str, Any]:
    title = str(fields.get("title", "")).strip()
    if not title:
        raise EventError("Give the event a name.")
    try:
        start_date = date.fromisoformat(str(fields["start_date"]))
    except (KeyError, ValueError) as error:
        raise EventError("Pick a date for the event.") from error
    all_day = bool(fields.get("all_day"))
    start, end = (0, 24 * 60) if all_day else (int(fields.get("start", -1)), int(fields.get("end", -1)))
    if not all_day and not (0 <= start < end <= 24 * 60):
        raise EventError("The event has to end after it starts, on the same day.")
    repeat = str(fields.get("repeat") or "none")
    if repeat not in REPEATS:
        raise EventError("Unknown repeat option.")
    until = fields.get("repeat_until") or None
    if until:
        try:
            if date.fromisoformat(str(until)) < start_date:
                raise EventError("“Repeat until” is before the first date.")
        except ValueError as error:
            raise EventError("Use a valid “repeat until” date.") from error
    return {"title": title[:120], "start_date": start_date.isoformat(), "start": start, "end": end, "all_day": int(all_day),
            "repeat": repeat, "repeat_until": str(until) if until and repeat != "none" else None,
            "location": str(fields.get("location") or "")[:120], "notes": str(fields.get("notes") or "")[:1000],
            "busy": int(fields.get("busy", True) not in (False, 0, "0"))}


def add_event(db: sqlite3.Connection, fields: dict[str, Any]) -> dict[str, Any]:
    values = _validate_event(fields)
    event_id, stamp = uuid.uuid4().hex[:12], _now()
    db.execute(
        """INSERT INTO my_events (id, title, start_date, start, end, all_day, repeat, repeat_until, location, notes, busy, created_at, updated_at)
           VALUES (:id, :title, :start_date, :start, :end, :all_day, :repeat, :repeat_until, :location, :notes, :busy, :stamp, :stamp)""",
        {**values, "id": event_id, "stamp": stamp},
    )
    return get_event(db, event_id)


def get_event(db: sqlite3.Connection, event_id: str) -> dict[str, Any]:
    row = db.execute("SELECT * FROM my_events WHERE id = ?", (event_id,)).fetchone()
    if not row:
        raise KeyError(event_id)
    out = dict(row)
    out["all_day"], out["busy"] = bool(out["all_day"]), bool(out["busy"])
    out["skip_dates"] = json.loads(out["skip_dates"] or "[]")
    return out


def update_event(db: sqlite3.Connection, event_id: str, fields: dict[str, Any], scope: str = "all", occurrence: str | None = None) -> dict[str, Any]:
    """Edit an event. For a repeating event, scope 'one' splits that date off as its own event."""
    current = get_event(db, event_id)
    if scope == "one" and current["repeat"] != "none":
        if not occurrence:
            raise EventError("Which date should change?")
        delete_event(db, event_id, "one", occurrence)
        return add_event(db, {**current, **fields, "start_date": fields.get("start_date", occurrence), "repeat": "none"})
    if scope == "future" and current["repeat"] != "none" and occurrence and occurrence > current["start_date"]:
        _end_series_before(db, current, occurrence)
        return add_event(db, {**current, **fields, "start_date": fields.get("start_date", occurrence)})
    values = _validate_event({**current, **fields})
    db.execute(
        """UPDATE my_events SET title = :title, start_date = :start_date, start = :start, end = :end, all_day = :all_day, repeat = :repeat,
           repeat_until = :repeat_until, location = :location, notes = :notes, busy = :busy, updated_at = :stamp WHERE id = :id""",
        {**values, "id": event_id, "stamp": _now()},
    )
    return get_event(db, event_id)


def _end_series_before(db: sqlite3.Connection, event: dict[str, Any], occurrence: str) -> None:
    last = (date.fromisoformat(occurrence) - timedelta(days=1)).isoformat()
    db.execute("UPDATE my_events SET repeat_until = ?, updated_at = ? WHERE id = ?", (last, _now(), event["id"]))


def delete_event(db: sqlite3.Connection, event_id: str, scope: str = "all", occurrence: str | None = None) -> None:
    event = get_event(db, event_id)
    if event["repeat"] == "none" or scope == "all" or not occurrence:
        db.execute("DELETE FROM my_events WHERE id = ?", (event_id,))
    elif scope == "future":
        if occurrence <= event["start_date"]:
            db.execute("DELETE FROM my_events WHERE id = ?", (event_id,))
        else:
            _end_series_before(db, event, occurrence)
    else:  # one occurrence
        skips = sorted(set(event["skip_dates"]) | {occurrence})
        db.execute("UPDATE my_events SET skip_dates = ?, updated_at = ? WHERE id = ?", (json.dumps(skips), _now(), event_id))


def _occurs_on(event: dict[str, Any], day: date) -> bool:
    first = date.fromisoformat(event["start_date"])
    if day < first or day.isoformat() in event["skip_dates"]:
        return False
    if event["repeat_until"] and day > date.fromisoformat(event["repeat_until"]):
        return False
    repeat = event["repeat"]
    if repeat == "none":
        return day == first
    if repeat == "daily":
        return True
    if repeat == "weekdays":
        return day.weekday() < 5
    if repeat == "weekly":
        return day.weekday() == first.weekday()
    if repeat == "biweekly":
        return (day - first).days % 14 == 0
    if repeat == "monthly":
        return day.day == first.day
    return False


def occurrences(db: sqlite3.Connection, start: date, end: date) -> list[dict[str, Any]]:
    """Every occurrence between start and end (inclusive), shaped like calendar events."""
    if (end - start).days > MAX_OCCURRENCE_SCAN:
        end = start + timedelta(days=MAX_OCCURRENCE_SCAN)
    rows = db.execute(
        "SELECT * FROM my_events WHERE start_date <= ? AND (repeat != 'none' OR start_date >= ?) AND (repeat_until IS NULL OR repeat_until >= ?)",
        (end.isoformat(), start.isoformat(), start.isoformat()),
    ).fetchall()
    out = []
    for row in rows:
        event = dict(row)
        event["skip_dates"] = json.loads(event["skip_dates"] or "[]")
        day = max(start, date.fromisoformat(event["start_date"]))
        while day <= end:
            if _occurs_on(event, day):
                out.append({
                    "id": f"mine:{event['id']}:{day.isoformat()}", "event_id": event["id"], "occurrence": day.isoformat(),
                    "title": event["title"], "start": _iso_at(day, event["start"]), "end": _iso_at(day, event["end"]),
                    "start_minutes": event["start"], "end_minutes": event["end"],
                    "all_day": bool(event["all_day"]), "busy": bool(event["busy"]), "location": event["location"], "notes": event["notes"],
                    "repeat": event["repeat"], "feed_name": "My events", "feed_kind": "mine", "fixed": True,
                })
            if event["repeat"] == "none":
                break
            day += timedelta(days=1)
    out.sort(key=lambda e: (e["start"], e["title"]))
    return out


# ---------------------------------------------------------------- to-dos

def _validate_todo(fields: dict[str, Any]) -> dict[str, Any]:
    title = str(fields.get("title", "")).strip()
    if not title:
        raise EventError("What's the to-do?")
    try:
        minutes = int(fields.get("minutes") or 30)
    except (TypeError, ValueError) as error:
        raise EventError("Estimate the time in minutes.") from error
    if not 5 <= minutes <= 12 * 60:
        raise EventError("Estimates can be 5 minutes to 12 hours.")
    due_date = fields.get("due_date") or None
    due_time = fields.get("due_time") or None
    if due_date:
        try:
            date.fromisoformat(str(due_date))
        except ValueError as error:
            raise EventError("Use a valid due date.") from error
    if due_time:
        try:
            datetime.strptime(str(due_time), "%H:%M")
        except ValueError as error:
            raise EventError("Use a valid due time.") from error
    return {"title": title[:160], "minutes": minutes, "due_date": due_date, "due_time": due_time if due_date else None,
            "notes": str(fields.get("notes") or "")[:1000]}


def add_todo(db: sqlite3.Connection, fields: dict[str, Any]) -> dict[str, Any]:
    values = _validate_todo(fields)
    todo_id = uuid.uuid4().hex[:12]
    db.execute("INSERT INTO todos (id, title, minutes, due_date, due_time, notes, created_at) VALUES (:id, :title, :minutes, :due_date, :due_time, :notes, :stamp)",
               {**values, "id": todo_id, "stamp": _now()})
    return get_todo(db, todo_id)


def get_todo(db: sqlite3.Connection, todo_id: str) -> dict[str, Any]:
    row = db.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    if not row:
        raise KeyError(todo_id)
    return {**dict(row), "done": bool(row["done"])}


def update_todo(db: sqlite3.Connection, todo_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    current = get_todo(db, todo_id)
    if "done" in fields:
        done = bool(fields["done"])
        db.execute("UPDATE todos SET done = ?, done_at = ? WHERE id = ?", (int(done), _now() if done else None, todo_id))
    editable = {key: fields[key] for key in ("title", "minutes", "due_date", "due_time", "notes") if key in fields}
    if editable:
        values = _validate_todo({**current, **editable})
        db.execute("UPDATE todos SET title = :title, minutes = :minutes, due_date = :due_date, due_time = :due_time, notes = :notes WHERE id = :id",
                   {**values, "id": todo_id})
    return get_todo(db, todo_id)


def delete_todo(db: sqlite3.Connection, todo_id: str) -> bool:
    return bool(db.execute("DELETE FROM todos WHERE id = ?", (todo_id,)).rowcount)


def list_todos(db: sqlite3.Connection, include_done: bool = False) -> list[dict[str, Any]]:
    planned = {row[0]: row[1] for row in db.execute("SELECT ref, SUM(end - start) FROM plan_blocks WHERE kind = 'task' GROUP BY ref")}
    rows = db.execute(
        f"SELECT * FROM todos {'' if include_done else 'WHERE done = 0'} ORDER BY done, due_date IS NULL, due_date, due_time, created_at"
    ).fetchall()
    return [{**dict(row), "done": bool(row["done"]), "planned_minutes": planned.get(row["id"], 0)} for row in rows]


def open_todos(db: sqlite3.Connection) -> list[dict[str, Any]]:
    return [todo for todo in list_todos(db) if not todo["done"]]
