"""Keep runs spaced well and fueled right, wherever they end up on the calendar.

- `classify` tags a session from its title/description: long (long or progression run), hard
  (tempo, intervals, threshold, marathon pace, hills, race…), legs (lower-body lifting).
- `spacing_issues(db, changes)` checks a proposed set of moves (from `schedule.plan_moves`) and
  returns warnings that involve a moved session: a long run the day after legs, a hard run the day
  after the long run, hard days back to back, or legs and a hard run on the same day. Completed
  sessions count, so "I just did legs today" is taken into account.
- `cards(db, now)` builds Fuel & prep cards from the *actual* schedule (today, tonight for
  tomorrow, a heads-up a few days out), using the run's planned time when it has a plan block.
  Advice scales with the run's length: short runs need little, 75+ minutes gets a carb-forward
  dinner, 90+ minutes gets fueling during the run. General guidance, not medical advice.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

HARD = re.compile(r"\b(tempo|interval|threshold|marathon pace|race|hill|fartlek|progression|speed|track|vo2)\b", re.IGNORECASE)
LONG = re.compile(r"\b(long run|progression)\b", re.IGNORECASE)
LEGS = re.compile(r"\b(leg|legs|lower|squat|hinge|deadlift)\b", re.IGNORECASE)
DINNER_MINUTES = 75   # runs this long (or races) get a carb-forward dinner the night before
FUEL_MINUTES = 90     # and this long need fuel during the run


def classify(row: Any) -> set[str]:
    text = f"{row['title']} {row['description'] if 'description' in row.keys() else ''}"
    tags: set[str] = set()
    if row["kind"] == "run":
        tags.add("run")
        if LONG.search(text):
            tags.add("long")
        if HARD.search(text) or str(row["title"]).startswith("Race:"):
            tags.add("hard")
    elif row["kind"] == "strength" and LEGS.search(text):
        tags.add("legs")
    return tags


def _nice(day: date) -> str:
    return day.strftime("%a %b %d").replace(" 0", " ")


def spacing_issues(db: sqlite3.Connection, changes: list[dict[str, Any]], window: tuple[date, date] | None = None) -> list[str]:
    """Spacing problems a set of workout moves would create (only ones involving a moved session),
    or — with `window` and no changes — every problem already in the plan for those dates."""
    if not changes and not window:
        return []
    moving = {c["id"]: c["to"] for c in changes}
    if window:
        lo, hi = window[0] - timedelta(days=1), window[1]
    else:
        touched = [date.fromisoformat(c["to"]) for c in changes] + [date.fromisoformat(c["from"]) for c in changes]
        lo, hi = min(touched) - timedelta(days=2), max(touched) + timedelta(days=2)
    rows = db.execute("SELECT id, session_date, kind, title, description, status FROM sessions WHERE status IN ('planned', 'completed') "
                      "AND (session_date BETWEEN ? AND ? OR id IN (%s))" % ",".join("?" for _ in moving),
                      (lo.isoformat(), hi.isoformat(), *moving)).fetchall()
    days: dict[date, list[tuple[str, set[str], str]]] = {}
    for row in rows:
        when = date.fromisoformat(moving.get(row["id"], row["session_date"]))
        tags = classify(row)
        if tags - {"run"}:
            title = re.sub(r"^W\d+\s*·\s*", "", row["title"])
            days.setdefault(when, []).append((row["id"], tags, title))

    def find(day: date, tag: str) -> list[tuple[str, set[str], str]]:
        return [s for s in days.get(day, []) if tag in s[1]]

    issues: list[str] = []

    def note(sessions: list[tuple[str, set[str], str]], message: str) -> None:
        if (window or any(s[0] in moving for s in sessions)) and message not in issues:
            issues.append(message)

    for day in sorted(d for d in days if not window or window[0] <= d <= window[1]):
        before, after = day - timedelta(days=1), day + timedelta(days=1)
        for long in find(day, "long"):
            for legs in find(before, "legs"):
                note([long, legs], f"{long[2]} on {_nice(day)} comes the day after {legs[2]} — tired legs make long runs harder. Keep a day between them if you can.")
            for hard in [s for s in find(after, "hard") if "run" in s[1]]:
                note([long, hard], f"{hard[2]} on {_nice(after)} is the day after the long run — make that an easy day instead.")
        for hard in [s for s in find(day, "hard") if "long" not in s[1]]:
            for next_hard in find(after, "hard") + find(after, "long"):
                if next_hard[0] != hard[0]:
                    note([hard, next_hard], f"{hard[2]} ({_nice(day)}) and {next_hard[2]} ({_nice(after)}) are hard days back to back — put an easy day between.")
        for legs in find(day, "legs"):
            for run in find(day, "hard") + find(day, "long"):
                note([legs, run], f"{legs[2]} and {run[2]} on the same day ({_nice(day)}) — split them if you can, or keep the run easy.")
    return issues


# ---------------------------------------------------------------- fuel & prep

def _clock(minutes: int) -> str:
    minutes %= 24 * 60
    hour, minute = divmod(int(minutes), 60)
    return f"{(hour % 12) or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def _start_times(db: sqlite3.Connection, today: date, days: int) -> dict[str, int]:
    """Planned start (minutes) per session, from accepted plan blocks, else today's suggestions."""
    from assistant import planner
    end = today + timedelta(days=days)
    times = {row["ref"]: row["start"] for row in db.execute(
        "SELECT ref, start FROM plan_blocks WHERE kind = 'workout' AND outcome != 'missed' AND block_date BETWEEN ? AND ?", (today.isoformat(), end.isoformat()))}
    try:
        plan = planner.build(db, today, 2)
        for day in plan["days"]:
            for block in day["blocks"]:
                if block["kind"] == "workout":
                    times.setdefault(block["ref"], block["start"])
    except Exception:  # noqa: BLE001 — times are a nicety; cards still work without them
        pass
    return times


def cards(db: sqlite3.Connection, now: datetime | None = None) -> list[dict[str, Any]]:
    import health
    now = now or datetime.now()
    today = now.date()
    rows = db.execute("SELECT * FROM sessions WHERE kind = 'run' AND status IN ('planned', 'completed') AND session_date BETWEEN ? AND ? ORDER BY session_date",
                      (today.isoformat(), (today + timedelta(days=3)).isoformat())).fetchall()
    if not rows:
        return []
    times = _start_times(db, today, 3)
    protein = health.protein_target(db)
    recovery_protein = f"{min(40, max(20, round(protein * 0.25 / 5) * 5)):.0f} g" if protein else "20–40 g"  # ~a quarter of the day's target
    minute_now = now.hour * 60 + now.minute
    out: list[dict[str, Any]] = []
    for row in rows:
        day = date.fromisoformat(row["session_date"])
        tags, minutes, start = classify(row), int(row["minutes"] or 0), times.get(row["id"])
        title = re.sub(r"^W\d+\s*·\s*", "", row["title"])
        big = minutes >= DINNER_MINUTES or str(row["title"]).startswith("Race:")
        at = f" at {_clock(start)}" if start is not None else ""
        if day == today:
            if row["status"] == "completed":
                if big or "hard" in tags:
                    out.append({"id": f"after:{row['id']}", "when": "Today", "title": f"Recover from {title}",
                                "items": [f"Protein ({recovery_protein}) plus carbs within about an hour — a meal or a shake and fruit.",
                                          "Drink to thirst plus a bit more; add salt if you sweated a lot."]})
                continue
            items = []
            if big:
                items.append(f"Carb-based meal 2–3 hours before{f' (by {_clock(start - 150)})' if start is not None else ''} — oats, rice, toast, bagel; easy on fat and fiber.")
                items.append("Small top-up 30–60 minutes before if you're hungry (banana, a few chews).")
            elif minutes >= 45 or "hard" in tags:
                items.append(f"Light snack 60–90 minutes before{f' (by {_clock(start - 60)})' if start is not None else ''} — banana, toast, a granola bar.")
            else:
                items.append("Short and easy: no special fuel needed. Run comfortably fed, not stuffed.")
            items.append("About 500 ml of water in the couple of hours before.")
            if minutes >= FUEL_MINUTES:
                items.append("Bring fuel: 30–60 g of carbs per hour (a gel or chews every ~45 minutes) and water.")
            if big or "hard" in tags:
                items.append(f"Afterwards: protein ({recovery_protein}) plus carbs within about an hour.")
            if start is None or start > minute_now - minutes:
                out.append({"id": f"today:{row['id']}", "when": "Today", "title": f"{title}{at} · {minutes} min", "items": items})
        elif day == today + timedelta(days=1):
            items = []
            if big:
                items += ["Carb-forward dinner tonight — rice, pasta or potatoes with some protein; go easy on very fatty or high-fiber food.",
                          "A glass or two of extra water this evening."]
            else:
                items.append("Normal dinner is fine — it's a shorter run.")
            if start is not None:
                bed = start - 30 - 8 * 60
                items.append(f"For ~8 hours of sleep, be in bed by about {_clock(bed)}.")
            items.append("Lay out shoes, clothes" + (" and fuel" if minutes >= FUEL_MINUTES else "") + " tonight.")
            out.append({"id": f"tonight:{row['id']}", "when": "Tonight", "title": f"Tomorrow: {title}{at} · {minutes} min", "items": items})
        elif big:
            out.append({"id": f"ahead:{row['id']}", "when": day.strftime("%A"), "title": f"Coming up: {title} · {minutes} min",
                        "items": ["Make sure you have easy carbs in the house (rice, pasta, oats, bagels)" + (" and run fuel (gels or chews)." if minutes >= FUEL_MINUTES else "."),
                                  "Keep the day before light on hard training."]})
    return out
