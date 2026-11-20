"""Offline checks for the Garmin sync: retry/backoff, per-day saves, field extraction.

Every test points the app at a temporary database and never calls Garmin.
"""

from __future__ import annotations

import io
import sqlite3
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import garmin_sync  # noqa: E402


@pytest.fixture(autouse=True)
def temp_database(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app, "DATABASE", tmp_path / "test.db")
    monkeypatch.setattr(app, "LEGACY_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(garmin_sync.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(garmin_sync, "_pacer", garmin_sync._Pacer())
    return tmp_path / "test.db"


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError("https://example.test", code, "error", headers, None)


def test_request_retries_429_then_succeeds(monkeypatch):
    calls = []
    sleeps = []
    monkeypatch.setattr(garmin_sync.time, "sleep", sleeps.append)

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        if len(calls) < 3:
            raise _http_error(429, retry_after="7")
        return _Response(b'{"ok": true}')

    monkeypatch.setattr(garmin_sync.urllib.request, "urlopen", fake_urlopen)
    assert garmin_sync._request("https://example.test", headers={}) == {"ok": True}
    assert len(calls) == 3
    assert sleeps.count(7.0) == 2  # Retry-After honored on both 429s


def test_request_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setenv("GARMIN_MAX_RETRIES", "2")
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(1)
        raise _http_error(429)

    monkeypatch.setattr(garmin_sync.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(garmin_sync.GarminFatalError):
        garmin_sync._request("https://example.test", headers={})
    assert len(calls) == 3


def test_partial_sync_keeps_days_saved_before_failure(monkeypatch, temp_database):
    monkeypatch.setattr(garmin_sync, "access_token", lambda: "token")
    days_seen = []

    def fake_get(path, token, **params):
        if "activitylist" in path:
            return []
        day = params.get("calendarDate") or params.get("date") or path.rsplit("/", 1)[-1]
        if day not in days_seen:
            days_seen.append(day)
        if len(days_seen) == 3:
            raise garmin_sync.GarminFatalError("Garmin kept rate-limiting requests after several retries.")
        if "usersummary" in path:
            return {"totalSteps": 1000 + len(days_seen)}
        return {}

    monkeypatch.setattr(garmin_sync, "garmin_get", fake_get)
    result = garmin_sync.run_sync(days=5)

    assert result["status"] == "partial"
    assert result["metrics_updated"] == 2
    db = sqlite3.connect(temp_database)
    assert db.execute("SELECT COUNT(*) FROM garmin_daily_metrics").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM garmin_raw_responses").fetchone()[0] == 8
    assert db.execute("SELECT status FROM garmin_sync_log").fetchone()[0] == "partial"


def test_null_from_failed_endpoint_does_not_erase_existing_value(temp_database):
    app.initialize()
    with app.connection() as db:
        garmin_sync.ensure_schema(db)
        day = "2026-01-05"
        garmin_sync.save_day(db, garmin_sync.daily_metric(day, {"summary": {"totalSteps": 9000}}), {}, "t1")
        garmin_sync.save_day(db, garmin_sync.daily_metric(day, {}), {}, "t2")
        assert db.execute("SELECT steps FROM garmin_daily_metrics WHERE calendar_date = ?", (day,)).fetchone()[0] == 9000


def test_garmin_activities_match_same_day_sessions_once():
    app.initialize()
    with app.connection() as db:
        garmin_sync.ensure_schema(db)
        sessions = [
            ("s-lift", "2026-01-05", "strength", "planned"),
            ("s-run", "2026-01-05", "run", "planned"),
            ("s-done", "2026-01-06", "strength", "completed"),
        ]
        for sid, day, kind, status in sessions:
            db.execute("INSERT INTO sessions (id, session_date, kind, title, minutes, status) VALUES (?, ?, ?, ?, 60, ?)", (sid, day, kind, f"W1 · {kind}", status))
        activities = [
            ("g1", "2026-01-05", "Strength Training", None), ("g2", "2026-01-05", "Treadmill Running", 3.0),
            ("g3", "2026-01-06", "Strength Training", None), ("g4", "2026-01-07", "Strength Training", None), ("g5", "2026-01-05", "Yoga", None),
        ]
        for aid, day, kind, miles in activities:
            db.execute("INSERT INTO imported_activities (id, source, activity_date, activity_type, title, distance_miles, duration_minutes, average_hr, imported_at) VALUES (?, 'Garmin API', ?, ?, ?, ?, 30, 120, 't')", (aid, day, kind, kind, miles))
        assert app.match_garmin_activities(db) == 3
        links = dict(db.execute("SELECT id, linked_session_id FROM imported_activities").fetchall())
        assert links == {"g1": "s-lift", "g2": "s-run", "g3": "s-done", "g4": None, "g5": None}
        statuses = dict(db.execute("SELECT id, status FROM sessions").fetchall())
        assert statuses == {"s-lift": "completed", "s-run": "completed", "s-done": "completed"}
        # The already-completed session is only linked; no extra completion log is written.
        assert db.execute("SELECT COUNT(*) FROM logs WHERE session_id = 's-done'").fetchone()[0] == 0
        assert app.match_garmin_activities(db) == 0  # idempotent


def test_lifting_history_import_merges_by_date_and_is_rerunnable(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    import import_lifting_history as importer

    csv_file = tmp_path / "history.csv"
    csv_file.write_text(
        "date,workout,exercise,set,weight_lbs,reps,rpe,notes\n"
        "2026-01-05,Push,Barbell Bench Press,1,225,5,,date approximate\n"
        "2026-01-05,Chest Tri Shoulder,Barbell Bench Press,2,225,4,,date approximate\n"
        "2026-01-05,Chest Tri Shoulder,Rope Pushdown,1,50,10,,\n"
        "2026-01-07,Back Bi,Lat Pulldown,1,170,8,,deload; date approximate\n",
        encoding="utf-8",
    )
    app.initialize()
    with app.connection() as db:
        garmin_sync.ensure_schema(db)
        db.execute("INSERT INTO sessions (id, session_date, kind, title, minutes, status, workout_json) VALUES ('plan-1', '2026-01-05', 'strength', 'W1 · Back & Biceps', 60, 'planned', '[]')")
        db.execute("INSERT INTO imported_activities (id, source, activity_date, activity_type, title, duration_minutes, imported_at) VALUES ('g7', 'Garmin API', '2026-01-07', 'Strength Training', 'Strength', 41, 't')")

    workouts = importer.read_workouts(csv_file)
    assert [(w["date"], w["title"], w["set_count"]) for w in workouts] == [("2026-01-05", "Chest Tri Shoulder", 3), ("2026-01-07", "Back Bi", 1)]
    for _ in range(2):  # second run must not duplicate anything
        importer.import_workouts(workouts)

    with app.connection() as db:
        sessions = {row["id"]: dict(row) for row in db.execute("SELECT * FROM sessions")}
        assert set(sessions) == {"plan-1", "history-2026-01-07"}
        assert sessions["plan-1"]["title"] == "Chest Tri Shoulder" and sessions["plan-1"]["status"] == "completed"
        assert "planned: W1 · Back & Biceps" in sessions["plan-1"]["description"]
        assert sessions["history-2026-01-07"]["minutes"] == 41  # Garmin duration
        assert db.execute("SELECT COUNT(*) FROM set_logs").fetchone()[0] == 4
        assert db.execute("SELECT note FROM set_logs WHERE session_id = 'history-2026-01-07'").fetchone()[0] == "deload"
        assert db.execute("SELECT linked_session_id FROM imported_activities WHERE id = 'g7'").fetchone()[0] == "history-2026-01-07"
        assert db.execute("SELECT COUNT(*) FROM logs").fetchone()[0] == 2
    bench = next(item for item in app.progress()["exercises"] if item["name"] == "Barbell Bench Press")
    assert bench["best_set"]["weight"] == 225 and len(bench["history"]) == 2


def test_daily_metric_extracts_extended_fields():
    payloads = {
        "summary": {"totalSteps": 8123, "floorsAscended": 12.0, "moderateIntensityMinutes": 20, "vigorousIntensityMinutes": 15, "avgWakingRespirationValue": 14.0},
        "sleep": {"dailySleepDTO": {"sleepTimeSeconds": 27000, "deepSleepSeconds": 5400, "remSleepSeconds": 6000, "sleepScores": {"overall": {"value": 84, "qualifierKey": "GOOD"}}, "sleepStartTimestampLocal": 1767650400000}},
        "hrv": {"hrvSummary": {"lastNightAvg": 58, "weeklyAvg": 61, "status": "BALANCED", "baseline": {"balancedLow": 52, "balancedUpper": 70}}},
        "heart_rate": {"restingHeartRate": 49},
    }
    record = garmin_sync.daily_metric("2026-01-06", payloads)
    assert record["steps"] == 8123
    assert record["floors_ascended"] == 12
    assert record["intensity_minutes_vigorous"] == 15
    assert record["sleep_score"] == 84
    assert record["sleep_deep_seconds"] == 5400
    assert record["sleep_start"] == "2026-01-05T22:00"
    assert record["hrv_ms"] == 58
    assert record["hrv_status"] == "BALANCED"
    assert record["hrv_baseline_low"] == 52
    assert record["resting_hr"] == 49
