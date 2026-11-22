"""How busy is a day, and when are you free? Built from calendar events.

This is the interface the training side uses (Today agenda, rescheduling warnings,
exam-week alerts). Times are local wall-clock; the planning day runs 6 AM–10 PM.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from assistant.calendars import events_between

DAY_START, DAY_END = 6 * 60, 22 * 60
EXAM_WORDS = re.compile(r"\b(exam|midterm|final|quiz|test|presentation|defense|practical)\b", re.IGNORECASE)
HEAVY_BUSY_MINUTES = 6 * 60


def _minutes(iso: str) -> int:
    moment = datetime.fromisoformat(iso)
    return moment.hour * 60 + moment.minute


def _clock(minutes: int) -> str:
    hour, minute = divmod(minutes, 60)
    return f"{(hour % 12) or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def _busy_blocks(events: list[dict[str, Any]], day: date) -> list[tuple[int, int]]:
    """Merged busy intervals (minutes after midnight) for one day, clipped to the planning day."""
    blocks = []
    day_start = datetime.combine(day, datetime.min.time())
    for event in events:
        if event["all_day"] or not event["busy"]:
            continue
        start = max(datetime.fromisoformat(event["start"]), day_start)
        end = min(datetime.fromisoformat(event["end"]), day_start + timedelta(days=1))
        if end <= start:
            continue
        a = int((start - day_start).total_seconds() // 60)
        b = int((end - day_start).total_seconds() // 60)
        a, b = max(a, DAY_START), min(b, DAY_END)
        if b > a:
            blocks.append((a, b))
    blocks.sort()
    merged: list[list[int]] = []
    for a, b in blocks:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def free_slots(blocks: list[tuple[int, int]], min_minutes: int = 45) -> list[dict[str, Any]]:
    slots, cursor = [], DAY_START
    for a, b in blocks + [(DAY_END, DAY_END)]:
        if a - cursor >= min_minutes:
            slots.append({"start": _clock(cursor), "end": _clock(a), "minutes": a - cursor, "start_minutes": cursor})
        cursor = max(cursor, b)
    return slots


def best_slot(slots: list[dict[str, Any]], minutes: int) -> dict[str, Any] | None:
    """Prefer a late-afternoon/evening slot (3–8 PM) that fits, else the earliest that fits."""
    fitting = [slot for slot in slots if slot["minutes"] >= minutes]
    if not fitting:
        return None
    preferred = [slot for slot in fitting if 15 * 60 <= slot["start_minutes"] + max(0, slot["minutes"] - minutes) and slot["start_minutes"] < 20 * 60]
    slot = (preferred or fitting)[0]
    start = max(slot["start_minutes"], 15 * 60) if preferred else slot["start_minutes"]
    start = min(start, slot["start_minutes"] + slot["minutes"] - minutes)
    return {"start": _clock(start), "end": _clock(start + minutes), "start_minutes": start}


def day_summary(db: sqlite3.Connection, day: date, workout_minutes: int = 60) -> dict[str, Any]:
    events = events_between(db, day, day)
    blocks = _busy_blocks(events, day)
    busy = sum(b - a for a, b in blocks)
    exams = [event["title"] for event in events if EXAM_WORDS.search(event["title"])]
    slots = free_slots(blocks)
    return {
        "date": day.isoformat(), "busy_minutes": busy, "events": events, "exams": exams,
        "heavy": busy >= HEAVY_BUSY_MINUTES or bool(exams), "free_slots": slots,
        "best_slot": best_slot(slots, workout_minutes), "has_calendar": bool(db.execute("SELECT 1 FROM calendar_feeds LIMIT 1").fetchone()),
    }


def day_notes(db: sqlite3.Connection, days: list[str]) -> list[dict[str, Any]]:
    """Heads-ups for specific dates (used by rescheduling previews): exams or very full days."""
    notes = []
    for iso in sorted(set(days)):
        summary = day_summary(db, date.fromisoformat(iso))
        if summary["heavy"]:
            notes.append({"date": iso, "busy_hours": round(summary["busy_minutes"] / 60, 1), "exams": summary["exams"]})
    return notes


def exam_alerts(db: sqlite3.Connection, today: date | None = None, horizon: int = 3) -> list[dict[str, Any]]:
    """Exams in the next few days on which a hard workout is planned → suggest moving or lightening it."""
    today = today or date.today()
    alerts = []
    for offset in range(0, horizon + 1):
        day = today + timedelta(days=offset)
        summary = day_summary(db, day)
        if not summary["exams"]:
            continue
        sessions = db.execute("SELECT title FROM sessions WHERE session_date = ? AND status = 'planned' AND kind IN ('strength', 'run')", (day.isoformat(),)).fetchall()
        when = "Today" if offset == 0 else "Tomorrow" if offset == 1 else day.strftime("%A")
        exam = summary["exams"][0]
        if sessions:
            workout = re.sub(r"^W\d+\s*·\s*", "", sessions[0][0])
            alerts.append({"level": "warn", "kind": "exam", "title": f"{when}: {exam}",
                           "message": f"You also have {workout} planned. Consider moving it or doing the short version."})
        else:
            alerts.append({"level": "info", "kind": "exam", "title": f"{when}: {exam}", "message": "No hard training planned that day — good."})
    return alerts
