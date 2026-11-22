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


def _busy_blocks(events: list[dict[str, Any]], day: date, lo: int = DAY_START, hi: int = DAY_END) -> list[tuple[int, int]]:
    """Merged busy intervals (minutes after midnight) for one day, clipped to [lo, hi] (default: the planning day)."""
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
        a, b = max(a, lo), min(b, hi)
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


def fixed_events(db: sqlite3.Connection, day: date) -> list[dict[str, Any]]:
    """Everything that isn't the plan: school/ICS feeds, your personal Outlook, your own Compass
    events and work shifts — all shaped like calendar events. The planner treats these as fixed."""
    from assistant import events, outlook, work  # local imports keep module load order simple
    items = events_between(db, day, day) + outlook.events_between(db, day, day) + events.occurrences(db, day, day)
    for shift in work.shifts_on(db, day):
        items.append({"title": shift["title"], "start": f"{day.isoformat()}T{shift['start']}", "end": f"{day.isoformat()}T{shift['end']}",
                      "all_day": False, "busy": True, "location": "", "feed_name": "Work", "feed_kind": "work", "fixed": True})
    return items


def plan_items(db: sqlite3.Connection, day: date) -> list[dict[str, Any]]:
    """Accepted plan blocks for one day, shaped like calendar events so they count as busy."""
    items = []
    for block in db.execute("SELECT * FROM plan_blocks WHERE block_date = ? ORDER BY start", (day.isoformat(),)):
        start, end = divmod(block["start"], 60), divmod(block["end"], 60)
        items.append({"title": block["title"], "start": f"{day.isoformat()}T{start[0]:02d}:{start[1]:02d}", "end": f"{day.isoformat()}T{end[0]:02d}:{end[1]:02d}",
                      "all_day": False, "busy": True, "location": "", "feed_name": "Plan", "feed_kind": f"plan-{block['kind']}", "fixed": False})
    return items


def has_calendar(db: sqlite3.Connection) -> bool:
    return any(db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
               for table in ("calendar_feeds", "work_shifts", "plan_blocks", "my_events", "outlook_events"))


def day_summary(db: sqlite3.Connection, day: date, workout_minutes: int = 60) -> dict[str, Any]:
    events = sorted(fixed_events(db, day) + plan_items(db, day), key=lambda e: (not e["all_day"], e["start"]))
    blocks = _busy_blocks(events, day)
    busy = sum(b - a for a, b in blocks)
    exams = [event["title"] for event in events if EXAM_WORDS.search(event["title"]) and not str(event.get("feed_kind", "")).startswith("plan")]
    slots = free_slots(blocks)
    return {
        "date": day.isoformat(), "busy_minutes": busy, "events": events, "exams": exams,
        "heavy": busy >= HEAVY_BUSY_MINUTES or bool(exams), "free_slots": slots,
        "best_slot": best_slot(slots, workout_minutes),
        "has_calendar": has_calendar(db),
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
