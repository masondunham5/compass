"""Pull a private Garmin Connect Personal API snapshot into Health Hub.

Run manually first:
    .\\.venv\\Scripts\\python.exe garmin_sync.py --days 7

The only credential is GARMIN_PERSONAL_API_KEY in the local .env file. This
module deliberately uses the official personal API—not a Garmin password or an
unofficial web-scraping library.

Requests are paced (GARMIN_REQUEST_INTERVAL seconds apart, default 1.5) and
429/5xx responses are retried with exponential backoff, honoring Retry-After.
Each day is committed as soon as it is fetched, so a sync that stops early
keeps everything it already pulled. Every endpoint's full response is kept in
garmin_raw_responses; the useful fields are also copied into columns on
garmin_daily_metrics.
"""

from __future__ import annotations

import argparse
import email.utils
import json
import os
import random
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from activities import match_garmin_activities, normalize_activity
from core import auto_backup, connection, initialize


BASE_URL = "https://personal.connectapi.garmin.com"
TOKEN_URL = f"{BASE_URL}/personalapitoken-service/personal-api/personal-api-key/token"

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_BACKOFF_SECONDS = 120.0


class GarminSyncError(RuntimeError):
    """A safe, user-facing error; never include the secret key in its text."""


class GarminFatalError(GarminSyncError):
    """Stops the sync: auth failure, persistent rate limiting, or no network."""


class GarminEndpointError(GarminSyncError):
    """One endpoint failed for one day; the rest of the sync can continue."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# HTTP: pacing + retry
# --------------------------------------------------------------------------- #

def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, "") or default))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, "") or default))
    except ValueError:
        return default


class _Pacer:
    """Keeps a minimum gap between requests; widens it after a 429."""

    def __init__(self) -> None:
        self.interval = _env_float("GARMIN_REQUEST_INTERVAL", 1.5)
        self._last = 0.0

    def wait(self) -> None:
        delay = self._last + self.interval - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._last = time.monotonic()

    def slow_down(self) -> None:
        self.interval = min(10.0, max(self.interval * 2, 2.0))


_pacer = _Pacer()


def _retry_after_seconds(error: urllib.error.HTTPError) -> float | None:
    header = error.headers.get("Retry-After") if error.headers else None
    if not header:
        return None
    header = header.strip()
    if header.isdigit():
        return float(header)
    try:
        when = email.utils.parsedate_to_datetime(header)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def _backoff(attempt: int) -> float:
    return min(MAX_BACKOFF_SECONDS, 5.0 * (2 ** attempt)) + random.uniform(0, 1)


def _request(url: str, *, headers: dict[str, str], method: str = "GET") -> Any:
    max_retries = _env_int("GARMIN_MAX_RETRIES", 5)
    attempt = 0
    while True:
        _pacer.wait()
        request = urllib.request.Request(url, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                body = response.read().decode("utf-8", errors="replace").strip()
            break
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                raise GarminFatalError("Garmin rejected the key. It may be expired, revoked, or copied incorrectly.") from error
            if error.code in RETRYABLE_STATUS and attempt < max_retries:
                if error.code == 429:
                    _pacer.slow_down()
                wait = _retry_after_seconds(error)
                wait = min(MAX_BACKOFF_SECONDS, wait) if wait is not None else _backoff(attempt)
                time.sleep(wait)
                attempt += 1
                continue
            if error.code == 429:
                raise GarminFatalError("Garmin kept rate-limiting requests after several retries. Try again later.") from error
            if error.code in {204, 404}:
                return None  # No data for that day/endpoint.
            raise GarminEndpointError(f"Garmin request failed ({error.code}).", error.code) from error
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt < max_retries:
                time.sleep(_backoff(attempt))
                attempt += 1
                continue
            raise GarminFatalError("Health Hub could not reach Garmin. Check this computer’s internet connection.") from error
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def access_token() -> str:
    key = os.getenv("GARMIN_PERSONAL_API_KEY", "").strip()
    if not key:
        raise GarminFatalError("Garmin is not connected. Add GARMIN_PERSONAL_API_KEY to the local .env file first.")
    try:
        payload = _request(TOKEN_URL, method="POST", headers={"Authorization": f"ApiKey {key}", "Accept": "*/*"})
    except GarminEndpointError as error:
        raise GarminFatalError(f"Garmin sign-in failed ({error.status}). Try again later.") from error
    if isinstance(payload, dict):
        token = payload.get("access_token") or payload.get("token")
    else:
        token = payload
    if not isinstance(token, str) or len(token.strip()) < 20:
        raise GarminFatalError("Garmin returned an unexpected sign-in response. Check that the key is still active.")
    return token.strip()


def garmin_get(path: str, token: str, **params: Any) -> Any:
    query = {key: value for key, value in params.items() if value is not None}
    suffix = f"?{urllib.parse.urlencode(query)}" if query else ""
    return _request(f"{BASE_URL}{path}{suffix}", headers={"Authorization": f"Bearer {token}", "Accept": "*/*"})


# --------------------------------------------------------------------------- #
# Field extraction
# --------------------------------------------------------------------------- #

def _walk(value: Any, *keys: str) -> Any:
    """Find a known Garmin field across minor response-shape variations."""
    if isinstance(value, dict):
        for key in keys:
            if value.get(key) is not None:
                return value[key]
        for child in value.values():
            found = _walk(child, *keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _walk(child, *keys)
            if found is not None:
                return found
    return None


def _path(value: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if isinstance(value, list):
            value = value[0] if value else None
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _find(value: Any, *keys: str) -> Any:
    """Dotted keys are exact paths; plain keys are searched recursively."""
    for key in keys:
        found = _path(value, key) if "." in key else _walk(value, key)
        if found is not None:
            return found
    return None


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    return str(value)


def _local_timestamp(value: Any) -> str | None:
    """Garmin '...TimestampLocal' values are epoch ms of local wall-clock time."""
    number = _number(value)
    if number is None:
        return _text(value)
    return datetime.fromtimestamp(number / 1000, tz=timezone.utc).replace(tzinfo=None).isoformat(timespec="minutes")


def _first_record(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return {}


# (column, SQL type, endpoint, candidate keys, converter)
Field = tuple[str, str, str, tuple[str, ...], Callable[[Any], Any]]

METRIC_FIELDS: list[Field] = [
    # Original columns (read by the dashboard and the recommendation engine).
    ("steps", "INTEGER", "summary", ("totalSteps", "steps"), _number),
    ("active_calories", "REAL", "summary", ("activeKilocalories", "activeCalories"), _number),
    ("total_calories", "REAL", "summary", ("totalKilocalories", "totalCalories"), _number),
    ("resting_hr", "INTEGER", "heart_rate", ("restingHeartRate", "restingHR"), _number),
    ("hrv_ms", "REAL", "hrv", ("lastNightAvg", "weeklyAvg", "hrvValue", "avgHrv"), _number),
    ("sleep_seconds", "INTEGER", "sleep", ("sleepTimeSeconds", "sleepSeconds", "durationInSeconds"), _number),
    ("stress_avg", "REAL", "summary", ("averageStressLevel", "avgStressLevel"), _number),
    ("body_battery", "INTEGER", "summary", ("bodyBatteryMostRecentValue", "bodyBattery"), _number),
    # Activity / movement
    ("step_goal", "INTEGER", "summary", ("dailyStepGoal",), _number),
    ("distance_meters", "REAL", "summary", ("totalDistanceMeters",), _number),
    ("floors_ascended", "REAL", "summary", ("floorsAscended",), _number),
    ("floors_descended", "REAL", "summary", ("floorsDescended",), _number),
    ("floors_goal", "REAL", "summary", ("userFloorsAscendedGoal", "floorsAscendedGoal"), _number),
    ("intensity_minutes_moderate", "INTEGER", "summary", ("moderateIntensityMinutes",), _number),
    ("intensity_minutes_vigorous", "INTEGER", "summary", ("vigorousIntensityMinutes",), _number),
    ("intensity_minutes_goal", "INTEGER", "summary", ("intensityMinutesGoal",), _number),
    ("active_seconds", "INTEGER", "summary", ("activeSeconds",), _number),
    ("highly_active_seconds", "INTEGER", "summary", ("highlyActiveSeconds",), _number),
    ("sedentary_seconds", "INTEGER", "summary", ("sedentarySeconds",), _number),
    ("bmr_calories", "REAL", "summary", ("bmrKilocalories",), _number),
    # Heart rate
    ("min_hr", "INTEGER", "heart_rate", ("minHeartRate",), _number),
    ("max_hr", "INTEGER", "heart_rate", ("maxHeartRate",), _number),
    ("resting_hr_7d_avg", "INTEGER", "heart_rate", ("lastSevenDaysAvgRestingHeartRate",), _number),
    # Stress / Body Battery
    ("stress_max", "INTEGER", "summary", ("maxStressLevel",), _number),
    ("body_battery_high", "INTEGER", "summary", ("bodyBatteryHighestValue",), _number),
    ("body_battery_low", "INTEGER", "summary", ("bodyBatteryLowestValue",), _number),
    ("body_battery_charged", "INTEGER", "summary", ("bodyBatteryChargedValue",), _number),
    ("body_battery_drained", "INTEGER", "summary", ("bodyBatteryDrainedValue",), _number),
    # Respiration / SpO2
    ("respiration_waking_avg", "REAL", "summary", ("avgWakingRespirationValue",), _number),
    ("respiration_low", "REAL", "summary", ("lowestRespirationValue",), _number),
    ("respiration_high", "REAL", "summary", ("highestRespirationValue",), _number),
    ("spo2_avg", "REAL", "summary", ("averageSpo2",), _number),
    ("spo2_low", "REAL", "summary", ("lowestSpo2",), _number),
    # Sleep
    ("sleep_score", "INTEGER", "sleep", ("dailySleepDTO.sleepScores.overall.value", "sleepScores.overall.value", "overallSleepScore"), _number),
    ("sleep_score_qualifier", "TEXT", "sleep", ("dailySleepDTO.sleepScores.overall.qualifierKey", "sleepScores.overall.qualifierKey"), _text),
    ("sleep_deep_seconds", "INTEGER", "sleep", ("deepSleepSeconds",), _number),
    ("sleep_light_seconds", "INTEGER", "sleep", ("lightSleepSeconds",), _number),
    ("sleep_rem_seconds", "INTEGER", "sleep", ("remSleepSeconds",), _number),
    ("sleep_awake_seconds", "INTEGER", "sleep", ("awakeSleepSeconds",), _number),
    ("sleep_awake_count", "INTEGER", "sleep", ("awakeCount",), _number),
    ("sleep_start", "TEXT", "sleep", ("sleepStartTimestampLocal",), _local_timestamp),
    ("sleep_end", "TEXT", "sleep", ("sleepEndTimestampLocal",), _local_timestamp),
    ("sleep_respiration_avg", "REAL", "sleep", ("averageRespirationValue",), _number),
    ("sleep_respiration_low", "REAL", "sleep", ("lowestRespirationValue",), _number),
    ("sleep_respiration_high", "REAL", "sleep", ("highestRespirationValue",), _number),
    ("sleep_stress_avg", "REAL", "sleep", ("avgSleepStress",), _number),
    ("sleep_avg_hr", "REAL", "sleep", ("avgHeartRate",), _number),
    ("sleep_need_minutes", "INTEGER", "sleep", ("sleepNeed.actual",), _number),
    ("nap_seconds", "INTEGER", "sleep", ("napTimeSeconds",), _number),
    ("body_battery_during_sleep", "INTEGER", "summary", ("bodyBatteryDuringSleep",), _number),
    ("body_battery_at_wake", "INTEGER", "summary", ("bodyBatteryAtWakeTime",), _number),
    ("stress_qualifier", "TEXT", "summary", ("stressQualifier",), _text),
    ("stress_high_seconds", "INTEGER", "summary", ("highStressDuration",), _number),
    # HRV
    ("hrv_weekly_avg", "REAL", "hrv", ("weeklyAvg",), _number),
    ("hrv_last_night_high", "REAL", "hrv", ("lastNight5MinHigh",), _number),
    ("hrv_status", "TEXT", "hrv", ("hrvSummary.status", "status"), _text),
    ("hrv_baseline_low", "REAL", "hrv", ("balancedLow",), _number),
    ("hrv_baseline_high", "REAL", "hrv", ("balancedUpper",), _number),
    ("hrv_feedback", "TEXT", "hrv", ("feedbackPhrase",), _text),
    # VO2 max comes from a separate range endpoint (see sync_vo2_max); only days Garmin updated it have a value.
    ("vo2_max", "REAL", "vo2max", ("vo2MaxPreciseValue", "vo2MaxValue"), _number),
]

VO2_MAX_PATH = "/metrics-service/metrics/maxmet/daily/{start}/{end}"
VO2_MAX_LOOKBACK_DAYS = 30

ORIGINAL_COLUMNS = {"steps", "active_calories", "total_calories", "resting_hr", "hrv_ms", "sleep_seconds", "stress_avg", "body_battery"}

DAILY_ENDPOINTS: dict[str, Callable[[str], tuple[str, dict[str, Any]]]] = {
    "summary": lambda day: ("/usersummary-service/usersummary/daily", {"calendarDate": day}),
    "sleep": lambda day: ("/sleep-service/sleep/dailySleep", {"date": day}),
    "hrv": lambda day: (f"/hrv-service/hrv/{day}", {}),
    "heart_rate": lambda day: ("/wellness-service/wellness/dailyHeartRate", {"date": day}),
}


def ensure_schema(db: sqlite3.Connection) -> None:
    """Add the Garmin raw table and any metric columns missing from older databases."""
    db.execute(
        """CREATE TABLE IF NOT EXISTS garmin_raw_responses (
               calendar_date TEXT NOT NULL,
               endpoint TEXT NOT NULL,
               payload_json TEXT,
               fetched_at TEXT NOT NULL,
               PRIMARY KEY (calendar_date, endpoint)
           )"""
    )
    existing = {row[1] for row in db.execute("PRAGMA table_info(garmin_daily_metrics)")}
    for column, sql_type, *_ in METRIC_FIELDS:
        if column not in existing:
            db.execute(f"ALTER TABLE garmin_daily_metrics ADD COLUMN {column} {sql_type}")
    activity_columns = {row[1] for row in db.execute("PRAGMA table_info(imported_activities)")}
    if "raw_json" not in activity_columns:
        db.execute("ALTER TABLE imported_activities ADD COLUMN raw_json TEXT")


def daily_metric(calendar_date: str, payloads: dict[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = {"calendar_date": calendar_date}
    for column, _type, endpoint, keys, convert in METRIC_FIELDS:
        record[column] = convert(_find(payloads.get(endpoint), *keys))
    record["source_json"] = json.dumps({name: _first_record(payloads.get(name)) for name in DAILY_ENDPOINTS})
    return record


def save_day(db: sqlite3.Connection, record: dict[str, Any], payloads: dict[str, Any], synced_at: str) -> None:
    for endpoint, payload in payloads.items():
        db.execute(
            """INSERT INTO garmin_raw_responses (calendar_date, endpoint, payload_json, fetched_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(calendar_date, endpoint) DO UPDATE SET payload_json=excluded.payload_json, fetched_at=excluded.fetched_at""",
            (record["calendar_date"], endpoint, json.dumps(payload), synced_at),
        )
    columns = [field[0] for field in METRIC_FIELDS]
    # A null from Garmin (e.g. one endpoint failed) never erases a value we already have.
    updates = ", ".join(f"{c}=COALESCE(excluded.{c}, garmin_daily_metrics.{c})" for c in columns)
    db.execute(
        f"""INSERT INTO garmin_daily_metrics (calendar_date, {', '.join(columns)}, source_json, synced_at)
            VALUES (?, {', '.join('?' for _ in columns)}, ?, ?)
            ON CONFLICT(calendar_date) DO UPDATE SET {updates}, source_json=excluded.source_json, synced_at=excluded.synced_at""",
        (record["calendar_date"], *(record[c] for c in columns), record["source_json"], synced_at),
    )


def sync_vo2_max(token: str, end: date) -> int:
    """One request for the last 30 days of VO2 max; stores raw per day and fills vo2_max."""
    start = end - timedelta(days=VO2_MAX_LOOKBACK_DAYS - 1)
    try:
        payload = garmin_get(VO2_MAX_PATH.format(start=start.isoformat(), end=end.isoformat()), token)
    except GarminEndpointError:
        return 0
    updated = 0
    now = _now()
    with connection() as db:
        for item in payload if isinstance(payload, list) else []:
            day = _find(item, "generic.calendarDate", "calendarDate")
            value = _number(_find(item, "vo2MaxPreciseValue", "vo2MaxValue"))
            if not day or value is None:
                continue
            db.execute(
                """INSERT INTO garmin_raw_responses (calendar_date, endpoint, payload_json, fetched_at) VALUES (?, 'vo2max', ?, ?)
                   ON CONFLICT(calendar_date, endpoint) DO UPDATE SET payload_json=excluded.payload_json, fetched_at=excluded.fetched_at""",
                (day, json.dumps(item), now),
            )
            db.execute(
                """INSERT INTO garmin_daily_metrics (calendar_date, vo2_max, source_json, synced_at) VALUES (?, ?, '{}', ?)
                   ON CONFLICT(calendar_date) DO UPDATE SET vo2_max=excluded.vo2_max""",
                (day, value, now),
            )
            updated += 1
    return updated


# --------------------------------------------------------------------------- #
# Activities
# --------------------------------------------------------------------------- #

def activity_from_api(raw: dict[str, Any]) -> dict[str, Any] | None:
    start = str(raw.get("startTimeLocal") or raw.get("startTimeGMT") or "")
    activity_type = raw.get("activityType") or {}
    type_name = activity_type.get("typeKey") if isinstance(activity_type, dict) else activity_type
    distance_meters = _number(raw.get("distance"))
    candidate = {
        "activity_date": start[:10],
        "activity_type": str(type_name or "Activity").replace("_", " ").title(),
        "title": raw.get("activityName") or raw.get("activityTypeDTO", {}).get("displayKey") or "Garmin activity",
        "distance_miles": round(distance_meters / 1609.344, 2) if distance_meters else None,
        "duration_minutes": round((_number(raw.get("duration")) or 0) / 60, 1) or None,
        "average_hr": _number(raw.get("averageHR") or raw.get("averageHeartRate")),
        "max_hr": _number(raw.get("maxHR") or raw.get("maxHeartRate")),
        "elevation_feet": round((_number(raw.get("elevationGain")) or 0) * 3.28084, 1) or None,
        "details": {
            "garmin_activity_id": raw.get("activityId"),
            "start_time": start,
            # Garmin may only include this on compatible running activities.
            "vo2_max": _number(_walk(raw, "vO2MaxValue", "vo2MaxValue", "vo2max", "vo2Max")),
        },
    }
    normalized = normalize_activity(candidate, "garmin-api")
    if normalized and raw.get("activityId"):
        normalized["id"] = f"garmin-api-{raw['activityId']}"
    return normalized


def save_activities(db: sqlite3.Connection, activities: list[Any], imported_at: str) -> int:
    imported = 0
    for raw in activities:
        if not isinstance(raw, dict):
            continue
        activity = activity_from_api(raw)
        if not activity:
            continue
        cursor = db.execute(
            """INSERT INTO imported_activities
               (id, source, activity_date, activity_type, title, distance_miles, duration_minutes, average_hr, max_hr, elevation_feet, details_json, raw_json, imported_at)
               VALUES (?, 'Garmin API', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 source=excluded.source, activity_date=excluded.activity_date, activity_type=excluded.activity_type,
                 title=excluded.title, distance_miles=excluded.distance_miles, duration_minutes=excluded.duration_minutes,
                 average_hr=excluded.average_hr, max_hr=excluded.max_hr, elevation_feet=excluded.elevation_feet,
                 details_json=excluded.details_json, raw_json=excluded.raw_json, imported_at=excluded.imported_at""",
            (activity["id"], activity["activity_date"], activity["activity_type"], activity["title"], activity["distance_miles"], activity["duration_minutes"], activity["average_hr"], activity["max_hr"], activity["elevation_feet"], json.dumps(activity["details"]), json.dumps(raw), imported_at),
        )
        imported += int(bool(cursor.rowcount))
    return imported


# --------------------------------------------------------------------------- #
# Sync
# --------------------------------------------------------------------------- #

def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def run_sync(days: int = 2, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Sync a rolling history, newest day first. Safe to run repeatedly; rows upsert by date/ID.

    Returns status "success", or "partial" if the sync stopped early (saved days
    are kept). Raises GarminSyncError only when nothing could be saved.
    """
    days = max(1, min(int(days), 30))
    report = progress or (lambda _message: None)
    initialize()
    try:
        auto_backup()  # snapshot before the sync writes anything (at most once a day)
    except (OSError, sqlite3.Error):
        pass
    started = _now()
    with connection() as db:
        ensure_schema(db)
        log_id = db.execute("INSERT INTO garmin_sync_log (started_at, status, message) VALUES (?, 'running', '')", (started,)).lastrowid

    def update_log(status: str, message: str, imported: int, saved: int, finished: bool) -> None:
        with connection() as db:
            db.execute(
                "UPDATE garmin_sync_log SET finished_at = ?, status = ?, activities_imported = ?, metrics_updated = ?, message = ? WHERE id = ?",
                (_now() if finished else None, status, imported, saved, message, log_id),
            )

    imported = 0
    saved = 0
    endpoint_failures: list[str] = []
    end = date.today()
    start = end - timedelta(days=days - 1)
    try:
        token = access_token()
        try:
            activities_payload = garmin_get(
                "/activitylist-service/activities/search/activities", token,
                start=0, limit=100, startDate=start.isoformat(), endDate=end.isoformat(),
            )
        except GarminEndpointError as error:
            endpoint_failures.append(f"activities ({error.status})")
            activities_payload = None
        activities = activities_payload.get("activities", []) if isinstance(activities_payload, dict) else (activities_payload or [])
        with connection() as db:
            imported = save_activities(db, activities, started)
            matched = match_garmin_activities(db)
        report(f"Activities: {imported} saved, {matched} matched to planned sessions.")
        report(f"VO2 max: {sync_vo2_max(token, end)} days.")
        update_log("running", "", imported, saved, finished=False)

        for offset in range(days):
            current = (end - timedelta(days=offset)).isoformat()
            payloads: dict[str, Any] = {}
            for name, endpoint in DAILY_ENDPOINTS.items():
                path, params = endpoint(current)
                try:
                    payloads[name] = garmin_get(path, token, **params)
                except GarminEndpointError as error:
                    endpoint_failures.append(f"{current} {name} ({error.status})")
            record = daily_metric(current, payloads)
            with connection() as db:
                save_day(db, record, payloads, _now())
            saved += 1
            update_log("running", "", imported, saved, finished=False)
            report(f"{current}: saved ({sum(name in payloads for name in DAILY_ENDPOINTS)}/{len(DAILY_ENDPOINTS)} endpoints).")
    except GarminFatalError as error:
        if saved == 0 and imported == 0:
            update_log("error", str(error), imported, saved, finished=True)
            raise
        message = f"Stopped after {saved} of {days} days: {error} Saved days were kept; run the sync again to finish."
        update_log("partial", message, imported, saved, finished=True)
        return {"status": "partial", "activities_imported": imported, "metrics_updated": saved, "message": message}

    message = "Sync complete."
    if endpoint_failures:
        shown = ", ".join(endpoint_failures[:5]) + (" …" if len(endpoint_failures) > 5 else "")
        message = f"Sync complete; {len(endpoint_failures)} request(s) failed and were skipped: {shown}"
    update_log("success", message, imported, saved, finished=True)
    return {"status": "success", "activities_imported": imported, "metrics_updated": saved, "message": message}


def rebuild_from_raw() -> int:
    """Re-derive every garmin_daily_metrics column from stored raw responses (no API calls)."""
    initialize()
    with connection() as db:
        ensure_schema(db)
        by_day: dict[str, dict[str, Any]] = {}
        for row in db.execute("SELECT calendar_date, endpoint, payload_json FROM garmin_raw_responses"):
            by_day.setdefault(row["calendar_date"], {})[row["endpoint"]] = json.loads(row["payload_json"]) if row["payload_json"] else None
        for day, payloads in by_day.items():
            save_day(db, daily_metric(day, payloads), {}, _now())
    return len(by_day)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync private Garmin data into Health Hub.")
    parser.add_argument("--days", type=int, default=int(os.getenv("GARMIN_SYNC_DAYS", "2")), help="Rolling days to refresh (1–30).")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild metric columns from stored raw responses without calling Garmin.")
    args = parser.parse_args()
    if args.rebuild:
        print(f"Rebuilt {rebuild_from_raw()} days from stored raw responses.")
        raise SystemExit(0)
    result = run_sync(args.days, progress=lambda line: print(line, flush=True))
    print(f"Garmin sync {result['status']}: {result['activities_imported']} activities, {result['metrics_updated']} daily records. {result['message']}")
