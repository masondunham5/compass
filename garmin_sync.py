"""Pull a private Garmin Connect Personal API snapshot into Health Hub.

Run manually first:
    .\\.venv\\Scripts\\python.exe garmin_sync.py --days 7

The only credential is GARMIN_PERSONAL_API_KEY in the local .env file. This
module deliberately uses the official personal API—not a Garmin password or an
unofficial web-scraping library.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any

from app import activity_id, connection, initialize, normalize_activity


BASE_URL = "https://personal.connectapi.garmin.com"
TOKEN_URL = f"{BASE_URL}/personalapitoken-service/personal-api/personal-api-key/token"


class GarminSyncError(RuntimeError):
    """A safe, user-facing error; never include the secret key in its text."""


def _request(url: str, *, headers: dict[str, str], method: str = "GET") -> Any:
    request = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            body = response.read().decode("utf-8", errors="replace").strip()
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            raise GarminSyncError("Garmin rejected the key. It may be expired, revoked, or copied incorrectly.") from error
        if error.code == 429:
            raise GarminSyncError("Garmin is temporarily rate-limiting requests. Try again later.") from error
        raise GarminSyncError(f"Garmin request failed ({error.code}). Try again later.") from error
    except urllib.error.URLError as error:
        raise GarminSyncError("Health Hub could not reach Garmin. Check this computer’s internet connection.") from error
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def access_token() -> str:
    key = os.getenv("GARMIN_PERSONAL_API_KEY", "").strip()
    if not key:
        raise GarminSyncError("Garmin is not connected. Add GARMIN_PERSONAL_API_KEY to the local .env file first.")
    payload = _request(TOKEN_URL, method="POST", headers={"Authorization": f"ApiKey {key}", "Accept": "*/*"})
    if isinstance(payload, dict):
        token = payload.get("access_token") or payload.get("token")
    else:
        token = payload
    if not isinstance(token, str) or len(token.strip()) < 20:
        raise GarminSyncError("Garmin returned an unexpected sign-in response. Check that the key is still active.")
    return token.strip()


def garmin_get(path: str, token: str, **params: Any) -> Any:
    query = {key: value for key, value in params.items() if value is not None}
    suffix = f"?{urllib.parse.urlencode(query)}" if query else ""
    return _request(f"{BASE_URL}{path}{suffix}", headers={"Authorization": f"Bearer {token}", "Accept": "*/*"})


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


def _number(value: Any) -> float | int | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _first_record(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return {}


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
        "details": {"garmin_activity_id": raw.get("activityId"), "start_time": start},
    }
    normalized = normalize_activity(candidate, "garmin-api")
    if normalized and raw.get("activityId"):
        normalized["id"] = f"garmin-api-{raw['activityId']}"
    return normalized


def daily_metric(calendar_date: str, summary: Any, sleep: Any, hrv: Any, heart_rate: Any) -> dict[str, Any]:
    summary_record = _first_record(summary)
    sleep_record = _first_record(sleep)
    hrv_record = _first_record(hrv)
    heart_record = _first_record(heart_rate)
    return {
        "calendar_date": calendar_date,
        "steps": _number(_walk(summary_record, "totalSteps", "steps")),
        "active_calories": _number(_walk(summary_record, "activeKilocalories", "activeCalories")),
        "total_calories": _number(_walk(summary_record, "totalKilocalories", "totalCalories")),
        "resting_hr": _number(_walk(heart_record, "restingHeartRate", "restingHR")),
        "hrv_ms": _number(_walk(hrv_record, "lastNightAvg", "weeklyAvg", "hrvValue", "avgHrv")),
        "sleep_seconds": _number(_walk(sleep_record, "sleepTimeSeconds", "sleepSeconds", "durationInSeconds")),
        "stress_avg": _number(_walk(summary_record, "averageStressLevel", "avgStressLevel")),
        "body_battery": _number(_walk(summary_record, "bodyBatteryMostRecentValue", "bodyBattery")),
        "source_json": json.dumps({"summary": summary_record, "sleep": sleep_record, "hrv": hrv_record, "heart_rate": heart_record}),
    }


def run_sync(days: int = 2) -> dict[str, Any]:
    """Sync a small rolling history. Safe to run repeatedly; rows upsert by date/ID."""
    days = max(1, min(int(days), 30))
    initialize()
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    with connection() as db:
        log_id = db.execute("INSERT INTO garmin_sync_log (started_at, status, message) VALUES (?, 'running', '')", (started,)).lastrowid
    try:
        token = access_token()
        end = date.today()
        start = end - timedelta(days=days - 1)
        activities_payload = garmin_get(
            "/activitylist-service/activities/search/activities", token,
            start=0, limit=100, startDate=start.isoformat(), endDate=end.isoformat(),
        )
        activities = activities_payload.get("activities", []) if isinstance(activities_payload, dict) else (activities_payload or [])
        imported = 0
        with connection() as db:
            for raw in activities:
                if not isinstance(raw, dict):
                    continue
                activity = activity_from_api(raw)
                if not activity:
                    continue
                cursor = db.execute(
                    """INSERT OR IGNORE INTO imported_activities
                       (id, source, activity_date, activity_type, title, distance_miles, duration_minutes, average_hr, max_hr, elevation_feet, details_json, imported_at)
                       VALUES (?, 'Garmin API', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (activity["id"], activity["activity_date"], activity["activity_type"], activity["title"], activity["distance_miles"], activity["duration_minutes"], activity["average_hr"], activity["max_hr"], activity["elevation_feet"], json.dumps(activity["details"]), started),
                )
                imported += int(bool(cursor.rowcount))

        metrics_updated = 0
        with connection() as db:
            for offset in range(days):
                current = (end - timedelta(days=offset)).isoformat()
                summary = garmin_get("/usersummary-service/usersummary/daily", token, calendarDate=current)
                sleep = garmin_get("/sleep-service/sleep/dailySleep", token, date=current)
                hrv = garmin_get(f"/hrv-service/hrv/{current}", token)
                heart = garmin_get("/wellness-service/wellness/dailyHeartRate", token, date=current)
                record = daily_metric(current, summary, sleep, hrv, heart)
                db.execute(
                    """INSERT INTO garmin_daily_metrics
                       (calendar_date, steps, active_calories, total_calories, resting_hr, hrv_ms, sleep_seconds, stress_avg, body_battery, source_json, synced_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(calendar_date) DO UPDATE SET steps=excluded.steps, active_calories=excluded.active_calories,
                       total_calories=excluded.total_calories, resting_hr=excluded.resting_hr, hrv_ms=excluded.hrv_ms,
                       sleep_seconds=excluded.sleep_seconds, stress_avg=excluded.stress_avg, body_battery=excluded.body_battery,
                       source_json=excluded.source_json, synced_at=excluded.synced_at""",
                    (record["calendar_date"], record["steps"], record["active_calories"], record["total_calories"], record["resting_hr"], record["hrv_ms"], record["sleep_seconds"], record["stress_avg"], record["body_battery"], record["source_json"], started),
                )
                metrics_updated += 1
            db.execute("UPDATE garmin_sync_log SET finished_at = ?, status = 'success', activities_imported = ?, metrics_updated = ?, message = ? WHERE id = ?", (datetime.now().astimezone().isoformat(timespec="seconds"), imported, metrics_updated, "Sync complete.", log_id))
        return {"status": "success", "activities_imported": imported, "metrics_updated": metrics_updated}
    except GarminSyncError as error:
        with connection() as db:
            db.execute("UPDATE garmin_sync_log SET finished_at = ?, status = 'error', message = ? WHERE id = ?", (datetime.now().astimezone().isoformat(timespec="seconds"), str(error), log_id))
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync private Garmin data into Health Hub.")
    parser.add_argument("--days", type=int, default=int(os.getenv("GARMIN_SYNC_DAYS", "2")), help="Rolling days to refresh (1–30).")
    args = parser.parse_args()
    result = run_sync(args.days)
    print(f"Garmin sync complete: {result['activities_imported']} activities, {result['metrics_updated']} daily records.")
