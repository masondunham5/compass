"""Draft a time-blocked plan: workouts and schoolwork fitted around fixed commitments.

Fixed (never moved): busy calendar events, work shifts, and blocks you've already accepted.
Flexible, placed in this order:
  1. Workouts — each planned session goes in the best free slot on its own day (prefers
     3–8 PM). If there's no room, the draft says so instead of silently skipping it.
  2. Exam review — two 60-minute review blocks in the days before each exam.
  3. Schoolwork — open School To Do tasks, earliest deadline first, split into blocks of
     30–90 minutes, spread across days, aiming to finish a day early (falling back to the
     due day if needed), with a daily study cap so nothing gets crammed.

The result is a draft: nothing is saved until you accept blocks. Accepted blocks are stored
and treated as fixed the next time the draft is built.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from assistant import availability, calendars, school, work

SETTINGS = {
    "day_start": 7 * 60,        # earliest block
    "day_end": 22 * 60 + 30,    # latest block end
    "max_block": 90, "min_block": 30, "gap": 15,
    "daily_study_cap": 240,     # minutes of schoolwork per day
    "study_start": 9 * 60,      # study blocks don't start before this
    "review_blocks": 2, "review_minutes": 60,
}
WORKOUT_WINDOW = (15 * 60, 20 * 60)


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS plan_blocks (
            id TEXT PRIMARY KEY,
            block_date TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
            kind TEXT NOT NULL, ref TEXT NOT NULL, title TEXT NOT NULL, course TEXT NOT NULL DEFAULT '',
            accepted_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS plan_blocks_date ON plan_blocks (block_date);
        """
    )


def clock(minutes: int) -> str:
    hour, minute = divmod(int(minutes), 60)
    return f"{(hour % 12) or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def _minutes(hhmm: str) -> int:
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)


@dataclass
class Day:
    date: date
    busy: list[tuple[int, int]]
    open_from: int
    study_minutes: int = 0

    def free(self, gap: int) -> list[tuple[int, int]]:
        """Free intervals between open_from and day end, keeping `gap` minutes around busy blocks."""
        slots, cursor = [], self.open_from
        for a, b in sorted(self.busy):
            if a - gap > cursor:
                slots.append((cursor, a - gap))
            cursor = max(cursor, b + gap)
        if SETTINGS["day_end"] > cursor:
            slots.append((cursor, SETTINGS["day_end"]))
        return [(a, b) for a, b in slots if b > a]

    def take(self, start: int, minutes: int) -> None:
        self.busy.append((start, start + minutes))


def _accepted(db: sqlite3.Connection, start: date, end: date) -> list[dict[str, Any]]:
    rows = db.execute("SELECT * FROM plan_blocks WHERE block_date BETWEEN ? AND ? ORDER BY block_date, start", (start.isoformat(), end.isoformat())).fetchall()
    return [dict(row) for row in rows]


def _build_days(db: sqlite3.Connection, start: date, days: int, now: datetime, accepted: list[dict[str, Any]]) -> dict[str, Day]:
    out = {}
    for offset in range(days):
        day = start + timedelta(days=offset)
        events = calendars.events_between(db, day, day)
        busy = list(availability._busy_blocks(events, day))
        busy += [(_minutes(s["start"]), _minutes(s["end"])) for s in work.shifts_on(db, day)]
        busy += [(b["start"], b["end"]) for b in accepted if b["block_date"] == day.isoformat()]
        open_from = SETTINGS["day_start"]
        if day == now.date():
            open_from = max(open_from, ((now.hour * 60 + now.minute + 14) // 15) * 15)
        out[day.isoformat()] = Day(day, busy, open_from)
    return out


def _place(day: Day, minutes: int, prefer: tuple[int, int] | None = None, latest_end: int | None = None, earliest: int = 0) -> int | None:
    """Earliest start that fits `minutes` (inside `prefer` if given), starting at/after `earliest`, ending by `latest_end`."""
    end_limit = min(SETTINGS["day_end"], latest_end) if latest_end is not None else SETTINGS["day_end"]
    candidates = []
    for a, b in day.free(SETTINGS["gap"]):
        a = max(a, earliest)
        b = min(b, end_limit)
        if b - a < minutes:
            continue
        if prefer:
            start = max(a, prefer[0])
            if start + minutes <= min(b, prefer[1] + minutes):
                candidates.append((0, start))
                continue
        candidates.append((1, a))
    return min(candidates)[1] if candidates else None


def build(db: sqlite3.Connection, start: date | None = None, days: int = 7, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now()
    start = start or now.date()
    horizon_end = start + timedelta(days=days - 1)
    cleanup(db)
    accepted = _accepted(db, start, horizon_end)
    plan_days = _build_days(db, start, days, now, accepted)
    drafts: list[dict[str, Any]] = []
    warnings: list[str] = []
    accepted_refs = {(b["kind"], b["ref"]) for b in accepted}

    def add(day: Day, start_min: int, minutes: int, kind: str, ref: str, title: str, course: str = "") -> None:
        day.take(start_min, minutes)
        drafts.append({"id": f"{kind}:{ref}:{day.date.isoformat()}:{start_min}", "block_date": day.date.isoformat(), "start": start_min,
                       "end": start_min + minutes, "kind": kind, "ref": ref, "title": title, "course": course})

    # 1. Workouts on their planned day.
    sessions = db.execute("SELECT id, session_date, kind, title, minutes FROM sessions WHERE status = 'planned' AND session_date BETWEEN ? AND ? AND kind IN ('strength', 'run', 'mobility') ORDER BY session_date",
                          (start.isoformat(), horizon_end.isoformat())).fetchall()
    for session in sessions:
        if ("workout", session["id"]) in accepted_refs:
            continue
        day = plan_days.get(session["session_date"])
        title = session["title"].split(" · ", 1)[-1] if session["title"].startswith("W") else session["title"]
        slot = _place(day, session["minutes"], prefer=WORKOUT_WINDOW) if day else None
        if slot is None:
            if day and day.date >= now.date():
                warnings.append(f"No free {session['minutes']}-minute slot for {title} on {day.date.strftime('%a %b %d')} — consider moving it or the short version.")
            continue
        add(day, slot, session["minutes"], "workout", session["id"], title)

    # Already-accepted study time counts toward each task.
    done_minutes: dict[str, int] = {}
    for block in accepted:
        if block["kind"] in ("study", "review"):
            done_minutes[block["ref"]] = done_minutes.get(block["ref"], 0) + block["end"] - block["start"]
    for day in plan_days.values():
        day.study_minutes = sum(b["end"] - b["start"] for b in accepted if b["block_date"] == day.date.isoformat() and b["kind"] in ("study", "review"))

    try:
        tasks = school.load_tasks()
    except school.SchoolSourceError as error:
        tasks = []
        warnings.append(str(error))

    # 2. Exam review blocks in the days before each exam.
    for exam in [t for t in tasks if t.kind == "exam" and start <= date.fromisoformat(t.due_date) <= horizon_end + timedelta(days=SETTINGS["review_blocks"])]:
        needed = SETTINGS["review_blocks"] * SETTINGS["review_minutes"] - done_minutes.get(exam.key, 0)
        exam_day = date.fromisoformat(exam.due_date)
        for back in range(1, 4):
            if needed <= 0:
                break
            day = plan_days.get((exam_day - timedelta(days=back)).isoformat())
            if not day or day.study_minutes >= SETTINGS["daily_study_cap"]:
                continue
            slot = _place(day, SETTINGS["review_minutes"], earliest=SETTINGS["study_start"])
            if slot is not None:
                add(day, slot, SETTINGS["review_minutes"], "review", exam.key, f"Review: {exam.title}", exam.course)
                day.study_minutes += SETTINGS["review_minutes"]
                needed -= SETTINGS["review_minutes"]

    # 3. Schoolwork, earliest deadline first.
    # Include work due up to a week past the window so big tasks can start early.
    lookahead = (horizon_end + timedelta(days=7)).isoformat()
    open_tasks = [t for t in tasks if not t.done and t.kind != "exam" and t.minutes > 0 and t.due_date <= lookahead]
    # Overdue work isn't auto-scheduled (it's often done but not checked off); past prep budgets just lapse.
    forced = forced_overdue(db)
    overdue_tasks = [t for t in open_tasks if t.due < now and t.kind != "prep" and t.key not in forced]
    if overdue_tasks:
        oldest = min(overdue_tasks, key=lambda t: t.due)
        warnings.append(f"{len(overdue_tasks)} overdue task{'s' if len(overdue_tasks) != 1 else ''} in School To Do (oldest: {oldest.course} {oldest.title}, "
                        f"due {oldest.due.strftime('%b %d')}) aren't in the plan. Check off what's done there, or use “Plan it anyway” in the School list.")
    # Work due before this window's first day belongs to an earlier draft (e.g. viewing next week on a Sunday).
    open_tasks = [t for t in open_tasks if (t.due >= now and t.due_date >= start.isoformat()) or t.key in forced]
    for task in sorted(open_tasks, key=lambda t: (t.due >= now, t.due)):  # forced overdue work first, then by deadline
        remaining = task.minutes - done_minutes.get(task.key, 0)
        if remaining <= 0:
            continue
        overdue = task.due < now
        due_day = date.fromisoformat(task.due_date)
        due_minute = _minutes(task.due_time)
        label = f"{task.course}: {task.title}"
        # Pass 1 aims to finish the day before (spreading one block per day); pass 2 allows the due day itself.
        for allow_due_day in (False, True):
            last_day = due_day if allow_due_day or overdue else due_day - timedelta(days=1)
            if overdue:
                last_day = max(last_day, start + timedelta(days=1))
            for spread in (True, False):
                for day in sorted(plan_days.values(), key=lambda d: d.date):
                    if remaining <= 0 or day.date > last_day:
                        break
                    placed_today = 0
                    while remaining > 0 and day.study_minutes < SETTINGS["daily_study_cap"]:
                        chunk = min(remaining, SETTINGS["max_block"], SETTINGS["daily_study_cap"] - day.study_minutes)
                        if chunk < min(SETTINGS["min_block"], remaining):
                            break
                        latest = due_minute if day.date == due_day and not overdue else None
                        slot = _place(day, chunk, latest_end=latest, earliest=SETTINGS["study_start"])
                        if slot is None:
                            break
                        add(day, slot, chunk, "study", task.key, label, task.course)
                        day.study_minutes += chunk
                        remaining -= chunk
                        placed_today += 1
                        if spread:
                            break
            if remaining <= 0:
                break
        if remaining > 0 and due_day <= horizon_end:  # later deadlines get more days in a future draft
            warnings.append(f"Couldn't fit {remaining} min of {label} before it's due ({task.due.strftime('%a %b %d, %I:%M %p').replace(' 0', ' ')}).")

    blocks = [{**b, "status": "accepted"} for b in accepted] + [{**b, "status": "draft"} for b in drafts]
    for block in blocks:
        block["start_label"], block["end_label"] = clock(block["start"]), clock(block["end"])
    blocks.sort(key=lambda b: (b["block_date"], b["start"]))
    by_day = []
    for iso, day in plan_days.items():
        shifts = work.shifts_on(db, day.date)
        by_day.append({"date": iso, "blocks": [b for b in blocks if b["block_date"] == iso],
                       "work": [{"start": clock(_minutes(s["start"])), "end": clock(_minutes(s["end"])), "title": s["title"]} for s in shifts],
                       "study_minutes": day.study_minutes})
    return {"start": start.isoformat(), "days": by_day, "warnings": warnings, "draft_count": len(drafts),
            "school": school.status()}


FORCED_KEY = "planner_forced_overdue"


def forced_overdue(db: sqlite3.Connection) -> set[str]:
    import json
    row = db.execute("SELECT value FROM settings WHERE key = ?", (FORCED_KEY,)).fetchone()
    return set(json.loads(row[0])) if row else set()


def set_forced(db: sqlite3.Connection, task_key: str, on: bool) -> set[str]:
    """Opt an overdue task into the plan (or back out). Stored locally; School To Do is not changed."""
    import json
    keys = forced_overdue(db)
    (keys.add if on else keys.discard)(task_key)
    db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (FORCED_KEY, json.dumps(sorted(keys))))
    return keys


def accept(db: sqlite3.Connection, blocks: list[dict[str, Any]]) -> int:
    """Store draft blocks as accepted (fixed). Re-accepting the same block is a no-op."""
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    saved = 0
    for block in blocks:
        if block.get("kind") not in {"study", "review", "workout"}:
            raise ValueError("unknown block kind")
        start, end = int(block["start"]), int(block["end"])
        if not (0 <= start < end <= 24 * 60):
            raise ValueError("block times out of range")
        date.fromisoformat(block["block_date"])
        saved += db.execute(
            "INSERT OR IGNORE INTO plan_blocks (id, block_date, start, end, kind, ref, title, course, accepted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (block["id"], block["block_date"], start, end, block["kind"], block["ref"], str(block["title"])[:200], str(block.get("course", ""))[:60], stamp),
        ).rowcount
    return saved


def unaccept(db: sqlite3.Connection, block_id: str | None = None, from_date: str | None = None) -> int:
    if block_id:
        return db.execute("DELETE FROM plan_blocks WHERE id = ?", (block_id,)).rowcount
    return db.execute("DELETE FROM plan_blocks WHERE block_date >= ?", (from_date or date.today().isoformat(),)).rowcount


def cleanup(db: sqlite3.Connection) -> int:
    """Drop accepted workout blocks whose session moved to another day or is no longer planned."""
    return db.execute(
        """DELETE FROM plan_blocks WHERE kind = 'workout' AND NOT EXISTS (
               SELECT 1 FROM sessions s WHERE s.id = plan_blocks.ref AND s.session_date = plan_blocks.block_date)"""
    ).rowcount
