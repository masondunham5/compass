"""Activity-file parsing (CSV/TCX/GPX) and matching Garmin activities to planned sessions."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
import csv
import hashlib
import io
import math
import xml.etree.ElementTree as ET

def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    match = re.search(r"-?[\d,.]+", str(value))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_duration_minutes(value: Any) -> float | None:
    if value is None:
        return None
    numeric = parse_number(value)
    text = str(value).strip()
    if ":" not in text:
        return numeric
    try:
        pieces = [float(part) for part in text.split(":")]
        if len(pieces) == 3:
            return pieces[0] * 60 + pieces[1] + pieces[2] / 60
        if len(pieces) == 2:
            return pieces[0] + pieces[1] / 60
    except ValueError:
        return numeric
    return numeric


def parse_iso_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def activity_id(source: str, activity: dict[str, Any]) -> str:
    signature = "|".join(str(activity.get(key, "")) for key in ("activity_date", "activity_type", "title", "distance_miles", "duration_minutes"))
    return f"activity-{hashlib.sha256(f'{source}|{signature}'.encode()).hexdigest()[:20]}"


def point_distance_miles(one: tuple[float, float], two: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, one)
    lat2, lon2 = map(math.radians, two)
    delta_lat, delta_lon = lat2 - lat1, lon2 - lon1
    a = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(a))


def normalize_activity(payload: dict[str, Any], source: str) -> dict[str, Any] | None:
    activity_type = str(payload.get("activity_type") or "Activity").strip()
    title = str(payload.get("title") or activity_type).strip()
    activity_date = str(payload.get("activity_date") or "").strip()[:10]
    if not activity_date:
        return None
    item = {
        "activity_date": activity_date,
        "activity_type": activity_type,
        "title": title,
        "distance_miles": payload.get("distance_miles"),
        "duration_minutes": payload.get("duration_minutes"),
        "average_hr": payload.get("average_hr"),
        "max_hr": payload.get("max_hr"),
        "elevation_feet": payload.get("elevation_feet"),
        "details": payload.get("details", {}),
    }
    item["id"] = activity_id(source, item)
    return item


def parse_garmin_csv(text: str) -> list[dict[str, Any]]:
    rows = list(csv.DictReader(io.StringIO(text)))
    activities = []
    for row in rows:
        lowered = {str(key).lower().strip(): value for key, value in row.items()}
        raw_type = lowered.get("activity type") or lowered.get("type") or "Activity"
        if "run" not in str(raw_type).lower():
            continue
        raw_date = lowered.get("date") or lowered.get("activity date") or lowered.get("start time")
        parsed = parse_iso_time(raw_date)
        date_value = parsed.date().isoformat() if parsed else (str(raw_date or "")[:10])
        distance = parse_number(lowered.get("distance"))
        if distance and "km" in str(lowered.get("distance", "")).lower():
            distance *= 0.621371
        activities.append({
            "activity_date": date_value,
            "activity_type": "Run",
            "title": lowered.get("title") or lowered.get("activity name") or "Garmin run",
            "distance_miles": distance,
            "duration_minutes": parse_duration_minutes(lowered.get("time") or lowered.get("duration") or lowered.get("moving time")),
            "average_hr": parse_number(lowered.get("avg hr") or lowered.get("average heart rate")),
            "max_hr": parse_number(lowered.get("max hr") or lowered.get("maximum heart rate")),
            "elevation_feet": parse_number(lowered.get("elev gain") or lowered.get("elevation gain")),
            "details": {"source_row": row},
        })
    return activities


def parse_xml_activity(content: bytes, source: str) -> list[dict[str, Any]]:
    root = ET.fromstring(content)
    tag = root.tag.lower()
    points = []
    times, heart_rates = [], []
    distances = []
    for node in root.iter():
        local = node.tag.rsplit("}", 1)[-1]
        if local == "Trackpoint":
            lat = node.findtext(".//{*}LatitudeDegrees")
            lon = node.findtext(".//{*}LongitudeDegrees")
            if lat and lon:
                points.append((float(lat), float(lon)))
            timestamp = node.findtext(".//{*}Time")
            if timestamp:
                times.append(parse_iso_time(timestamp))
            distance = parse_number(node.findtext(".//{*}DistanceMeters"))
            if distance is not None:
                distances.append(distance)
            heart_rate = parse_number(node.findtext(".//{*}HeartRateBpm/{*}Value"))
            if heart_rate is not None:
                heart_rates.append(int(heart_rate))
    if not times:
        return []
    distance_miles = (max(distances) / 1609.344) if distances else sum(point_distance_miles(points[index - 1], point) for index, point in enumerate(points) if index)
    duration = (times[-1] - times[0]).total_seconds() / 60 if len(times) > 1 and times[0] and times[-1] else None
    return [{
        "activity_date": times[0].date().isoformat(), "activity_type": "Run", "title": f"Imported {source.upper()} run",
        "distance_miles": round(distance_miles, 2) if distance_miles else None,
        "duration_minutes": round(duration, 1) if duration else None,
        "average_hr": round(sum(heart_rates) / len(heart_rates)) if heart_rates else None,
        "max_hr": max(heart_rates) if heart_rates else None,
        "elevation_feet": None, "details": {"track_points": len(points)},
    }]


def garmin_activity_kind(activity_type: str) -> str | None:
    lowered = (activity_type or "").lower()
    if "run" in lowered:
        return "run"
    if "strength" in lowered:
        return "strength"
    return None


def match_garmin_activities(db: sqlite3.Connection) -> int:
    """Pair each unlinked Garmin activity with the same-day session of the same kind.

    A still-planned session is marked completed using Garmin's duration/HR, so a
    workout recorded on the watch and planned here appears once, not twice. A
    session already completed in Compass is only linked. Activities with no
    same-day, same-kind session stay as standalone history.
    """
    today = date.today().isoformat()
    rows = db.execute(
        "SELECT * FROM imported_activities WHERE linked_session_id IS NULL AND source = 'Garmin API' AND activity_date <= ? ORDER BY activity_date, imported_at",
        (today,),
    ).fetchall()
    matched = 0
    # Pass 1 links same-day pairs for every activity; pass 2 lets leftovers take a planned
    # session shifted by one day, so a shifted activity never steals another day's match.
    leftovers = []
    for shifted, candidates in ((False, rows), (True, leftovers)):
        for activity in list(candidates):
            matched += _link_activity(db, activity, shifted, leftovers)
    return matched


def _link_activity(db: sqlite3.Connection, activity: sqlite3.Row, shifted: bool, leftovers: list) -> int:
    kind = garmin_activity_kind(activity["activity_type"])
    if not kind:
        return 0
    if not shifted:
        session = db.execute(
            """SELECT * FROM sessions WHERE session_date = ? AND kind = ? AND status IN ('planned', 'completed')
               ORDER BY CASE status WHEN 'completed' THEN 0 ELSE 1 END, title LIMIT 1""",
            (activity["activity_date"], kind),
        ).fetchone()
        if not session:
            leftovers.append(activity)
            return 0
    else:
        day = date.fromisoformat(activity["activity_date"])
        session = db.execute(
            """SELECT * FROM sessions s WHERE kind = ? AND status = 'planned' AND session_date IN (?, ?)
               AND NOT EXISTS (SELECT 1 FROM imported_activities a WHERE a.linked_session_id = s.id)
               ORDER BY session_date, title LIMIT 1""",
            (kind, (day - timedelta(days=1)).isoformat(), (day + timedelta(days=1)).isoformat()),
        ).fetchone()
        if not session:
            return 0
    if session["status"] == "planned":
        metrics: dict[str, Any] = {
            "source": "Garmin (matched automatically)", "garmin_activity_id": activity["id"],
            "duration_minutes": activity["duration_minutes"], "average_hr": activity["average_hr"], "max_hr": activity["max_hr"],
        }
        if kind == "run" and activity["distance_miles"]:
            metrics["distance_miles"] = activity["distance_miles"]
            if activity["duration_minutes"]:
                metrics["pace_seconds_per_mile"] = round(activity["duration_minutes"] * 60 / activity["distance_miles"])
        note = f"Completed from a Garmin activity on {activity['activity_date']}." if shifted else "Completed from a Garmin activity on the same day."
        db.execute("UPDATE sessions SET status = 'completed' WHERE id = ?", (session["id"],))
        db.execute(
            "INSERT INTO logs (session_id, completed_at, minutes, notes, metrics_json) VALUES (?, ?, ?, ?, ?)",
            (session["id"], datetime.now().astimezone().isoformat(timespec="seconds"), round(activity["duration_minutes"] or session["minutes"]), note, json.dumps(metrics)),
        )
    db.execute("UPDATE imported_activities SET linked_session_id = ? WHERE id = ?", (session["id"], activity["id"]))
    return 1
