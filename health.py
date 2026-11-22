"""Health analysis on top of the stored data: training load, illness early warning,
sleep coaching, long-term trends, race predictions, personal insights, pain-aware
exercise warnings, goals, habits, and reports.

Everything here is deterministic and explainable (no AI). Functions take an open
SQLite connection and return plain dicts for the API.
"""

from __future__ import annotations

import json
import math
import statistics
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from exercises import canonical_exercise, estimate_1rm

# ---------------------------------------------------------------- schema

LIFESTYLE_TAGS = ["alcohol", "late caffeine", "late meal", "travel", "sick", "stressful day", "great day"]
PAIN_AREAS = {
    "neck": ["shrug", "overhead press", "push press", "upright row"],
    "shoulder (left)": ["bench", "overhead", "shoulder press", "lateral raise", "dips", "push press", "fly", "pec dec", "upright row", "arnold",
                        "incline", "chest press", "floor press", "push-up", "military", "front raise"],
    "shoulder (right)": ["bench", "overhead", "shoulder press", "lateral raise", "dips", "push press", "fly", "pec dec", "upright row", "arnold",
                         "incline", "chest press", "floor press", "push-up", "military", "front raise"],
    "elbow": ["skull crusher", "pushdown", "tricep extension", "curl", "close grip", "dips"],
    "wrist": ["curl", "bench", "push-up", "front squat", "skull crusher"],
    "upper back": ["row", "pulldown", "pull-up", "chin-up", "deadlift"],
    "lower back": ["deadlift", "romanian", "row", "squat", "good morning", "back extension", "hip thrust"],
    "hip": ["squat", "deadlift", "lunge", "hip thrust", "leg press", "step-up", "abduction"],
    "knee (left)": ["squat", "lunge", "leg extension", "leg press", "step-up", "hack"],
    "knee (right)": ["squat", "lunge", "leg extension", "leg press", "step-up", "hack"],
    "ankle / foot": ["calf raise", "lunge", "step-up", "squat"],
    "hamstring": ["hamstring curl", "romanian", "deadlift", "hip thrust"],
}
AUTO_HABITS = {
    "sleep_7h": "Sleep 7+ hours",
    "steps_goal": "Hit your step goal",
    "protein_goal": "Hit your protein target",
    "checked_in": "Daily check-in",
}


def ensure_health_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS body_measurements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            measured_on TEXT NOT NULL,
            weight_lbs REAL, waist_in REAL, body_fat_pct REAL, notes TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS pain_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_on TEXT NOT NULL, area TEXT NOT NULL, severity INTEGER NOT NULL,
            notes TEXT NOT NULL DEFAULT '', resolved_on TEXT
        );
        CREATE TABLE IF NOT EXISTS health_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_on TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
            value TEXT NOT NULL DEFAULT '', unit TEXT NOT NULL DEFAULT '', reference TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL, title TEXT NOT NULL, exercise TEXT, target REAL NOT NULL,
            start_value REAL, deadline TEXT, created_on TEXT NOT NULL, achieved_on TEXT
        );
        CREATE TABLE IF NOT EXISTS habits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, auto_key TEXT, active INTEGER NOT NULL DEFAULT 1, created_on TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS habit_checks (
            habit_id INTEGER NOT NULL, checked_on TEXT NOT NULL, PRIMARY KEY (habit_id, checked_on)
        );
        """
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(daily_health)")}
    for column, sql_type in (("tags_json", "TEXT NOT NULL DEFAULT '[]'"), ("protein_g", "REAL"), ("water_l", "REAL")):
        if column not in columns:
            db.execute(f"ALTER TABLE daily_health ADD COLUMN {column} {sql_type}")
    if not db.execute("SELECT 1 FROM habits LIMIT 1").fetchone():
        created = date.today().isoformat()
        for key, name in AUTO_HABITS.items():
            db.execute("INSERT INTO habits (name, auto_key, created_on) VALUES (?, ?, ?)", (name, key, created))
        db.execute("INSERT INTO habits (name, auto_key, created_on) VALUES ('10 minutes of mobility', NULL, ?)", (created,))


# ---------------------------------------------------------------- helpers

def _rows(db: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(sql, params).fetchall()]


def _mean(values: list[float]) -> float | None:
    values = [float(v) for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _metrics(db: sqlite3.Connection, start: date, end: date) -> list[dict[str, Any]]:
    return _rows(db, "SELECT * FROM garmin_daily_metrics WHERE calendar_date BETWEEN ? AND ? ORDER BY calendar_date", (start.isoformat(), end.isoformat()))


def bodyweight(db: sqlite3.Connection) -> float | None:
    rows = _rows(db, """SELECT day, weight FROM (
        SELECT checkin_date AS day, weight_lbs AS weight FROM daily_health WHERE weight_lbs IS NOT NULL
        UNION ALL SELECT measured_on, weight_lbs FROM body_measurements WHERE weight_lbs IS NOT NULL) ORDER BY day DESC LIMIT 1""")
    if rows:
        return float(rows[0]["weight"])
    saved = db.execute("SELECT value FROM settings WHERE key = 'bodyweight_lbs'").fetchone()
    return float(saved[0]) if saved else None


# ---------------------------------------------------------------- training load

def daily_training_load(db: sqlite3.Connection, start: date, end: date) -> dict[str, float]:
    """Garmin's per-activity training load (EPOC-based), summed per day."""
    loads: dict[str, float] = {}
    for row in db.execute("SELECT activity_date, raw_json FROM imported_activities WHERE activity_date BETWEEN ? AND ? AND raw_json IS NOT NULL",
                          (start.isoformat(), end.isoformat())):
        try:
            value = float(json.loads(row[1]).get("activityTrainingLoad") or 0)
        except (TypeError, ValueError):
            value = 0.0
        loads[row[0]] = loads.get(row[0], 0.0) + value
    return loads


def load_status(ratio: float | None) -> tuple[str, str]:
    if ratio is None:
        return "unknown", "Needs four weeks of Garmin activities."
    if ratio > 1.5:
        return "high", "Your last 7 days are well above your 4-week norm — injury and illness risk rise when load spikes like this."
    if ratio > 1.3:
        return "rising", "Load is climbing faster than usual. Fine for a week; don't stack another big jump on it."
    if ratio < 0.8:
        return "low", "Lighter than your recent norm — good for recovery, or room to build."
    return "steady", "Load is in line with what you're used to."


def training_load(db: sqlite3.Connection, today: date | None = None) -> dict[str, Any]:
    """Acute (7-day) vs chronic (28-day weekly average) load, plus HR-zone time and run mileage."""
    today = today or date.today()
    start = today - timedelta(days=41)
    loads = daily_training_load(db, start, today)
    days = [(start + timedelta(days=i)).isoformat() for i in range(42)]
    acute = sum(loads.get(day, 0) for day in days[-7:])
    chronic_days = [day for day in days[-28:]]
    have_history = any(loads.get(day) for day in days[-28:-21])
    chronic = sum(loads.get(day, 0) for day in chronic_days) / 4 if have_history else None
    ratio = round(acute / chronic, 2) if chronic else None
    status, message = load_status(ratio)
    zones = [0.0] * 5
    effects: dict[str, int] = {}
    week_start = (today - timedelta(days=6)).isoformat()
    for row in db.execute("SELECT raw_json FROM imported_activities WHERE activity_date >= ? AND raw_json IS NOT NULL", (week_start,)):
        raw = json.loads(row[0])
        for zone in range(5):
            zones[zone] += float(raw.get(f"hrTimeInZone_{zone + 1}") or 0) / 60
        label = raw.get("trainingEffectLabel")
        if label:
            effects[label] = effects.get(label, 0) + 1
    miles = lambda a, b: float(db.execute(
        "SELECT COALESCE(SUM(distance_miles), 0) FROM imported_activities WHERE lower(activity_type) LIKE '%run%' AND activity_date BETWEEN ? AND ?",
        (a.isoformat(), b.isoformat())).fetchone()[0])
    acute_miles = miles(today - timedelta(days=6), today)
    chronic_miles = miles(today - timedelta(days=27), today) / 4
    return {
        "acute": round(acute), "chronic": round(chronic) if chronic else None, "ratio": ratio, "status": status, "message": message,
        "daily": [{"date": day, "load": round(loads.get(day, 0), 1)} for day in days],
        "zone_minutes": [round(z) for z in zones], "effects": effects,
        "run_miles": {"acute": round(acute_miles, 1), "chronic": round(chronic_miles, 1), "ratio": round(acute_miles / chronic_miles, 2) if chronic_miles else None},
    }


# ---------------------------------------------------------------- illness early warning

def illness_signal(db: sqlite3.Connection, today: date | None = None) -> dict[str, Any]:
    """Flag days where resting HR is up, HRV is down, and (when available) breathing rate is up
    versus the prior 28 days. Two flagged days among the last three is a warning."""
    today = today or date.today()
    recent = _metrics(db, today - timedelta(days=2), today)
    baseline = _metrics(db, today - timedelta(days=31), today - timedelta(days=3))
    base_rhr = _mean([row.get("resting_hr") for row in baseline])
    base_hrv = _mean([row.get("hrv_ms") for row in baseline])
    base_resp = _mean([row.get("sleep_respiration_avg") for row in baseline])
    if not (base_rhr and base_hrv) or len(baseline) < 10:
        return {"warning": False, "flagged_days": [], "message": "Needs about two weeks of Garmin history.", "baseline": None}
    flagged = []
    for row in recent:
        rhr, hrv, resp = row.get("resting_hr"), row.get("hrv_ms"), row.get("sleep_respiration_avg")
        if rhr is None or hrv is None:
            continue
        rhr_up = rhr >= base_rhr + max(3, base_rhr * 0.05)
        hrv_down = hrv <= base_hrv * 0.9
        resp_up = resp is not None and base_resp is not None and resp >= base_resp + 1.0
        if (rhr_up and hrv_down) or (resp_up and (rhr_up or hrv_down)):
            flagged.append({"date": row["calendar_date"], "resting_hr": rhr, "hrv": hrv, "respiration": resp,
                            "signs": [label for label, hit in (("resting HR up", rhr_up), ("HRV down", hrv_down), ("breathing rate up", resp_up)) if hit]})
    warning = len(flagged) >= 2
    message = ("Resting HR, HRV, and breathing rate look like your body is fighting something. Consider easy days and extra sleep until they settle."
               if warning else "No sustained illness pattern.")
    return {"warning": warning, "flagged_days": flagged, "message": message,
            "baseline": {"resting_hr": round(base_rhr, 1), "hrv": round(base_hrv, 1), "respiration": round(base_resp, 1) if base_resp else None}}


# ---------------------------------------------------------------- sleep coaching

def _clock(minutes: float | None) -> str | None:
    if minutes is None:
        return None
    minutes = int(round(minutes)) % 1440
    hour, minute = divmod(minutes, 60)
    return f"{(hour % 12) or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def _bedtime_minutes(iso: str | None) -> float | None:
    """Minutes after 6 PM (so 11 PM = 300, 1 AM = 420) — keeps bedtimes around midnight comparable."""
    if not iso:
        return None
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return ((moment.hour * 60 + moment.minute) - 18 * 60) % 1440


def sleep_coach(db: sqlite3.Connection, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    rows = _metrics(db, today - timedelta(days=13), today)
    week = [row for row in rows if row["calendar_date"] >= (today - timedelta(days=6)).isoformat()]
    debt = 0.0
    for row in week:
        need = row.get("sleep_need_minutes") or 480
        if row.get("sleep_seconds") is not None:
            debt += max(0.0, need - row["sleep_seconds"] / 60)
    bedtimes = [b for b in (_bedtime_minutes(row.get("sleep_start")) for row in rows) if b is not None]
    spread = statistics.pstdev(bedtimes) if len(bedtimes) >= 3 else None
    average_bed = (statistics.mean(bedtimes) + 18 * 60) if bedtimes else None
    latest = next((row for row in reversed(rows) if row.get("next_bedtime_start_mins") is not None), None)
    tonight = None
    if latest:
        tonight = {"need_minutes": latest.get("next_sleep_need_minutes"), "window_start": _clock(latest["next_bedtime_start_mins"]),
                   "window_end": _clock(latest.get("next_bedtime_end_mins")), "start_minutes": latest["next_bedtime_start_mins"]}
    consistency = None if spread is None else ("consistent" if spread <= 30 else "a bit variable" if spread <= 60 else "irregular")
    tips = []
    if debt >= 300:
        tips.append(f"You're about {debt / 60:.1f} h short of your sleep need this week. An earlier night or two pays it back faster than a long lie-in.")
    if spread and spread > 45:
        tips.append(f"Your bedtime varies by about ±{round(spread)} min. A steadier bedtime usually improves sleep score and HRV.")
    if tonight and tonight["window_start"]:
        tips.append(f"Garmin suggests getting to bed between {tonight['window_start']} and {tonight['window_end']} tonight.")
    return {"debt_minutes": round(debt), "nights": len(week), "bedtime_spread_minutes": round(spread) if spread is not None else None,
            "consistency": consistency, "average_bedtime": _clock(average_bed), "tonight": tonight, "tips": tips}


# ---------------------------------------------------------------- trends and race predictions

def _week_key(day: str) -> str:
    value = date.fromisoformat(day)
    return (value - timedelta(days=value.weekday())).isoformat()


def top_lifts(db: sqlite3.Connection, limit: int = 4) -> list[str]:
    counts: dict[str, int] = {}
    for row in db.execute("SELECT sl.exercise_index, s.workout_json FROM set_logs sl JOIN sessions s ON s.id = sl.session_id"):
        workout = json.loads(row[1] or "[]")
        if row[0] < len(workout):
            name = canonical_exercise(workout[row[0]].partition("—")[0])
            counts[name] = counts.get(name, 0) + 1
    return [name for name, _ in sorted(counts.items(), key=lambda item: -item[1])[:limit]]


def trends(db: sqlite3.Connection, days: int = 180, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    start = today - timedelta(days=days - 1)
    weeks: dict[str, dict[str, list]] = {}
    for row in _metrics(db, start, today):
        bucket = weeks.setdefault(_week_key(row["calendar_date"]), {})
        for key in ("resting_hr", "hrv_ms", "sleep_seconds", "vo2_max", "steps"):
            if row.get(key) is not None:
                bucket.setdefault(key, []).append(row[key])
    for row in _rows(db, """SELECT day, weight FROM (SELECT checkin_date AS day, weight_lbs AS weight FROM daily_health WHERE weight_lbs IS NOT NULL
                            UNION ALL SELECT measured_on, weight_lbs FROM body_measurements WHERE weight_lbs IS NOT NULL) WHERE day >= ?""", (start.isoformat(),)):
        weeks.setdefault(_week_key(row["day"]), {}).setdefault("weight", []).append(row["weight"])
    for row in db.execute("SELECT activity_date, distance_miles FROM imported_activities WHERE lower(activity_type) LIKE '%run%' AND activity_date >= ?", (start.isoformat(),)):
        weeks.setdefault(_week_key(row[0]), {}).setdefault("run_miles", []).append(row[1] or 0)
    lifts = top_lifts(db)
    bw = bodyweight(db)
    for row in db.execute("""SELECT sl.exercise_index, sl.weight, sl.reps, s.session_date, s.workout_json FROM set_logs sl
                             JOIN sessions s ON s.id = sl.session_id WHERE s.session_date >= ?""", (start.isoformat(),)):
        workout = json.loads(row[4] or "[]")
        if row[0] >= len(workout):
            continue
        name = canonical_exercise(workout[row[0]].partition("—")[0])
        if name in lifts:
            weeks.setdefault(_week_key(row[3]), {}).setdefault(f"lift:{name}", []).append(estimate_1rm(name, row[1], row[2], bw))
    series = []
    for week in sorted(weeks):
        bucket = weeks[week]
        avg = lambda key, scale=1.0: round(_mean(bucket.get(key, [])) / scale, 1) if bucket.get(key) else None
        series.append({
            "week": week, "resting_hr": avg("resting_hr"), "hrv": avg("hrv_ms"), "sleep_hours": avg("sleep_seconds", 3600),
            "vo2_max": round(max(bucket["vo2_max"]), 1) if bucket.get("vo2_max") else None, "steps": avg("steps"),
            "weight": avg("weight"), "run_miles": round(sum(bucket.get("run_miles", [])), 1),
            "lifts": {name: round(max(bucket[f"lift:{name}"]), 1) for name in lifts if bucket.get(f"lift:{name}")},
        })
    vo2 = db.execute("SELECT vo2_max FROM garmin_daily_metrics WHERE vo2_max IS NOT NULL ORDER BY calendar_date DESC LIMIT 1").fetchone()
    return {"days": days, "weeks": series, "lifts": lifts, "race_predictions": race_predictions(float(vo2[0])) if vo2 else None}


def _daniels_vo2(meters_per_minute: float) -> float:
    return -4.60 + 0.182258 * meters_per_minute + 0.000104 * meters_per_minute ** 2


def _daniels_fraction(minutes: float) -> float:
    return 0.8 + 0.1894393 * math.exp(-0.012778 * minutes) + 0.2989558 * math.exp(-0.1932605 * minutes)


def predict_race_minutes(vo2max: float, meters: float) -> float:
    """Daniels/Gilbert: find the time t where the race's oxygen cost equals the sustainable share of VO2max."""
    low, high = 5.0, 600.0
    for _ in range(80):
        mid = (low + high) / 2
        if _daniels_vo2(meters / mid) > vo2max * _daniels_fraction(mid):
            low = mid  # too fast for this VO2max — needs more time
        else:
            high = mid
    return (low + high) / 2


def race_predictions(vo2max: float) -> dict[str, Any]:
    distances = {"5K": 5000, "10K": 10000, "Half marathon": 21097.5, "Marathon": 42195}
    out = {}
    for label, meters in distances.items():
        minutes = predict_race_minutes(vo2max, meters)
        total = round(minutes * 60)
        hours, rest = divmod(total, 3600)
        out[label] = f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"
    return {"vo2_max": vo2max, "times": out, "note": "Garmin VO₂ max-based estimate (Daniels formula); assumes training specific to the distance."}


# ---------------------------------------------------------------- nutrition and body

def protein_target(db: sqlite3.Connection) -> float | None:
    weight = bodyweight(db)
    return round(weight * 0.8) if weight else None  # ~0.8 g per lb of bodyweight


def body_summary(db: sqlite3.Connection, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    entries = _rows(db, """SELECT day, weight, waist, body_fat FROM (
        SELECT checkin_date AS day, weight_lbs AS weight, NULL AS waist, NULL AS body_fat FROM daily_health WHERE weight_lbs IS NOT NULL
        UNION ALL SELECT measured_on, weight_lbs, waist_in, body_fat_pct FROM body_measurements) ORDER BY day""")
    weights = [(row["day"], row["weight"]) for row in entries if row["weight"] is not None]
    recent = [w for d, w in weights if d >= (today - timedelta(days=6)).isoformat()]
    prior = [w for d, w in weights if (today - timedelta(days=34)).isoformat() <= d < (today - timedelta(days=27)).isoformat()]
    avg7 = _mean(recent)
    latest = lambda key: next((row[key] for row in reversed(entries) if row[key] is not None), None)
    protein_rows = _rows(db, "SELECT checkin_date, protein_g, water_l FROM daily_health WHERE checkin_date >= ? ORDER BY checkin_date", ((today - timedelta(days=6)).isoformat(),))
    target = protein_target(db)
    return {
        "weight_7day_avg": round(avg7, 1) if avg7 else None,
        "weight_change_4wk": round(avg7 - _mean(prior), 1) if avg7 and prior else None,
        "latest_weight": weights[-1][1] if weights else None, "latest_waist": latest("waist"), "latest_body_fat": latest("body_fat"),
        "measurements": entries[-20:], "protein_target": target,
        "protein_days": [{"date": row["checkin_date"], "protein_g": row["protein_g"], "water_l": row["water_l"]} for row in protein_rows],
        "protein_avg": round(_mean([row["protein_g"] for row in protein_rows]) or 0) or None,
    }


# ---------------------------------------------------------------- insights

def _compare(label: str, with_values: list[float], without_values: list[float], unit: str, higher_is_better: bool, min_n: int = 3) -> dict[str, Any] | None:
    if len(with_values) < min_n or len(without_values) < min_n:
        return None
    a, b = statistics.mean(with_values), statistics.mean(without_values)
    diff = a - b
    good = (diff > 0) == higher_is_better
    return {"title": label, "difference": round(diff, 1), "unit": unit, "favorable": good if abs(diff) > 0.01 else None,
            "confidence": "early pattern" if min(len(with_values), len(without_values)) < 10 else "consistent pattern",
            "with_avg": round(a, 1), "without_avg": round(b, 1), "n_with": len(with_values), "n_without": len(without_values)}


def insights(db: sqlite3.Connection, today: date | None = None) -> dict[str, Any]:
    """Simple before/after comparisons from your own data. Shown only with enough days on both sides."""
    today = today or date.today()
    since = (today - timedelta(days=180)).isoformat()
    metrics = {row["calendar_date"]: row for row in _rows(db, "SELECT * FROM garmin_daily_metrics WHERE calendar_date >= ?", (since,))}
    checkins = _rows(db, "SELECT checkin_date, tags_json FROM daily_health WHERE checkin_date >= ?", (since,))
    tag_days: dict[str, set[str]] = {}
    for row in checkins:
        for tag in json.loads(row["tags_json"] or "[]"):
            tag_days.setdefault(tag, set()).add(row["checkin_date"])
    checked_days = {row["checkin_date"] for row in checkins}
    found, pending = [], []
    for tag in LIFESTYLE_TAGS:
        days = tag_days.get(tag, set())
        next_morning = lambda day: metrics.get((date.fromisoformat(day) + timedelta(days=1)).isoformat())
        with_rows = [next_morning(day) for day in days]
        without_rows = [next_morning(day) for day in checked_days - days]
        for key, label, unit, better in (("hrv_ms", "HRV", " ms", True), ("resting_hr", "resting HR", " bpm", False), ("sleep_score", "sleep score", "", True)):
            result = _compare(f"Next-morning {label} after “{tag}” days", [r[key] for r in with_rows if r and r.get(key) is not None],
                              [r[key] for r in without_rows if r and r.get(key) is not None], unit, better)
            if result:
                found.append(result)
        if 0 < len(days) < 3:
            pending.append(f"Tag “{tag}” on {3 - len(days)} more day{'s' if 3 - len(days) > 1 else ''} to see its effect.")
    # Lifting performance after good vs short sleep: each session's average % of that lift's best e1RM so far.
    bw = bodyweight(db)
    best: dict[str, float] = {}
    sessions: dict[str, list[float]] = {}
    for row in db.execute("""SELECT sl.exercise_index, sl.weight, sl.reps, s.session_date, s.workout_json FROM set_logs sl
                             JOIN sessions s ON s.id = sl.session_id ORDER BY s.session_date"""):
        workout = json.loads(row[4] or "[]")
        if row[0] >= len(workout):
            continue
        name = canonical_exercise(workout[row[0]].partition("—")[0])
        e1rm = estimate_1rm(name, row[1], row[2], bw)
        if e1rm and best.get(name):
            sessions.setdefault(row[3], []).append(e1rm / best[name] * 100)
        best[name] = max(best.get(name, 0), e1rm)
    good_sleep, short_sleep = [], []
    for day, ratios in sessions.items():
        night = metrics.get(day)
        if night and night.get("sleep_seconds") is not None:
            (good_sleep if night["sleep_seconds"] >= 7 * 3600 else short_sleep).append(statistics.mean(ratios))
    lift = _compare("Lifting strength after 7h+ sleep vs less", good_sleep, short_sleep, "% of best", True)
    if lift:
        found.append(lift)
    elif sessions:
        pending.append("Log a few more workouts on nights with Garmin sleep data to compare lifting after good vs short sleep.")
    # Training load the day before vs next-morning HRV.
    loads = daily_training_load(db, today - timedelta(days=180), today)
    heavy = [metrics[(date.fromisoformat(d) + timedelta(days=1)).isoformat()]["hrv_ms"] for d, v in loads.items() if v >= 60
             and metrics.get((date.fromisoformat(d) + timedelta(days=1)).isoformat(), {}).get("hrv_ms") is not None]
    light = [m["hrv_ms"] for d, m in metrics.items() if m.get("hrv_ms") is not None and loads.get((date.fromisoformat(d) - timedelta(days=1)).isoformat(), 0) < 20]
    load_effect = _compare("Next-morning HRV after big training days (load 60+)", heavy, light, " ms", True)
    if load_effect:
        found.append(load_effect)
    found.sort(key=lambda item: -abs(item["difference"]))
    return {"insights": found, "pending": pending[:4]}


# ---------------------------------------------------------------- pain

def active_pain(db: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows(db, "SELECT * FROM pain_log WHERE resolved_on IS NULL ORDER BY severity DESC, logged_on DESC")


def pain_warning(exercise: str, pains: list[dict[str, Any]]) -> dict[str, Any] | None:
    lowered = canonical_exercise(exercise).lower()
    hits = [pain for pain in pains if pain["severity"] >= 3 and any(keyword in lowered for keyword in PAIN_AREAS.get(pain["area"], []))]
    if not hits:
        return None
    worst = max(hits, key=lambda pain: pain["severity"])
    return {"area": worst["area"], "severity": worst["severity"],
            "message": f"You logged {worst['area']} pain ({worst['severity']}/10). Go lighter, stop if it hurts, or swap this exercise."}


RELATED_GROUPS = {"Legs": ["Glutes", "Hamstrings", "Calves"], "Hamstrings": ["Glutes"], "Glutes": ["Hamstrings"],
                  "Chest": ["Triceps"], "Shoulders": ["Back"], "Back": ["Biceps"], "Triceps": [], "Biceps": [], "Core": []}


def safe_swaps(exercise: str, group: str, pains: list[dict[str, Any]], limit: int = 3) -> list[str]:
    """Same-group exercises that avoid the painful area; if none, related groups (e.g. legs → glutes/hamstrings)."""
    from catalog import EXERCISES
    risky = [keyword for pain in pains if pain["severity"] >= 3 for keyword in PAIN_AREAS.get(pain["area"], [])]
    for groups in ([group], RELATED_GROUPS.get(group, [])):
        swaps = [name for name, g, _, _ in EXERCISES if g in groups and name != exercise and not any(k in name.lower() for k in risky)]
        if swaps:
            return swaps[:limit]
    return []


# ---------------------------------------------------------------- goals and habits

def goal_progress(db: sqlite3.Connection, goal: dict[str, Any]) -> dict[str, Any]:
    current = None
    kind = goal["kind"]
    if kind == "lift":
        bw = bodyweight(db)
        best = 0.0
        for row in db.execute("SELECT sl.exercise_index, sl.weight, sl.reps, s.workout_json FROM set_logs sl JOIN sessions s ON s.id = sl.session_id"):
            workout = json.loads(row[3] or "[]")
            if row[0] < len(workout) and canonical_exercise(workout[row[0]].partition("—")[0]).lower() == canonical_exercise(goal["exercise"] or "").lower():
                best = max(best, estimate_1rm(goal["exercise"], row[1], row[2], bw))
        current = best or None
    elif kind == "bodyweight":
        current = body_summary(db)["weight_7day_avg"] or bodyweight(db)
    elif kind == "weekly_miles":
        current = training_load(db)["run_miles"]["acute"]
    elif kind == "vo2_max":
        row = db.execute("SELECT vo2_max FROM garmin_daily_metrics WHERE vo2_max IS NOT NULL ORDER BY calendar_date DESC LIMIT 1").fetchone()
        current = row[0] if row else None
    start, target = goal.get("start_value"), goal["target"]
    percent = None
    if current is not None and start is not None and target != start:
        percent = max(0, min(100, round((current - start) / (target - start) * 100)))
    elif current is not None and start is None:
        percent = max(0, min(100, round(current / target * 100))) if target else None
    achieved = current is not None and ((target >= (start or 0) and current >= target) or (start is not None and target < start and current <= target))
    return {**goal, "current": round(current, 1) if current is not None else None, "percent": percent, "achieved": achieved}


def habit_status(db: sqlite3.Connection, today: date | None = None, days: int = 14) -> list[dict[str, Any]]:
    today = today or date.today()
    window = [(today - timedelta(days=i)).isoformat() for i in range(days)]
    metrics = {row["calendar_date"]: row for row in _metrics(db, today - timedelta(days=days), today)}
    checkins = {row["checkin_date"]: row for row in _rows(db, "SELECT * FROM daily_health WHERE checkin_date >= ?", (window[-1],))}
    target = protein_target(db)
    out = []
    for habit in _rows(db, "SELECT * FROM habits WHERE active = 1 ORDER BY id"):
        done_days = set()
        if habit["auto_key"] == "sleep_7h":
            done_days = {d for d, m in metrics.items() if (m.get("sleep_seconds") or 0) >= 7 * 3600}
        elif habit["auto_key"] == "steps_goal":
            done_days = {d for d, m in metrics.items() if m.get("steps") and m.get("step_goal") and m["steps"] >= m["step_goal"]}
        elif habit["auto_key"] == "protein_goal":
            done_days = {d for d, c in checkins.items() if (c.get("protein_g") and target and c["protein_g"] >= target) or (c.get("protein_goal_met") and not c.get("protein_g"))}
        elif habit["auto_key"] == "checked_in":
            done_days = set(checkins)
        else:
            done_days = {row[0] for row in db.execute("SELECT checked_on FROM habit_checks WHERE habit_id = ? AND checked_on >= ?", (habit["id"], window[-1]))}
        streak = 0
        for index, day in enumerate(window):
            if day in done_days:
                streak += 1
            elif index == 0:
                continue  # today isn't over yet — don't break the streak
            else:
                break
        out.append({**habit, "automatic": bool(habit["auto_key"]), "done_today": today.isoformat() in done_days, "streak": streak,
                    "history": [day in done_days for day in reversed(window)]})  # oldest → today
    return out


# ---------------------------------------------------------------- alerts (Today screen)

def alerts(db: sqlite3.Connection, today: date | None = None) -> list[dict[str, Any]]:
    today = today or date.today()
    out = []
    illness = illness_signal(db, today)
    if illness["warning"]:
        out.append({"level": "warn", "kind": "illness", "title": "Possible illness coming on", "message": illness["message"]})
    load = training_load(db, today)
    if load["status"] == "high":
        out.append({"level": "warn", "kind": "load", "title": f"Training load spike ({load['ratio']}× your norm)", "message": load["message"]})
    sleep = sleep_coach(db, today)
    if sleep["debt_minutes"] >= 300:
        out.append({"level": "info", "kind": "sleep", "title": f"{sleep['debt_minutes'] / 60:.1f} h of sleep debt this week", "message": sleep["tips"][0] if sleep["tips"] else ""})
    for pain in active_pain(db):
        if pain["severity"] >= 5:
            out.append({"level": "info", "kind": "pain", "title": f"{pain['area'].capitalize()} pain {pain['severity']}/10", "message": "Workouts flag exercises that load this area."})
    return out
