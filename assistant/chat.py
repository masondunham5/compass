"""Chat with Compass (typed or spoken) to change your schedule.

How it works:
- `reply(history)` first tries the built-in commands in `commands.py` (no AI, instant, always
  available). Anything else goes to a hosted model through an OpenAI-compatible API — Groq by
  default (free tier); xAI and Gemini keys also work. The provider is picked from the key's prefix
  and the model from the provider's live model list.
- The model never changes data. Its action tools only *propose* a change: Compass validates it,
  builds a preview with the same code the app uses (e.g. `schedule.swap_sessions`) and returns it
  as a proposal card. `apply(proposal_id)` runs it when you tap Confirm and returns an undo.
- School calendars are read-only: they're shown to the model as context, never as editable.
- The key is stored in data/chat_key.bin, encrypted with Windows DPAPI (like the Outlook sign-in),
  never in .env and never sent anywhere except the provider.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Callable

import core

Transport = Callable[[str, str, dict[str, str], "bytes | None"], "tuple[int, bytes]"]

PROVIDERS = {
    "groq": {"name": "Groq", "prefix": "gsk_", "base": "https://api.groq.com/openai/v1",
             "prefer": ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "moonshotai/kimi-k2-instruct", "llama-3.3-70b-versatile"],
             "match": ["gpt-oss", "kimi", "llama", "qwen"]},
    "xai": {"name": "xAI Grok", "prefix": "xai-", "base": "https://api.x.ai/v1", "prefer": [], "match": ["grok-4", "grok-3", "grok"]},
    "gemini": {"name": "Google Gemini", "prefix": "AIza", "base": "https://generativelanguage.googleapis.com/v1beta/openai",
               "prefer": [], "match": ["flash-lite", "flash"]},
}
HISTORY = 12          # messages of conversation sent to the model
CONTEXT_DAYS = 7      # schedule days included with every message
MAX_ROUNDS = 4        # tool-call rounds per message
PROPOSAL_TTL = 60 * 60


class ChatError(RuntimeError):
    """User-facing; never contains the key."""


# ---------------------------------------------------------------- key + provider

def _key_path():
    return core.DATA_DIR / "chat_key.bin"


def _setting(db, key: str) -> str | None:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _set(db, key: str, value: str | None) -> None:
    if value is None:
        db.execute("DELETE FROM settings WHERE key = ?", (key,))
    else:
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def _load_key() -> str | None:
    from assistant.outlook import OutlookError, _dpapi
    path = _key_path()
    if not path.exists():
        return None
    try:
        return _dpapi(path.read_bytes(), protect=False).decode("utf-8")
    except (OSError, OutlookError, UnicodeDecodeError):
        return None


def provider_for(key: str) -> str:
    for name, info in PROVIDERS.items():
        if key.startswith(info["prefix"]):
            return name
    raise ChatError("That doesn't look like a Groq (gsk_…), xAI (xai-…) or Gemini (AIza…) API key.")


def _http(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read() or b""
    except (urllib.error.URLError, TimeoutError) as error:
        raise ChatError("Couldn't reach the AI service. Check the internet connection.") from error


def _call(provider: str, key: str, method: str, path: str, payload: dict[str, Any] | None = None, transport: Transport | None = None) -> Any:
    info = PROVIDERS[provider]
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": "Compass"}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode()
    status, raw = (transport or _http)(method, f"{info['base']}{path}", headers, body)
    if status in (401, 403):
        raise ChatError(f"{info['name']} didn't accept the API key. Check it in Settings → AI assistant.")
    if status == 429:
        raise ChatError(f"{info['name']}'s free limit was reached for now. Wait a minute and try again — built-in commands still work.")
    if status >= 400:
        try:
            message = json.loads(raw).get("error", {}).get("message", "")
        except (ValueError, AttributeError):
            message = ""
        raise ChatError(f"{info['name']} returned an error ({status}). {str(message).replace(key, '…')[:200]}".strip())
    return json.loads(raw) if raw else {}


def pick_model(provider: str, models: list[str]) -> str:
    info = PROVIDERS[provider]
    for wanted in info["prefer"]:
        if wanted in models:
            return wanted
    usable = [m for m in models if not any(bad in m for bad in ("whisper", "tts", "guard", "embed", "image", "vision", "audio", "playai"))]
    for fragment in info["match"]:
        found = sorted(m for m in usable if fragment in m)
        if found:
            return found[-1].removeprefix("models/")
    if usable:
        return usable[0].removeprefix("models/")
    raise ChatError(f"{info['name']} didn't list any chat models for this key.")


def save_key(db, key: str, transport: Transport | None = None) -> dict[str, Any]:
    """Check the key with the provider, pick a model, and store the key encrypted."""
    from assistant.outlook import _dpapi
    key = key.strip()
    provider = provider_for(key)
    listed = _call(provider, key, "GET", "/models", transport=transport)
    model = pick_model(provider, [m.get("id", "") for m in (listed or {}).get("data", [])])
    path = _key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_dpapi(key.encode("utf-8"), protect=True))
    _set(db, "chat_provider", provider)
    _set(db, "chat_model", model)
    return status(db)


def forget_key(db) -> dict[str, Any]:
    _key_path().unlink(missing_ok=True)
    _set(db, "chat_provider", None)
    _set(db, "chat_model", None)
    return status(db)


def status(db) -> dict[str, Any]:
    provider = _setting(db, "chat_provider")
    connected = bool(provider) and _key_path().exists()
    return {"connected": connected, "provider": PROVIDERS[provider]["name"] if connected and provider in PROVIDERS else None,
            "model": _setting(db, "chat_model") if connected else None}


# ---------------------------------------------------------------- short, stable references

def alias(kind: str, raw_id: str) -> str:
    return f"{kind}:{hashlib.sha1(raw_id.encode()).hexdigest()[:8]}"


def _resolve_block(db, ref: str) -> dict[str, Any]:
    for row in db.execute("SELECT * FROM plan_blocks WHERE block_date >= ?", ((date.today() - timedelta(days=7)).isoformat(),)):
        if alias("b", row["id"]) == ref or row["id"] == ref:
            return dict(row)
    raise ChatError(f"I couldn't find plan block {ref}.")


def _resolve_outlook(db, ref: str) -> dict[str, Any]:
    for row in db.execute("SELECT * FROM outlook_events"):
        if alias("o", row["id"]) == ref or row["id"] == ref:
            return dict(row)
    raise ChatError(f"I couldn't find Outlook event {ref}.")


def _session(db, session_id: str) -> dict[str, Any]:
    row = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row:
        raise ChatError(f"I couldn't find workout {session_id}.")
    return dict(row)


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _minutes(value: Any, label: str) -> int:
    try:
        hour, minute = str(value).strip().split(":")[:2]
        result = int(hour) * 60 + int(minute)
    except (ValueError, AttributeError) as error:
        raise ChatError(f"{label} should look like 14:30.") from error
    if not 0 <= result <= 24 * 60:
        raise ChatError(f"{label} is out of range.")
    return result


def _date(value: Any, label: str = "date") -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise ChatError(f"The {label} should be YYYY-MM-DD.") from error


def _nice(day: date | str) -> str:
    day = day if isinstance(day, date) else date.fromisoformat(day)
    return day.strftime("%a %b %d").replace(" 0", " ")


def _clock(minutes: int) -> str:
    from assistant.planner import clock
    return clock(minutes)


# ---------------------------------------------------------------- schedule context for the model

def schedule_text(db, start: date, days: int) -> str:
    """Compact, model-friendly schedule: workouts, events (with what can change), plan blocks."""
    from assistant import availability, planner
    days = max(1, min(days, 14))
    end = start + timedelta(days=days - 1)
    plan = planner.build(db, max(start, date.today()), (end - max(start, date.today())).days + 1) if end >= date.today() else {"days": []}
    drafts: dict[str, int] = {}
    for day in plan["days"]:
        drafts[day["date"]] = sum(1 for b in day["blocks"] if b["status"] == "draft")
    lines = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        iso = day.isoformat()
        lines.append(f"{day.strftime('%a')} {iso}")
        for s in db.execute("SELECT id, kind, title, minutes, status, locked FROM sessions WHERE session_date = ? AND kind IN ('strength','run','mobility') ORDER BY kind", (iso,)):
            flags = ", locked" if s["locked"] else ""
            lines.append(f"  workout [{s['id']}] {s['title']} ({s['kind']}, {s['minutes']} min, {s['status']}{flags})")
        for event in availability.fixed_events(db, day):
            span = "all day" if event["all_day"] else f"{event['start'][11:16]}-{event['end'][11:16]}"
            kind = event.get("feed_kind")
            if kind == "mine":
                ref = f"m:{event['event_id']}:{event['occurrence']}"
                tag = f"your event {ref}{', repeats ' + event['repeat'] if event.get('repeat') not in (None, 'none') else ''}"
            elif kind == "outlook":
                tag = f"personal Outlook {alias('o', event['id'])}"
            elif kind == "work":
                tag = "work shift, read-only"
            else:
                tag = f"{event.get('feed_name') or 'school calendar'}, read-only"
            free = "" if event.get("busy", True) else ", free"
            lines.append(f"  {span} {event['title']} [{tag}{free}]")
        for block in db.execute("SELECT * FROM plan_blocks WHERE block_date = ? ORDER BY start", (iso,)):
            outcome = f", {block['outcome']}" if block["outcome"] else ""
            lines.append(f"  {_hhmm(block['start'])}-{_hhmm(block['end'])} {block['title']} [plan block {alias('b', block['id'])}, {block['kind']}{outcome}]")
        if drafts.get(iso):
            lines.append(f"  ({drafts[iso]} suggested plan block{'s' if drafts[iso] != 1 else ''} not accepted yet)")
    return "\n".join(lines)


def todos_text(db) -> str:
    from assistant import events
    todos = events.open_todos(db)
    if not todos:
        return "none"
    return "\n".join(f"  [{t['id']}] {t['title']} ({t['minutes']} min{', due ' + t['due_date'] + (' ' + t['due_time'] if t['due_time'] else '') if t['due_date'] else ''})" for t in todos)


def system_prompt(db, now: datetime) -> str:
    return f"""You are Compass, a planning assistant inside the user's personal life-planner app. Today is {now.strftime('%A %Y-%m-%d')}, {now.strftime('%H:%M')}.

You help change their schedule: workouts, their own events, personal Outlook events, to-dos and plan blocks.
Rules:
- Use the action tools to propose changes. Nothing changes until the user taps Confirm, so never say something is done; say what you proposed.
- School calendar events and work shifts are read-only. Never try to change them; plan around them.
- Use the exact ids in [brackets] from the schedule. Use YYYY-MM-DD dates and 24-hour HH:MM times.
- Workouts: to trade two days use swap_workouts (also_next_pairs repeats it for the following pairs of the same two workouts). Keep lifting sessions for the same muscle group at least 48 hours apart when you can.
- If a request is ambiguous (which event, what time), ask one short question instead of guessing.
- Keep replies short and conversational; they may be read aloud.

Schedule, next {CONTEXT_DAYS} days:
{schedule_text(db, now.date(), CONTEXT_DAYS)}

Open to-dos:
{todos_text(db)}"""


# ---------------------------------------------------------------- tools

def _fn(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description,
                                              "parameters": {"type": "object", "properties": properties, "required": required}}}


S = {"type": "string"}
TOOLS = [
    _fn("get_schedule", "Read the schedule for other dates (up to 14 days).", {"start_date": S, "days": {"type": "integer"}}, ["start_date"]),
    _fn("swap_workouts", "Propose trading the days of two planned workouts.",
        {"workout_id": S, "other_workout_id": S, "also_next_pairs": {"type": "integer", "description": "Also swap the next N pairs of the same two workouts (0-3)."}},
        ["workout_id", "other_workout_id"]),
    _fn("move_workout", "Propose moving one planned workout to another date.", {"workout_id": S, "new_date": S}, ["workout_id", "new_date"]),
    _fn("push_training_back", "Propose shifting every planned workout from a date onward later by N days.",
        {"from_date": S, "days": {"type": "integer"}, "include_runs": {"type": "boolean"}}, ["from_date", "days"]),
    _fn("add_event", "Propose adding an event to the user's calendar (it also goes to their Outlook).",
        {"title": S, "date": S, "start_time": S, "end_time": S, "all_day": {"type": "boolean"}, "location": S}, ["title", "date"]),
    _fn("change_event", "Propose changing one of the user's own events (m:… refs) or personal Outlook events (o:… refs). Only pass what changes.",
        {"event_ref": S, "new_date": S, "start_time": S, "end_time": S, "title": S}, ["event_ref"]),
    _fn("delete_event", "Propose deleting one of the user's own events (m:…) or personal Outlook events (o:…).", {"event_ref": S}, ["event_ref"]),
    _fn("add_todo", "Propose adding a to-do with a time estimate; the planner finds time for it.",
        {"title": S, "minutes": {"type": "integer"}, "due_date": S, "due_time": S}, ["title"]),
    _fn("complete_todo", "Propose checking off a to-do.", {"todo_id": S}, ["todo_id"]),
    _fn("mark_block", "Propose marking a plan block (b:… ref) done or missed. Missed work is rescheduled automatically.",
        {"block_ref": S, "outcome": {"type": "string", "enum": ["done", "missed"]}}, ["block_ref", "outcome"]),
    _fn("accept_suggestions", "Propose accepting the planner's suggested blocks for one date.", {"date": S}, ["date"]),
]


# ---------------------------------------------------------------- proposals (validated, previewed, not applied)

_proposals: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def _store(kind: str, args: dict[str, Any], summary: str, details: list[str] | None = None, warning: str = "") -> dict[str, Any]:
    proposal = {"id": uuid.uuid4().hex[:10], "kind": kind, "args": args, "summary": summary, "details": details or [], "warning": warning,
                "created": time.time()}
    with _lock:
        for old in [k for k, v in _proposals.items() if time.time() - v["created"] > PROPOSAL_TTL]:
            _proposals.pop(old, None)
        _proposals[proposal["id"]] = proposal
    return {k: v for k, v in proposal.items() if k not in ("args", "created")}


def _schedule_card(kind: str, args: dict[str, Any], plan: dict[str, Any], summary: str) -> dict[str, Any]:
    if not plan["changes"]:
        reasons = "; ".join(f"{s.get('title', s['id'])}: {s['reason']}" for s in plan["skipped"]) or "nothing would change"
        raise ChatError(f"That wouldn't move anything ({reasons}).")
    details = [f"{c['title']}: {_nice(c['from'])} → {_nice(c['to'])}" for c in plan["changes"]]
    details += [f"Stays: {s.get('title', s['id'])} ({s['reason']})" for s in plan["skipped"]]
    warning = " ".join([f"{_nice(c['date'])} would have {len(c['sessions'])} lifting sessions." for c in plan.get("conflicts", [])] + plan.get("spacing", []))
    return _store(kind, args, summary, details, warning)


def propose(db, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate and preview one action. Raises ChatError with a reason the model (or user) can act on."""
    from assistant import events, outlook, planner, schedule
    if name == "swap_workouts":
        first, second = _session(db, args["workout_id"]), _session(db, args["other_workout_id"])
        pairs = max(0, min(int(args.get("also_next_pairs") or 0), 3))
        plan = schedule.swap_sessions(db, first["id"], second["id"], pairs)
        more = f", and the next {pairs} pair{'s' if pairs != 1 else ''}" if pairs else ""
        return _schedule_card("swap", {"session_id": first["id"], "other_id": second["id"], "repeat": pairs}, plan,
                              f"Swap {first['title']} and {second['title']}{more}")
    if name == "move_workout":
        session, day = _session(db, args["workout_id"]), _date(args["new_date"], "new date")
        return _schedule_card("move", {"session_id": session["id"], "to_date": day.isoformat()}, schedule.move_session(db, session["id"], day.isoformat()),
                              f"Move {session['title']} to {_nice(day)}")
    if name == "push_training_back":
        day, count = _date(args["from_date"], "start date"), int(args.get("days") or 1)
        if not 1 <= count <= 30:
            raise ChatError("Push back between 1 and 30 days.")
        runs = args.get("include_runs", True) is not False
        return _schedule_card("push", {"from_date": day.isoformat(), "days": count, "include_runs": runs},
                              schedule.push_back(db, day.isoformat(), count, runs), f"Push training back {count} day{'s' if count != 1 else ''} from {_nice(day)}")
    if name == "add_event":
        day = _date(args["date"])
        all_day = bool(args.get("all_day")) or not args.get("start_time")
        fields = {"title": args.get("title", ""), "start_date": day.isoformat(), "all_day": all_day, "location": args.get("location") or ""}
        if not all_day:
            start = _minutes(args["start_time"], "Start time")
            end = _minutes(args["end_time"], "End time") if args.get("end_time") else start + 60
            fields.update(start=start, end=end)
        try:
            events._validate_event(fields)
        except events.EventError as error:
            raise ChatError(str(error)) from error
        when = "all day" if all_day else f"{_clock(fields['start'])}–{_clock(fields['end'])}"
        return _store("add_event", fields, f"Add “{fields['title']}” on {_nice(day)}, {when}", [fields["location"]] if fields["location"] else [])
    if name in ("change_event", "delete_event"):
        ref = str(args.get("event_ref", ""))
        if ref.startswith("m:"):
            try:
                _, event_id, occurrence = ref.split(":", 2)
                event = events.get_event(db, event_id)
            except (ValueError, KeyError) as error:
                raise ChatError(f"I couldn't find your event {ref}.") from error
            repeating = event["repeat"] != "none"
            if name == "delete_event":
                return _store("delete_mine", {"event_id": event_id, "occurrence": occurrence, "repeating": repeating},
                              f"Delete “{event['title']}”" + (f" on {_nice(occurrence)} (just that one)" if repeating else ""),
                              warning="" if not repeating else "Only this occurrence is removed.")
            changes = _changes(args, occurrence)
            label = _describe_changes(changes, event["title"])
            return _store("change_mine", {"event_id": event_id, "occurrence": occurrence, "changes": changes, "repeating": repeating},
                          label, ["Just this occurrence" if repeating else ""])
        if ref.startswith("o:"):
            row = _resolve_outlook(db, ref)
            if name == "delete_event":
                return _store("delete_outlook", {"event_id": row["id"]}, f"Delete “{row['title']}” from your Outlook Calendar",
                              warning="This can't be undone from Compass.")
            changes = _changes(args, row["start"][:10])
            changes.pop("all_day", None)
            return _store("change_outlook", {"event_id": row["id"], "changes": changes,
                                              "before": {"date": row["start"][:10], "start": _minutes(row["start"][11:16], "start"), "end": _minutes(row["end"][11:16], "end"), "title": row["title"]}},
                          _describe_changes(changes, row["title"]) + " (Outlook)")
        raise ChatError("Only your own events (m:…) and personal Outlook events (o:…) can change. School and work events are read-only.")
    if name == "add_todo":
        fields = {k: args.get(k) for k in ("title", "minutes", "due_date", "due_time") if args.get(k) not in (None, "")}
        try:
            values = events._validate_todo(fields)
        except events.EventError as error:
            raise ChatError(str(error)) from error
        due = f", due {_nice(values['due_date'])}{' ' + _clock(_minutes(values['due_time'], 'due')) if values['due_time'] else ''}" if values["due_date"] else ""
        return _store("add_todo", values, f"Add to-do “{values['title']}” ({values['minutes']} min{due})")
    if name == "complete_todo":
        try:
            todo = events.get_todo(db, str(args["todo_id"]))
        except KeyError as error:
            raise ChatError("I couldn't find that to-do.") from error
        return _store("complete_todo", {"todo_id": todo["id"]}, f"Check off “{todo['title']}”")
    if name == "mark_block":
        block, outcome = _resolve_block(db, str(args["block_ref"])), args.get("outcome")
        if outcome not in ("done", "missed"):
            raise ChatError("Mark it done or missed.")
        starts = datetime.combine(date.fromisoformat(block["block_date"]), datetime.min.time()) + timedelta(minutes=block["start"])
        if starts > datetime.now():
            raise ChatError("That block hasn't started yet, so it can't be checked in.")
        return _store("mark_block", {"block_id": block["id"], "outcome": outcome},
                      f"Mark {block['title']} ({_nice(block['block_date'])} {_clock(block['start'])}) {outcome}",
                      ["Missed work is rescheduled automatically."] if outcome == "missed" else [])
    if name == "accept_suggestions":
        day = _date(args["date"])
        if day < date.today():
            raise ChatError("Suggestions are only for today onward.")
        plan = planner.build(db, day, 1)
        drafts = [b for d in plan["days"] for b in d["blocks"] if b["status"] == "draft" and b["kind"] != "move"]
        if not drafts:
            raise ChatError(f"There are no suggested blocks on {_nice(day)}.")
        return _store("accept", {"date": day.isoformat()}, f"Accept {len(drafts)} suggested block{'s' if len(drafts) != 1 else ''} on {_nice(day)}",
                      [f"{b['start_label']} {b['title']}" for b in drafts])
    raise ChatError(f"Unknown action {name}.")


def _changes(args: dict[str, Any], current_date: str) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if args.get("title"):
        changes["title"] = str(args["title"])[:120]
    if args.get("new_date"):
        changes["date"] = _date(args["new_date"], "new date").isoformat()
    if args.get("start_time"):
        changes["start"] = _minutes(args["start_time"], "Start time")
    if args.get("end_time"):
        changes["end"] = _minutes(args["end_time"], "End time")
    if "start" in changes and "end" not in changes:
        raise ChatError("Give an end time too.")
    if not changes:
        raise ChatError("Say what should change (date, time or title).")
    return changes


def _describe_changes(changes: dict[str, Any], title: str) -> str:
    parts = []
    if "date" in changes:
        parts.append(f"to {_nice(changes['date'])}")
    if "start" in changes:
        parts.append(f"at {_clock(changes['start'])}–{_clock(changes['end'])}")
    if "title" in changes:
        parts.append(f"renamed “{changes['title']}”")
    return f"Move “{title}” {' '.join(parts)}" if ("date" in changes or "start" in changes) else f"Change “{title}”: {' '.join(parts)}"


# ---------------------------------------------------------------- applying (and undoing)

def apply(db, proposal_id: str) -> dict[str, Any]:
    """Run a confirmed proposal. Returns a summary and, when possible, an undo proposal."""
    from assistant import events, outlook, planner, schedule
    with _lock:
        proposal = _proposals.pop(proposal_id, None)
    if not proposal:
        raise ChatError("That suggestion expired or was already used. Ask again.")
    kind, args = proposal["kind"], proposal["args"]
    undo = None
    if kind in ("swap", "move", "push"):
        if kind == "swap":
            plan = schedule.swap_sessions(db, args["session_id"], args["other_id"], args["repeat"])
        elif kind == "move":
            plan = schedule.move_session(db, args["session_id"], args["to_date"])
        else:
            plan = schedule.push_back(db, args["from_date"], args["days"], args["include_runs"])
        result = schedule.apply_moves(db, plan)
        planner.cleanup(db)
        undo = _store("schedule_undo", {}, "Undo that workout change")
        summary = result["summary"]
    elif kind == "schedule_undo":
        summary = schedule.undo_last(db)["summary"]
    elif kind == "add_event":
        event = events.add_event(db, args)
        undo = _store("delete_mine", {"event_id": event["id"], "occurrence": event["start_date"], "repeating": False}, f"Remove “{event['title']}”")
        summary = f"Added “{event['title']}”."
    elif kind == "delete_mine":
        event = events.get_event(db, args["event_id"])
        if args["repeating"]:
            events.delete_event(db, args["event_id"], "one", args["occurrence"])
        else:
            events.delete_event(db, args["event_id"])
            undo = _store("add_event", {k: event[k] for k in ("title", "start_date", "start", "end", "all_day", "location", "notes", "busy")},
                          f"Put “{event['title']}” back")
        summary = f"Deleted “{event['title']}”."
    elif kind == "change_mine":
        before = events.get_event(db, args["event_id"])
        changes = dict(args["changes"])
        fields = {"title": changes.get("title", before["title"])}
        if "date" in changes:
            fields["start_date"] = changes["date"]
        if "start" in changes:
            fields.update(start=changes["start"], end=changes["end"], all_day=False)
        scope = "one" if args["repeating"] else "all"
        updated = events.update_event(db, args["event_id"], fields, scope, args["occurrence"])
        if not args["repeating"]:
            back = {"title": before["title"], "date": before["start_date"]}
            if not before["all_day"]:
                back.update(start=before["start"], end=before["end"])
            undo = _store("change_mine", {"event_id": args["event_id"], "occurrence": updated["start_date"], "repeating": False, "changes": back},
                          f"Put “{before['title']}” back")
        summary = f"Updated “{updated['title']}”."
    elif kind == "change_outlook":
        try:
            row = outlook.edit_event(db, args["event_id"], args["changes"])
        except outlook.OutlookError as error:
            raise ChatError(str(error)) from error
        undo = _store("change_outlook", {"event_id": args["event_id"], "changes": args["before"], "before": {**args["changes"], "title": row["title"]}},
                      f"Put “{args['before'].get('title', row['title'])}” back")
        summary = f"Updated “{row['title']}” in Outlook."
    elif kind == "delete_outlook":
        try:
            outlook.delete_event(db, args["event_id"])
        except outlook.OutlookError as error:
            raise ChatError(str(error)) from error
        summary = "Deleted it from your Outlook Calendar."
    elif kind == "add_todo":
        todo = events.add_todo(db, args)
        undo = _store("delete_todo", {"todo_id": todo["id"]}, f"Remove “{todo['title']}”")
        summary = f"Added to-do “{todo['title']}”. The plan will find time for it."
    elif kind == "delete_todo":
        events.delete_todo(db, args["todo_id"])
        planner.cleanup(db)
        summary = "Removed the to-do."
    elif kind == "complete_todo":
        todo = events.update_todo(db, args["todo_id"], {"done": True})
        planner.cleanup(db)
        undo = _store("reopen_todo", {"todo_id": todo["id"]}, f"Reopen “{todo['title']}”")
        summary = f"Checked off “{todo['title']}”."
    elif kind == "reopen_todo":
        todo = events.update_todo(db, args["todo_id"], {"done": False})
        summary = f"Reopened “{todo['title']}”."
    elif kind == "mark_block":
        result = planner.mark(db, args["block_id"], args["outcome"])
        undo = _store("mark_block_clear", {"block_id": args["block_id"]}, "Undo the check-in")
        summary = result["message"] or f"Marked {args['outcome']}."
    elif kind == "mark_block_clear":
        planner.mark(db, args["block_id"], "")
        summary = "Cleared the check-in."
    elif kind == "accept":
        day = date.fromisoformat(args["date"])
        plan = planner.build(db, day, 1)
        drafts = [{k: v for k, v in b.items() if k not in ("status", "start_label", "end_label")}
                  for d in plan["days"] for b in d["blocks"] if b["status"] == "draft" and b["kind"] != "move"]
        planner.accept(db, drafts)
        undo = _store("unaccept", {"ids": [b["id"] for b in drafts]}, "Undo accepting those")
        summary = f"Accepted {len(drafts)} block{'s' if len(drafts) != 1 else ''}."
    elif kind == "unaccept":
        for block_id in args["ids"]:
            planner.unaccept(db, block_id)
        summary = "Removed those blocks from the plan."
    else:
        raise ChatError("Unknown suggestion.")
    return {"summary": summary, "undo": undo}


# ---------------------------------------------------------------- the conversation

def _run_tool(db, name: str, args: dict[str, Any], proposals: list[dict[str, Any]]) -> str:
    try:
        if name == "get_schedule":
            return schedule_text(db, _date(args.get("start_date") or date.today().isoformat(), "start date"), int(args.get("days") or 7))
        card = propose(db, name, args)
        proposals.append(card)
        return f"Proposed to the user (waiting for them to confirm): {card['summary']}. " + " ".join(card["details"][:6])
    except ChatError as error:
        return f"Couldn't propose that: {error}"
    except (KeyError, TypeError, ValueError) as error:
        return f"Couldn't propose that: missing or invalid argument ({error})."


def reply(db, history: list[dict[str, str]], transport: Transport | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Answer the latest user message. Returns {reply, proposals, source}."""
    from assistant import commands
    now = now or datetime.now()
    history = [m for m in history if m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip()][-HISTORY:]
    if not history or history[-1]["role"] != "user":
        raise ChatError("Say or type something first.")
    text = str(history[-1]["content"]).strip()[:2000]
    built_in = commands.match(db, text, now)
    if built_in:
        return {**built_in, "source": "built-in"}
    key = _load_key()
    provider, model = _setting(db, "chat_provider"), _setting(db, "chat_model")
    if not key or provider not in PROVIDERS or not model:
        return {"reply": "I can only do built-in commands until an AI key is added in Settings → AI assistant. Try “swap chest and back”, "
                         "“push training back 2 days”, “move legs to Friday” or “add to-do: call insurance, 20 min”.",
                "proposals": [], "source": "built-in"}
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt(db, now)}]
    messages += [{"role": m["role"], "content": str(m["content"])[:2000]} for m in history]
    proposals: list[dict[str, Any]] = []
    for _ in range(MAX_ROUNDS):
        db.commit()  # never hold a database lock while waiting on the AI service
        data = _call(provider, key, "POST", "/chat/completions",
                     {"model": model, "messages": messages, "tools": TOOLS, "tool_choice": "auto", "temperature": 0.2, "max_tokens": 900},
                     transport=transport)
        message = ((data or {}).get("choices") or [{}])[0].get("message") or {}
        calls = message.get("tool_calls") or []
        if not calls:
            content = (message.get("content") or "").strip()
            if not content:
                content = "Here's what I'd change — confirm below." if proposals else "Sorry, I didn't catch that. Could you say it another way?"
            return {"reply": content, "proposals": proposals, "source": PROVIDERS[provider]["name"]}
        messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": calls})
        for call in calls:
            function = call.get("function") or {}
            try:
                args = json.loads(function.get("arguments") or "{}")
            except ValueError:
                args = {}
            result = _run_tool(db, function.get("name", ""), args if isinstance(args, dict) else {}, proposals)
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result[:6000]})
    return {"reply": "Here's what I'd change — confirm below." if proposals else "That took too many steps. Could you break it into smaller requests?",
            "proposals": proposals, "source": PROVIDERS[provider]["name"]}


# ---------------------------------------------------------------- API

from fastapi import APIRouter, HTTPException  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

router = APIRouter(prefix="/api/chat")


class KeyRequest(BaseModel):
    key: str = Field(min_length=20, max_length=300)


class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=4000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)


@router.get("/status")
def chat_status() -> dict[str, Any]:
    with core.connection() as db:
        return status(db)


@router.post("/key")
def chat_key(payload: KeyRequest) -> dict[str, Any]:
    """Check the key with its provider and store it encrypted on this computer."""
    with core.connection() as db:
        try:
            return save_key(db, payload.key)
        except ChatError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error


@router.delete("/key")
def chat_forget() -> dict[str, Any]:
    with core.connection() as db:
        return forget_key(db)


@router.post("")
def chat(payload: ChatRequest) -> dict[str, Any]:
    with core.connection() as db:
        try:
            return reply(db, [m.model_dump() for m in payload.messages])
        except ChatError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/apply/{proposal_id}")
def chat_apply(proposal_id: str) -> dict[str, Any]:
    from assistant import outlook
    with core.connection() as db:
        try:
            result = apply(db, proposal_id)
        except ChatError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    outlook.sync_soon()
    return result
