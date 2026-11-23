"""Places and travel time, so the plan leaves room to get somewhere.

- A *place* is somewhere you go (Home, Campus, Work, a gym) with an address and keywords. An
  event's location is matched to a place by keyword ("Bessey Hall 108" -> Campus when Campus has
  the keyword "Bessey"); online locations (Teams, Zoom, …) need no travel; unknown locations
  are assumed to be close by (no travel).
- Travel minutes between two places come from OpenStreetMap — Nominatim to find the address,
  the routing.openstreetmap.de OSRM servers for the route — estimated once on request and stored.
  You can type your own number instead; a manual number is never overwritten. Each place can add
  "extra" minutes on arrival (parking, walking to the building).
- `day_travel` walks a day's located, busy events in order (starting and ending at Home) and
  returns travel strips (busy time for the planner, thin bars on the calendar) plus warnings when
  two events are too close together to make it. A "Drive to …" style event you made yourself
  counts as that travel, so nothing is doubled.
- Workouts: if you lift somewhere other than Home (setting `lift_place`), strength workouts get
  travel from and back to Home on each side (`workout_padding`).
Only place addresses are sent to OpenStreetMap, and only when you press Estimate.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime
from typing import Any, Callable

KINDS = {"home": "Home", "campus": "Campus", "work": "Work", "gym": "Gym", "other": "Other"}
MODES = {"drive": "routed-car", "walk": "routed-foot", "bike": "routed-bike"}
VIRTUAL = re.compile(r"\b(teams|zoom|virtual|online|webex|meet\.google|google meet|remote|skype|discord)\b", re.IGNORECASE)
TRAVEL_TITLE = re.compile(r"^\s*(drive|driving|commute|bus|walk|bike|ride|travel|uber|lyft|head)\b", re.IGNORECASE)
NOMINATIM = "https://nominatim.openstreetmap.org/search"
OSRM = "https://routing.openstreetmap.de/{profile}/route/v1/driving/{a};{b}?overview=false"
USER_AGENT = "Compass-planner/1.0 (personal planner)"

Transport = Callable[[str], Any]


class PlaceError(ValueError):
    """User-facing."""


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """CREATE TABLE IF NOT EXISTS places (
               id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'other', address TEXT NOT NULL DEFAULT '',
               keywords TEXT NOT NULL DEFAULT '', extra INTEGER NOT NULL DEFAULT 0, lat REAL, lon REAL, created_at TEXT NOT NULL
           );
           CREATE TABLE IF NOT EXISTS travel_times (
               a TEXT NOT NULL, b TEXT NOT NULL, minutes INTEGER NOT NULL, manual INTEGER NOT NULL DEFAULT 0, mode TEXT NOT NULL DEFAULT 'drive',
               updated_at TEXT NOT NULL, PRIMARY KEY (a, b)
           );"""
    )


def _setting(db, key: str, default: str | None = None) -> str | None:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def _set(db, key: str, value: str | None) -> None:
    if value is None:
        db.execute("DELETE FROM settings WHERE key = ?", (key,))
    else:
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- places

def _clean(fields: dict[str, Any]) -> dict[str, Any]:
    name = str(fields.get("name") or "").strip()[:60]
    if not name:
        raise PlaceError("Give the place a name.")
    kind = str(fields.get("kind") or "other")
    if kind not in KINDS:
        raise PlaceError("Unknown kind of place.")
    keywords = ", ".join(dict.fromkeys(k.strip() for k in str(fields.get("keywords") or "").split(",") if len(k.strip()) >= 2))[:500]
    try:
        extra = int(fields.get("extra") or 0)
    except (TypeError, ValueError) as error:
        raise PlaceError("Extra minutes should be a number.") from error
    if not 0 <= extra <= 60:
        raise PlaceError("Extra minutes can be 0–60.")
    return {"name": name, "kind": kind, "address": str(fields.get("address") or "").strip()[:200], "keywords": keywords, "extra": extra}


def list_places(db) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute("SELECT * FROM places ORDER BY kind != 'home', created_at")]


def add_place(db, fields: dict[str, Any]) -> dict[str, Any]:
    values = _clean(fields)
    if values["kind"] == "home" and db.execute("SELECT 1 FROM places WHERE kind = 'home'").fetchone():
        raise PlaceError("You already have a Home. Edit that one instead.")
    place_id = uuid.uuid4().hex[:10]
    db.execute("INSERT INTO places (id, name, kind, address, keywords, extra, created_at) VALUES (:id, :name, :kind, :address, :keywords, :extra, :now)",
               {**values, "id": place_id, "now": _now()})
    return get_place(db, place_id)


def get_place(db, place_id: str) -> dict[str, Any]:
    row = db.execute("SELECT * FROM places WHERE id = ?", (place_id,)).fetchone()
    if not row:
        raise KeyError(place_id)
    return dict(row)


def update_place(db, place_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    current = get_place(db, place_id)
    values = _clean({**current, **{k: v for k, v in fields.items() if v is not None}})
    if values["kind"] == "home" and db.execute("SELECT 1 FROM places WHERE kind = 'home' AND id != ?", (place_id,)).fetchone():
        raise PlaceError("You already have a Home.")
    moved = values["address"] != current["address"]
    db.execute("UPDATE places SET name = :name, kind = :kind, address = :address, keywords = :keywords, extra = :extra WHERE id = :id", {**values, "id": place_id})
    if moved:  # new address: forget the map point and the estimated (not manual) travel times
        db.execute("UPDATE places SET lat = NULL, lon = NULL WHERE id = ?", (place_id,))
        db.execute("DELETE FROM travel_times WHERE manual = 0 AND (a = ? OR b = ?)", (place_id, place_id))
    return get_place(db, place_id)


def delete_place(db, place_id: str) -> bool:
    db.execute("DELETE FROM travel_times WHERE a = ? OR b = ?", (place_id, place_id))
    if _setting(db, "lift_place") == place_id:
        _set(db, "lift_place", None)
    return bool(db.execute("DELETE FROM places WHERE id = ?", (place_id,)).rowcount)


def home(db) -> dict[str, Any] | None:
    row = db.execute("SELECT * FROM places WHERE kind = 'home' LIMIT 1").fetchone()
    return dict(row) if row else None


def resolve(location: str, places: list[dict[str, Any]]) -> dict[str, Any] | str | None:
    """The place an event's location belongs to; 'virtual' for online; None when unknown."""
    text = str(location or "").strip().lower()
    if not text:
        return None
    if VIRTUAL.search(text):
        return "virtual"
    best, best_len = None, 0
    for place in places:
        candidates = [k.strip().lower() for k in place["keywords"].split(",") if k.strip()]
        candidates.append(place["name"].lower())
        street = place["address"].split(",")[0].strip().lower()
        if len(street) >= 6:
            candidates.append(street)
        for word in candidates:
            if len(word) >= 2 and re.search(rf"(?<![a-z0-9]){re.escape(word)}", text) and len(word) > best_len:
                best, best_len = place, len(word)
    return best


# ---------------------------------------------------------------- travel times

def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def travel_minutes(db, a: dict[str, Any] | None, b: dict[str, Any] | None, table: dict | None = None) -> int:
    """Minutes from place a to place b, including b's extra (parking/walking). 0 if unknown."""
    if not a or not b or a["id"] == b["id"]:
        return 0
    key = _pair(a["id"], b["id"])
    if table is not None:
        base = table.get(key)
    else:
        row = db.execute("SELECT minutes FROM travel_times WHERE a = ? AND b = ?", key).fetchone()
        base = row[0] if row else None
    if base is None:
        return 0
    return int(base) + int(b.get("extra") or 0)


def travel_table(db) -> dict[tuple[str, str], int]:
    return {(row["a"], row["b"]): row["minutes"] for row in db.execute("SELECT * FROM travel_times")}


def set_travel(db, a: str, b: str, minutes: int | None) -> None:
    """Your own number for a pair (None goes back to the estimate on the next Estimate)."""
    key = _pair(a, b)
    if minutes is None:
        db.execute("DELETE FROM travel_times WHERE a = ? AND b = ?", key)
        return
    if not 0 <= int(minutes) <= 240:
        raise PlaceError("Travel time can be 0–240 minutes.")
    db.execute("INSERT INTO travel_times (a, b, minutes, manual, mode, updated_at) VALUES (?, ?, ?, 1, ?, ?) "
               "ON CONFLICT(a, b) DO UPDATE SET minutes = excluded.minutes, manual = 1, updated_at = excluded.updated_at",
               (*key, int(minutes), mode(db), _now()))


def mode(db) -> str:
    value = _setting(db, "travel_mode", "drive")
    return value if value in MODES else "drive"


def _get_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise PlaceError(f"The map service returned an error ({error.code}). Try again later or type the minutes yourself.") from error
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise PlaceError("Couldn't reach the map service. Check the internet connection or type the minutes yourself.") from error


def estimate(db, transport: Transport | None = None, pause: float = 1.1) -> dict[str, Any]:
    """Find each place on the map (once) and estimate travel times between every pair you
    haven't set yourself. Returns what happened, per place and pair."""
    get = transport or _get_json
    places = list_places(db)
    problems = []
    for place in places:
        if place["lat"] is not None or not place["address"]:
            if not place["address"]:
                problems.append(f"{place['name']} has no address.")
            continue
        found = get(f"{NOMINATIM}?{urllib.parse.urlencode({'q': place['address'], 'format': 'json', 'limit': 1})}")
        if found:
            place["lat"], place["lon"] = float(found[0]["lat"]), float(found[0]["lon"])
            db.execute("UPDATE places SET lat = ?, lon = ? WHERE id = ?", (place["lat"], place["lon"], place["id"]))
        else:
            problems.append(f"Couldn't find {place['name']}'s address on the map.")
        time.sleep(pause)  # Nominatim allows one request a second
    db.commit()
    profile, estimated = MODES[mode(db)], 0
    manual = {(r["a"], r["b"]) for r in db.execute("SELECT a, b FROM travel_times WHERE manual = 1")}
    located = [p for p in places if p["lat"] is not None]
    for i, a in enumerate(located):
        for b in located[i + 1:]:
            key = _pair(a["id"], b["id"])
            if key in manual:
                continue
            route = get(OSRM.format(profile=profile, a=f"{a['lon']},{a['lat']}", b=f"{b['lon']},{b['lat']}"))
            routes = (route or {}).get("routes") or []
            if not routes:
                problems.append(f"No route between {a['name']} and {b['name']}.")
                continue
            minutes = max(5, int(-(-routes[0]["duration"] // 300) * 5))  # round up to 5 minutes
            db.execute("INSERT INTO travel_times (a, b, minutes, manual, mode, updated_at) VALUES (?, ?, ?, 0, ?, ?) "
                       "ON CONFLICT(a, b) DO UPDATE SET minutes = excluded.minutes, mode = excluded.mode, updated_at = excluded.updated_at",
                       (*key, minutes, mode(db), _now()))
            estimated += 1
    return {"estimated": estimated, "problems": problems}


def travel_list(db) -> list[dict[str, Any]]:
    """Every pair of places with its minutes (None = not known yet)."""
    places = list_places(db)
    table = {(r["a"], r["b"]): dict(r) for r in db.execute("SELECT * FROM travel_times")}
    out = []
    for i, a in enumerate(places):
        for b in places[i + 1:]:
            row = table.get(_pair(a["id"], b["id"]))
            out.append({"a": a["id"], "b": b["id"], "a_name": a["name"], "b_name": b["name"],
                        "minutes": row["minutes"] if row else None, "manual": bool(row and row["manual"])})
    return out


# ---------------------------------------------------------------- a day's travel

def _minute(iso: str, day: date) -> int:
    moment = datetime.fromisoformat(iso)
    if moment.date() < day:
        return 0
    if moment.date() > day:
        return 24 * 60
    return moment.hour * 60 + moment.minute


def day_travel(db, day: date, events: list[dict[str, Any]], places: list[dict[str, Any]] | None = None,
               table: dict | None = None) -> dict[str, Any]:
    """Travel strips and warnings for one day's fixed events (starting and ending at Home)."""
    places = list_places(db) if places is None else places
    start_home = next((p for p in places if p["kind"] == "home"), None)
    if not places:
        return {"strips": [], "warnings": []}
    table = travel_table(db) if table is None else table
    timed = sorted((e for e in events if not e.get("all_day") and e.get("busy", True)), key=lambda e: e["start"])
    manual_travel = [(_minute(e["start"], day), _minute(e["end"], day)) for e in timed if TRAVEL_TITLE.search(e["title"])]
    strips, warnings = [], []
    here, here_end, here_title = start_home, None, "home"
    for event in timed:
        if TRAVEL_TITLE.search(event["title"]):
            continue
        place = resolve(event.get("location", ""), places)
        if place is None and event.get("place_hint"):  # work shifts happen at your Work place
            place = next((p for p in places if p["kind"] == event["place_hint"]), None)
        if not isinstance(place, dict):
            continue  # online or unknown: no travel
        start, end = _minute(event["start"], day), _minute(event["end"], day)
        need = travel_minutes(db, here, place, table)
        if need:
            leave = start - need
            covered = any(a <= start and b >= start - 15 and b - a >= need * 0.5 for a, b in manual_travel)
            if here_end is not None and leave < here_end:
                gap = start - here_end
                warnings.append({"date": day.isoformat(), "message": f"{here_title} ends {_clock(here_end)} at {here['name']} and {event['title']} starts "
                                                                       f"{_clock(start)} at {place['name']} — about {need} min apart, you have {gap}."})
                leave = here_end
            if not covered and start > leave:
                strips.append({"start": max(0, leave), "end": start, "minutes": need, "from": here["name"], "to": place["name"]})
        here, here_end, here_title = place, end, event["title"]
    if start_home and here and here["id"] != start_home["id"] and here_end is not None:
        need = travel_minutes(db, here, start_home, table)
        covered = any(a >= here_end - 5 and a <= here_end + 30 for a, b in manual_travel)
        if need and not covered and here_end < 24 * 60:
            strips.append({"start": here_end, "end": min(24 * 60, here_end + need), "minutes": need, "from": here["name"], "to": start_home["name"]})
    return {"strips": strips, "warnings": warnings}


def workout_padding(db, kind: str, places: list[dict[str, Any]] | None = None, table: dict | None = None) -> int:
    """Travel minutes on each side of a strength workout when you lift away from Home."""
    if kind != "strength":
        return 0
    lift = _setting(db, "lift_place")
    if not lift:
        return 0
    places = list_places(db) if places is None else places
    gym = next((p for p in places if p["id"] == lift), None)
    start_home = next((p for p in places if p["kind"] == "home"), None)
    return travel_minutes(db, start_home, gym, table) if gym and start_home else 0


def unmatched_locations(db, start: date, days: int = 30, places: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Event locations in the next few weeks that don't match a place yet (most common first)."""
    from datetime import timedelta

    from assistant import availability
    places = list_places(db) if places is None else places
    counts: dict[str, int] = {}
    for offset in range(days):
        for event in availability.fixed_events(db, start + timedelta(days=offset)):
            location = str(event.get("location") or "").strip()
            if location and resolve(location, places) is None:
                counts[location] = counts.get(location, 0) + 1
    return [{"location": loc, "count": n} for loc, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:15]]


def _clock(minutes: int) -> str:
    hour, minute = divmod(int(minutes), 60)
    return f"{(hour % 12) or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"
