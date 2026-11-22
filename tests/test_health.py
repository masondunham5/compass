"""Health features: load, illness warning, sleep coach, race predictor, insights, pain, goals, habits, reports."""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import core  # noqa: E402
import health  # noqa: E402

TODAY = date.today()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    monkeypatch.setattr(core, "DATABASE", tmp_path / "test.db")
    monkeypatch.setattr(core, "LEGACY_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(core, "BACKUP_DIR", tmp_path / "backups")
    core.initialize()
    with core.connection() as connection:
        yield connection


def day(offset: int) -> str:
    return (TODAY - timedelta(days=offset)).isoformat()


def add_metrics(db, offset: int, **values):
    columns = ["calendar_date", "source_json", "synced_at", *values]
    db.execute(f"INSERT INTO garmin_daily_metrics ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
               (day(offset), "{}", "t", *values.values()))


def add_activity(db, offset: int, load: float, kind: str = "Running", miles: float = 3.0):
    raw = {"activityTrainingLoad": load, "hrTimeInZone_2": 1200, "trainingEffectLabel": "AEROBIC_BASE"}
    db.execute("INSERT INTO imported_activities (id, source, activity_date, activity_type, title, distance_miles, imported_at, raw_json) VALUES (?, 'Garmin API', ?, ?, 'x', ?, 't', ?)",
               (f"a{offset}-{load}", day(offset), kind, miles, json.dumps(raw)))


def test_training_load_flags_a_spike(db):
    for offset in range(7, 35):
        add_activity(db, offset, 40)  # steady ~280/week
    for offset in range(0, 7):
        add_activity(db, offset, 100)  # 700 this week
    load = health.training_load(db)
    assert load["acute"] == 700 and load["status"] == "high" and load["ratio"] > 1.5
    assert load["zone_minutes"][1] == 140  # 7 activities × 20 min in zone 2
    assert any(alert["kind"] == "load" for alert in health.alerts(db))


def test_illness_warning_needs_two_flagged_days(db):
    for offset in range(3, 31):
        add_metrics(db, offset, resting_hr=50, hrv_ms=50, sleep_respiration_avg=14)
    add_metrics(db, 1, resting_hr=57, hrv_ms=40, sleep_respiration_avg=15.5)
    assert not health.illness_signal(db)["warning"]
    add_metrics(db, 0, resting_hr=58, hrv_ms=39, sleep_respiration_avg=15.4)
    signal = health.illness_signal(db)
    assert signal["warning"] and len(signal["flagged_days"]) == 2
    assert "breathing rate up" in signal["flagged_days"][0]["signs"]


def test_sleep_coach_debt_and_tonights_window(db):
    for offset in range(7):
        add_metrics(db, offset, sleep_seconds=6 * 3600, sleep_need_minutes=480, sleep_start=f"{day(offset + 1)}T23:30",
                    next_sleep_need_minutes=490, next_bedtime_start_mins=1400, next_bedtime_end_mins=40)
    coach = health.sleep_coach(db)
    assert coach["debt_minutes"] == 7 * 120
    assert coach["tonight"]["window_start"] == "11:20 PM" and coach["tonight"]["window_end"] == "12:40 AM"
    assert coach["consistency"] == "consistent" and coach["average_bedtime"] == "11:30 PM"


def test_race_predictor_matches_daniels_tables():
    times = health.race_predictions(50)["times"]
    assert times["5K"].startswith("19:") or times["5K"].startswith("20:0")  # Daniels VDOT 50 ≈ 19:57
    hours, minutes, _ = map(int, times["Marathon"].split(":"))
    assert hours == 3 and 5 <= minutes <= 15  # ≈ 3:10


def test_insights_compare_tagged_days(db):
    for offset in range(1, 13):
        tags = ["alcohol"] if offset % 2 else []
        db.execute("INSERT INTO daily_health (checkin_date, sleep_hours, sleep_quality, energy, soreness, stress, protein_goal_met, notes, updated_at, tags_json) VALUES (?, 7, 7, 7, 3, 4, 0, '', 't', ?)",
                   (day(offset), json.dumps(tags)))
        add_metrics(db, offset - 1, hrv_ms=40 if offset % 2 else 52, resting_hr=50)
    result = health.insights(db)
    alcohol = next(item for item in result["insights"] if "alcohol" in item["title"] and "HRV" in item["title"])
    assert alcohol["difference"] == -12 and alcohol["favorable"] is False and alcohol["confidence"] == "early pattern"


def test_pain_flags_risky_exercises_and_suggests_swaps(db):
    pains = [{"area": "shoulder (left)", "severity": 6}]
    assert health.pain_warning("Seated Dumbbell Shoulder Press", pains)["area"] == "shoulder (left)"
    assert health.pain_warning("Lat Pulldown", pains) is None
    assert health.pain_warning("Seated Dumbbell Shoulder Press", [{"area": "shoulder (left)", "severity": 2}]) is None
    chest_swaps = health.safe_swaps("Barbell Bench Press", "Chest", pains)
    assert not any("press" in name.lower() or "fly" in name.lower() for name in chest_swaps)  # may be empty: chest work loads the shoulder
    knee = [{"area": "knee (left)", "severity": 5}]
    leg_swaps = health.safe_swaps("Back Squat", "Legs", knee)
    assert leg_swaps and not any(word in name.lower() for name in leg_swaps for word in ("squat", "lunge", "leg extension", "leg press", "step-up"))


def test_health_api_end_to_end(db, tmp_path):
    with TestClient(app.app) as client:
        assert client.get("/api/health/hub").status_code == 200
        client.put("/api/settings", json={"bodyweight_lbs": 180})
        saved = client.post("/api/health/checkin", json={"sleep_hours": 7, "sleep_quality": 7, "energy": 7, "soreness": 3, "stress": 4,
                                                          "tags": ["Alcohol", "travel"], "protein_g": 150, "water_l": 3}).json()
        assert saved["status"] == "saved"
        row = db.execute("SELECT tags_json, protein_g, protein_goal_met FROM daily_health").fetchone()
        assert json.loads(row[0]) == ["alcohol", "travel"] and row[1] == 150 and row[2] == 1  # 150 g ≥ 0.8 × 180
        habits = client.get("/api/health/habits").json()["habits"]
        assert next(h for h in habits if h["auto_key"] == "protein_goal")["done_today"]
        manual = next(h for h in habits if not h["automatic"])
        assert next(h for h in client.post(f"/api/health/habits/{manual['id']}/toggle").json()["habits"] if h["id"] == manual["id"])["done_today"]
        assert client.post(f"/api/health/habits/{habits[0]['id']}/toggle").status_code == 400  # automatic habit
        assert client.post("/api/health/pain", json={"area": "elbow", "severity": 5}).status_code == 200
        assert len(client.post("/api/health/pain", json={"area": "elbow", "severity": 3}).json()["pain"]) == 1  # updates, no duplicate
        assert client.post("/api/health/pain", json={"area": "spleen", "severity": 3}).status_code == 400
        assert client.post("/api/health/records", json={"kind": "blood_pressure", "name": "BP", "value": "118"}).status_code == 400
        records = client.post("/api/health/records", json={"kind": "blood_pressure", "name": "BP", "value": "118/76", "unit": "mmHg"}).json()["records"]
        client.post("/api/health/records", json={"kind": "supplement", "name": "Creatine", "value": "5", "unit": "g"})
        assert client.post(f"/api/health/records/{records[0]['id']}/toggle").status_code == 200
        assert client.post("/api/health/body", json={"weight_lbs": 182, "waist_in": 33}).json()["latest_waist"] == 33
        goals = client.post("/api/health/goals", json={"kind": "bodyweight", "title": "Cut", "target": 175}).json()["goals"]
        assert goals[0]["current"] == 182 and goals[0]["start_value"] == 182
        assert client.post("/api/health/goals", json={"kind": "lift", "title": "Bench", "target": 250}).status_code == 400
        doctor = client.get("/api/health/report/doctor")
        assert doctor.status_code == 200 and "118/76" in doctor.text and "Creatine" in doctor.text and "elbow" in doctor.text
        monthly = client.get(f"/api/health/report/monthly?month={TODAY.strftime('%Y-%m')}")
        assert monthly.status_code == 200 and TODAY.strftime("%B %Y") in monthly.text
        assert client.get("/api/health/report/monthly?month=nope").status_code == 400
        assert client.put("/api/health/reminders", json={"enabled": True, "checkin": "07:30", "bedtime": True}).json()["checkin"] == "07:30"
        assert client.put("/api/health/reminders", json={"checkin": "25:00"}).status_code == 422
