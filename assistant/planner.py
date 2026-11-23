"""Draft a time-blocked plan: workouts and schoolwork fitted around fixed commitments.

Fixed (never moved): school/ICS calendar events, work shifts, your own Compass events and blocks
you've already accepted. Personal Outlook events are fixed too, except that when schoolwork or a
to-do can't fit before its deadline, the draft may suggest moving one of them (a "move" draft you
accept or ignore; see `_make_room`).
Flexible, placed in this order:
  1. Workouts — each planned session goes in the best free slot on its own day (prefers
     3–8 PM). If there's no room, the draft says so instead of silently skipping it.
  2. Exam review — two 60-minute review blocks in the days before each exam.
  3. Schoolwork — open School To Do tasks, earliest deadline first, split into blocks of
     30–90 minutes, spread across days, aiming to finish a day early (falling back to the
     due day if needed), with a daily study cap so nothing gets crammed.
  4. Personal to-dos — deadline first, then undated ones oldest first, one block per day where
     possible, with their own daily cap.

The result is a draft: nothing is saved until you accept blocks. Accepted blocks are stored
and treated as fixed the next time the draft is built.

Check-in: once a block has started you can mark it done or missed (`mark`). A missed block keeps
its time slot reserved but no longer counts toward its task, so the work is planned again; `mark`
accepts the replacement right away (automatic rescheduling). Unmarked past blocks count as done.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from assistant import availability, events, places, school, work

SETTINGS = {
    "day_start": 7 * 60,        # earliest block
    "day_end": 22 * 60 + 30,    # latest block end
    "max_block": 90, "min_block": 30, "gap": 15,
    "daily_study_cap": 240,     # minutes of schoolwork per day
    "study_start": 9 * 60,      # study blocks don't start before this
    "review_blocks": 2, "review_minutes": 60,
    "daily_task_cap": 120,      # minutes of personal to-dos per day
    "max_moves": 3,             # personal Outlook events suggested for moving per draft
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
    columns = {row[1] for row in db.execute("PRAGMA table_info(plan_blocks)")}
    if "outcome" not in columns:  # '' (not checked in), 'done' or 'missed'
        db.execute("ALTER TABLE plan_blocks ADD COLUMN outcome TEXT NOT NULL DEFAULT ''")
        db.execute("ALTER TABLE plan_blocks ADD COLUMN outcome_at TEXT")


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
    task_minutes: int = 0
    held: list[dict[str, Any]] = field(default_factory=list)  # movable personal Outlook events (busy too)

    def free(self, gap: int) -> list[tuple[int, int]]:
        """Free intervals between open_from and day end, keeping `gap` minutes around busy blocks."""
        slots, cursor = [], self.open_from
        for a, b in sorted(self.busy + [(h["start"], h["end"]) for h in self.held]):
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
    place_list, table = places.list_places(db), places.travel_table(db)
    for offset in range(days):
        day = start + timedelta(days=offset)
        # Fixed: school/ICS feeds, personal Outlook, your own events, work shifts — then accepted blocks.
        fixed = availability.fixed_events(db, day)
        held = [event for event in fixed if event.get("movable")]
        busy = list(availability._busy_blocks([e for e in fixed if not e.get("movable")], day, 0, 24 * 60))
        busy += [(s["start"], s["end"]) for s in places.day_travel(db, day, fixed, place_list, table)["strips"]]  # getting there
        busy += [(b["start"], b["end"]) for b in accepted if b["block_date"] == day.isoformat()]
        open_from = SETTINGS["day_start"]
        if day == now.date():
            open_from = max(open_from, ((now.hour * 60 + now.minute + 14) // 15) * 15)
        midnight = datetime.combine(day, datetime.min.time())
        spans = [{"id": e["id"], "title": e["title"], "start": int((datetime.fromisoformat(e["start"]) - midnight).total_seconds() // 60),
                  "end": int((datetime.fromisoformat(e["end"]) - midnight).total_seconds() // 60)} for e in held]
        out[day.isoformat()] = Day(day, busy, open_from, held=spans)
    return out


def _closest(day: Day, minutes: int, around: int) -> int | None:
    """The free start nearest `around` (minutes after midnight) that fits `minutes`, on 5-minute marks."""
    best = None
    for a, b in day.free(SETTINGS["gap"]):
        if b - a < minutes:
            continue
        start = min(max(around, a), b - minutes)
        start = start - start % 5 if start - start % 5 >= a else start
        if best is None or abs(start - around) < abs(best - around):
            best = start
    return best


def _make_room(plan_days: dict[str, Day], day: Day, minutes: int, latest_end: int | None, earliest: int) -> tuple[int, dict[str, Any]] | None:
    """Find a personal Outlook event on `day` that, moved elsewhere, frees `minutes` there.
    Returns (slot for the work, the move) without changing anything, or None."""
    for event in sorted(day.held, key=lambda h: h["start"]):
        if event.get("fixed_by_move"):
            continue
        day.held.remove(event)
        try:
            slot = _place(day, minutes, latest_end=latest_end, earliest=earliest)
            if slot is None:
                continue
            day.take(slot, minutes)  # pretend the work is there while finding the event a new home
            try:
                length = event["end"] - event["start"]
                best = None  # (score, day, start): same time of day if possible, soon after
                for target in plan_days.values():
                    if target.date < day.date:
                        continue
                    new_start = _closest(target, length, event["start"])
                    if new_start is None or (target is day and new_start == event["start"]):
                        continue
                    score = (target.date - day.date).days * 180 + abs(new_start - event["start"])
                    if best is None or score < best[0]:
                        best = (score, target, new_start)
                if best:
                    return slot, {"event": event, "day": best[1], "start": best[2], "minutes": length}
            finally:
                day.busy.pop()
        finally:
            if event not in day.held and not event.get("moving"):
                day.held.append(event)
    return None


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
    db.commit()  # don't hold a write lock while the rest of the draft is built
    every_block = _accepted(db, start, horizon_end)
    plan_days = _build_days(db, start, days, now, every_block)  # missed blocks keep their slot busy…
    accepted = [b for b in every_block if b["outcome"] != "missed"]  # …but don't count as work done
    drafts: list[dict[str, Any]] = []
    warnings: list[str] = []
    accepted_refs = {(b["kind"], b["ref"]) for b in accepted}

    def add(day: Day, start_min: int, minutes: int, kind: str, ref: str, title: str, course: str = "") -> dict[str, Any]:
        day.take(start_min, minutes)
        drafts.append({"id": f"{kind}:{ref}:{day.date.isoformat()}:{start_min}", "block_date": day.date.isoformat(), "start": start_min,
                       "end": start_min + minutes, "kind": kind, "ref": ref, "title": title, "course": course})
        return drafts[-1]

    moves_left = [SETTINGS["max_moves"]]

    def relieve(remaining: int, last_day: date, kind: str, ref: str, title: str, course: str, cap_key: str, cap: int,
                latest_for: Any) -> int:
        """Last resort before a deadline: suggest moving personal Outlook events to make room."""
        for day in sorted(plan_days.values(), key=lambda d: d.date):
            if remaining <= 0 or moves_left[0] <= 0 or day.date > last_day:
                break
            while remaining > 0 and moves_left[0] > 0 and getattr(day, cap_key) < cap:
                chunk = min(remaining, SETTINGS["max_block"], cap - getattr(day, cap_key))
                if chunk < min(SETTINGS["min_block"], remaining):
                    break
                found = _make_room(plan_days, day, chunk, latest_for(day), SETTINGS["study_start"])
                if not found:
                    break
                slot, move = found
                event, target = move["event"], move["day"]
                event["moving"] = True
                day.held.remove(event)
                work = add(day, slot, chunk, kind, ref, title, course)
                moved = add(target, move["start"], move["minutes"], "move", event["id"], event["title"])
                moved.update({"from_date": day.date.isoformat(), "from_start": event["start"], "from_end": event["end"],
                              "for_title": title, "for_id": work["id"]})
                target.held.append({**event, "start": move["start"], "end": move["start"] + move["minutes"], "fixed_by_move": True})
                target.busy.pop()  # the moved event is tracked in held, not busy
                setattr(day, cap_key, getattr(day, cap_key) + chunk)
                remaining -= chunk
                moves_left[0] -= 1
        return remaining

    # 1. Workouts on their planned day.
    sessions = db.execute("SELECT id, session_date, kind, title, minutes FROM sessions WHERE status = 'planned' AND session_date BETWEEN ? AND ? AND kind IN ('strength', 'run', 'mobility') ORDER BY session_date",
                          (start.isoformat(), horizon_end.isoformat())).fetchall()
    for session in sessions:
        if ("workout", session["id"]) in accepted_refs:
            continue
        day = plan_days.get(session["session_date"])
        title = session["title"].split(" · ", 1)[-1] if session["title"].startswith("W") else session["title"]
        pad = places.workout_padding(db, session["kind"])  # travel to and from the gym, if it isn't at home
        slot = _place(day, session["minutes"] + 2 * pad, prefer=(WORKOUT_WINDOW[0] - pad, WORKOUT_WINDOW[1] - pad)) if day else None
        if slot is None:
            if day and day.date >= now.date():
                warnings.append(f"No free {session['minutes'] + 2 * pad}-minute slot for {title} on {day.date.strftime('%a %b %d')}"
                                f"{' (including travel to the gym)' if pad else ''} — consider moving it or the short version.")
            continue
        if pad:
            day.take(slot, pad)
            day.take(slot + pad + session["minutes"], pad)
        add(day, slot + pad, session["minutes"], "workout", session["id"], title)

    # Already-accepted study time counts toward each task.
    done_minutes: dict[str, int] = {}
    for block in accepted:
        if block["kind"] in ("study", "review", "task"):
            done_minutes[block["ref"]] = done_minutes.get(block["ref"], 0) + block["end"] - block["start"]
    # Accepted blocks outside the window still count toward their task.
    for row in db.execute("SELECT ref, SUM(end - start) FROM plan_blocks WHERE kind IN ('study', 'review', 'task') AND outcome != 'missed' AND (block_date < ? OR block_date > ?) GROUP BY ref",
                          (start.isoformat(), horizon_end.isoformat())):
        done_minutes[row[0]] = done_minutes.get(row[0], 0) + row[1]
    for day in plan_days.values():
        day.study_minutes = sum(b["end"] - b["start"] for b in accepted if b["block_date"] == day.date.isoformat() and b["kind"] in ("study", "review"))
        day.task_minutes = sum(b["end"] - b["start"] for b in accepted if b["block_date"] == day.date.isoformat() and b["kind"] == "task")

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
        if remaining > 0 and due_day <= horizon_end and not overdue:
            remaining = relieve(remaining, due_day, "study", task.key, label, task.course, "study_minutes", SETTINGS["daily_study_cap"],
                                lambda d, due_day=due_day, due_minute=due_minute: due_minute if d.date == due_day else None)
        if remaining > 0 and due_day <= horizon_end:  # later deadlines get more days in a future draft
            warnings.append(f"Couldn't fit {remaining} min of {label} before it's due ({task.due.strftime('%a %b %d, %I:%M %p').replace(' 0', ' ')}).")

    # 4. Personal to-dos: deadline first, then undated (oldest first).
    def todo_due(todo: dict[str, Any]) -> datetime | None:
        if not todo["due_date"]:
            return None
        return datetime.strptime(f"{todo['due_date']} {todo['due_time'] or '23:59'}", "%Y-%m-%d %H:%M")

    todos = events.open_todos(db)
    todos.sort(key=lambda t: (t["due_date"] is None, todo_due(t) or datetime.max, t["created_at"]))
    for todo in todos:
        remaining = todo["minutes"] - done_minutes.get(todo["id"], 0)
        if remaining <= 0:
            continue
        due = todo_due(todo)
        if due and due.date() > horizon_end + timedelta(days=7):
            continue  # far-off deadline: a later draft handles it
        last_day = due.date() if due and due >= now else horizon_end
        for spread in (True, False):
            for day in sorted(plan_days.values(), key=lambda d: d.date):
                if remaining <= 0 or day.date > last_day:
                    break
                while remaining > 0 and day.task_minutes < SETTINGS["daily_task_cap"]:
                    chunk = min(remaining, SETTINGS["max_block"], SETTINGS["daily_task_cap"] - day.task_minutes)
                    if chunk < min(SETTINGS["min_block"], remaining):
                        break
                    latest = due.hour * 60 + due.minute if due and day.date == due.date() and due >= now else None
                    slot = _place(day, chunk, latest_end=latest, earliest=SETTINGS["study_start"])
                    if slot is None:
                        break
                    add(day, slot, chunk, "task", todo["id"], todo["title"])
                    day.task_minutes += chunk
                    remaining -= chunk
                    if spread:
                        break
            if remaining <= 0:
                break
        if remaining > 0 and due and due >= now and due.date() <= horizon_end:
            remaining = relieve(remaining, due.date(), "task", todo["id"], todo["title"], "", "task_minutes", SETTINGS["daily_task_cap"],
                                lambda d, due=due: due.hour * 60 + due.minute if d.date == due.date() else None)
        if remaining > 0 and due and due.date() <= horizon_end:
            warnings.append(f"Couldn't fit {remaining} min of “{todo['title']}” before it's due ({due.strftime('%a %b %d')}).")

    blocks = [{**b, "status": "accepted"} for b in every_block] + [{**b, "status": "draft"} for b in drafts]
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
        if block.get("kind") not in {"study", "review", "workout", "task"}:
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


OUTCOMES = {"", "done", "missed"}


def mark(db: sqlite3.Connection, block_id: str, outcome: str, now: datetime | None = None) -> dict[str, Any]:
    """Record how an accepted block went. Marking it missed plans that work again right away
    (accepting the new blocks) and reports where it went, or why it couldn't."""
    now = now or datetime.now()
    if outcome not in OUTCOMES:
        raise ValueError("outcome must be done, missed or empty")
    row = db.execute("SELECT * FROM plan_blocks WHERE id = ?", (block_id,)).fetchone()
    if not row:
        raise KeyError(block_id)
    block = dict(row)
    starts = datetime.combine(date.fromisoformat(block["block_date"]), datetime.min.time()) + timedelta(minutes=block["start"])
    if outcome and starts > now:
        raise ValueError("You can check in on a block once it has started. To skip a future block, remove it from the plan.")
    db.execute("UPDATE plan_blocks SET outcome = ?, outcome_at = ? WHERE id = ?",
               (outcome, now.astimezone().isoformat(timespec="seconds") if outcome else None, block_id))
    result: dict[str, Any] = {"block_id": block_id, "outcome": outcome, "rescheduled": [], "message": ""}
    if outcome != "missed":
        return result
    plan = build(db, now.date(), 7, now=now)
    drafts = [b for day in plan["days"] for b in day["blocks"] if b["status"] == "draft" and b["kind"] == block["kind"] and b["ref"] == block["ref"]]
    need, chosen = block["end"] - block["start"], []
    for draft in sorted(drafts, key=lambda b: (b["block_date"], b["start"])):
        if need <= 0:
            break
        chosen.append({k: v for k, v in draft.items() if k not in ("status", "start_label", "end_label")})
        need -= draft["end"] - draft["start"]
    accept(db, chosen)
    result["rescheduled"] = chosen
    if chosen:
        when = ", ".join(f"{date.fromisoformat(b['block_date']).strftime('%a')} {clock(b['start'])}" for b in chosen)
        result["message"] = f"Rescheduled to {when}."
    elif block["kind"] == "workout":
        result["message"] = "No time left today for it. Move or skip the workout in Train if you want to make it up."
    else:
        result["message"] = next((w for w in plan["warnings"] if block["title"] in w),
                                 "Nothing new was scheduled: there's no free time before it's due, or the work is already covered.")
    return result


def checkin(db: sqlite3.Connection, now: datetime | None = None, days_back: int = 3) -> list[dict[str, Any]]:
    """Accepted blocks that have ended in the last few days and haven't been marked done or missed."""
    now = now or datetime.now()
    first = (now.date() - timedelta(days=days_back)).isoformat()
    rows = db.execute("SELECT * FROM plan_blocks WHERE outcome = '' AND block_date BETWEEN ? AND ? ORDER BY block_date, start",
                      (first, now.date().isoformat())).fetchall()
    minute = now.hour * 60 + now.minute
    out = []
    for row in rows:
        if row["block_date"] == now.date().isoformat() and row["end"] > minute:
            continue
        out.append({**dict(row), "start_label": clock(row["start"]), "end_label": clock(row["end"])})
    return out


def unaccept(db: sqlite3.Connection, block_id: str | None = None, from_date: str | None = None) -> int:
    if block_id:
        return db.execute("DELETE FROM plan_blocks WHERE id = ?", (block_id,)).rowcount
    return db.execute("DELETE FROM plan_blocks WHERE block_date >= ?", (from_date or date.today().isoformat(),)).rowcount


def cleanup(db: sqlite3.Connection, today: date | None = None) -> int:
    """Drop accepted workout blocks whose session moved to another day or is no longer planned,
    and upcoming to-do blocks whose to-do was deleted or checked off (past ones stay as history)."""
    today_iso = (today or date.today()).isoformat()
    removed = db.execute(
        """DELETE FROM plan_blocks WHERE kind = 'workout' AND NOT EXISTS (
               SELECT 1 FROM sessions s WHERE s.id = plan_blocks.ref AND s.session_date = plan_blocks.block_date)"""
    ).rowcount
    removed += db.execute(
        """DELETE FROM plan_blocks WHERE kind = 'task' AND (
               NOT EXISTS (SELECT 1 FROM todos t WHERE t.id = plan_blocks.ref)
               OR (block_date >= ? AND EXISTS (SELECT 1 FROM todos t WHERE t.id = plan_blocks.ref AND t.done = 1)))""",
        (today_iso,),
    ).rowcount
    return removed
