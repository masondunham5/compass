"""Built-in chat commands: understood without any AI, so they're instant, free and always work.

  swap chest and back [this week and next | and the next 2 pairs | for 3 weeks]
  move legs to friday            (workouts only; events go to the AI)
  push training back 2 days
  add to-do: call insurance, 20 min, due friday

Each returns the same proposal cards as the AI (via chat.propose), so nothing changes until
you confirm. Anything that doesn't match exactly is left for the AI (returns None).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
FILLER = {"my", "the", "day", "days", "workout", "workouts", "session", "lift", "and", "&", "with"}


def _number(word: str | None) -> int | None:
    if not word:
        return None
    word = word.lower()
    return int(word) if word.isdigit() else NUMBERS.get(word)


def parse_day(text: str, today: date) -> date | None:
    text = text.strip().lower().rstrip(".!?")
    text = re.sub(r"^(on|this|next)\s+", "", text)
    if text == "today":
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)
    for index, name in enumerate(WEEKDAYS):
        if text in (name, name[:3], name[:3] + "."):
            ahead = (index - today.weekday()) % 7 or 7
            return today + timedelta(days=ahead)
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})", text)
    if match:
        month, day = int(match[1]), int(match[2])
        try:
            found = date(today.year, month, day)
            return found if found >= today else date(today.year + 1, month, day)
        except ValueError:
            return None
    return None


def _words(term: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]+", term.lower()) if w not in FILLER]


def _matches(term: str, title: str) -> bool:
    from assistant.schedule import workout_type
    words = _words(term)
    if not words:
        return False
    pool = re.findall(r"[a-z]+", workout_type(title)) + re.findall(r"[a-z]+", title.lower())
    return any(p.startswith(words[0]) for p in pool)


def _upcoming(db, today: date) -> list[dict[str, Any]]:
    rows = db.execute("SELECT id, session_date, kind, title FROM sessions WHERE status = 'planned' AND session_date BETWEEN ? AND ? "
                      "AND kind IN ('strength', 'run', 'mobility') ORDER BY session_date",
                      (today.isoformat(), (today + timedelta(days=21)).isoformat())).fetchall()
    return [dict(row) for row in rows]


def _repeat(text: str) -> tuple[str, int]:
    """Strip a 'this week and next' style tail; return (rest, extra pairs)."""
    patterns = [
        (r"\s+(?:for\s+)?this\s+week\s+and\s+next(?:\s+week)?$", lambda m: 1),
        (r"\s+and\s+next\s+week(?:\s+too)?$", lambda m: 1),
        (r"\s+next\s+week\s+too$", lambda m: 1),
        (r"\s+for\s+(?:the\s+next\s+)?(\w+)\s+weeks?$", lambda m: max(0, (_number(m[1]) or 1) - 1)),
        (r"\s+(?:and\s+)?(?:also\s+)?the\s+next\s+(\w+)?\s*(?:pairs?|ones?)$", lambda m: _number(m[1]) or 1),
        (r"\s+(?:and\s+)?(?:also\s+)?the\s+next\s+(?:pair|one)$", lambda m: 1),
    ]
    for pattern, count in patterns:
        found = re.search(pattern, text)
        if found:
            return text[:found.start()], min(3, count(found))
    return text, 0


def match(db, text: str, now: datetime) -> dict[str, Any] | None:
    from assistant import chat
    today = now.date()
    clean = re.sub(r"\s+", " ", text.strip().lower()).rstrip(".!?")
    clean = re.sub(r"^(please|can you|could you|hey compass,?|compass,?)\s+", "", clean)
    try:
        # swap / flip two workouts
        found = re.match(r"^(?:swap|flip|switch|trade)\s+(.+)$", clean)
        if found:
            body, pairs = _repeat(found[1])
            first_term, second_term = (body.split(" with ", 1) if " with " in body else body.split(" and ", 1) if " and " in body else (body, ""))
            sessions = _upcoming(db, today)
            first = next((s for s in sessions if _matches(first_term, s["title"])), None)
            if not first or not second_term:
                return None
            others = [s for s in sessions if s["id"] != first["id"] and s["session_date"] != first["session_date"] and _matches(second_term, s["title"])]
            if not others:
                return None
            anchor = date.fromisoformat(first["session_date"])
            second = min(others, key=lambda s: abs((date.fromisoformat(s["session_date"]) - anchor).days))
            card = chat.propose(db, "swap_workouts", {"workout_id": first["id"], "other_workout_id": second["id"], "also_next_pairs": pairs})
            return {"reply": "Here's the swap. Confirm to apply it.", "proposals": [card]}

        # move a workout to a day
        found = re.match(r"^(?:move|reschedule)\s+(.+?)\s+to\s+(.+)$", clean)
        if found:
            day = parse_day(found[2], today)
            session = next((s for s in _upcoming(db, today) if _matches(found[1], s["title"])), None)
            if not day or not session:
                return None  # probably an event: let the AI handle it
            card = chat.propose(db, "move_workout", {"workout_id": session["id"], "new_date": day.isoformat()})
            return {"reply": "Here's the move. Confirm to apply it.", "proposals": [card]}

        # push training back N days
        found = re.match(r"^push\s+(?:my\s+)?(?:training|workouts?|plan|lifts?)\s+back\s+(?:by\s+)?(\w+)\s+days?$", clean)
        if found and _number(found[1]):
            card = chat.propose(db, "push_training_back", {"from_date": today.isoformat(), "days": _number(found[1])})
            return {"reply": "Here's what would shift. Confirm to apply it.", "proposals": [card]}

        # add a to-do
        found = re.match(r"^(?:add\s+)?(?:a\s+)?to-?do[:,]?\s+(.+)$", clean)
        if found:
            body = text.strip().rstrip(".!?")
            body = re.sub(r"^.*?to-?do[:,]?\s+", "", body, count=1, flags=re.IGNORECASE)
            args: dict[str, Any] = {}
            minutes = re.search(r",?\s*(\d+(?:\.\d+)?)\s*(min|mins|minutes|m|h|hr|hrs|hour|hours)\b", body, re.IGNORECASE)
            if minutes:
                value = float(minutes[1])
                args["minutes"] = int(value * 60 if minutes[2].lower().startswith("h") else value)
                body = body[:minutes.start()] + body[minutes.end():]
            due = re.search(r",?\s*due\s+([\w/-]+)", body, re.IGNORECASE)
            if due:
                day = parse_day(due[1], today)
                if day:
                    args["due_date"] = day.isoformat()
                body = body[:due.start()] + body[due.end():]
            args["title"] = body.strip(" ,;-")
            if not args["title"]:
                return None
            card = chat.propose(db, "add_todo", args)
            return {"reply": "Here's the to-do. Confirm to add it.", "proposals": [card]}
    except chat.ChatError as error:
        return {"reply": str(error), "proposals": []}
    return None
