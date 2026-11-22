"""HTTP-level checks through FastAPI's TestClient (startup, routes, validation), on a temp database."""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import core  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    monkeypatch.setattr(core, "DATABASE", tmp_path / "test.db")
    monkeypatch.setattr(core, "LEGACY_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(core, "BACKUP_DIR", tmp_path / "backups")
    core.initialize()
    with TestClient(app.app) as test_client:  # runs the lifespan: initialize + daily backup
        yield test_client


def test_startup_makes_one_daily_backup(client, tmp_path):
    assert len(list((tmp_path / "backups").glob("auto_compass_*.db"))) == 1
    assert core.auto_backup() is None  # a second call the same day does nothing


def test_old_database_name_is_moved_once_and_never_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATABASE", tmp_path / "compass.db")
    (tmp_path / "lifting_hub.db").write_bytes(b"old")
    assert core.migrate_database_name() is True
    assert (tmp_path / "compass.db").read_bytes() == b"old" and not (tmp_path / "lifting_hub.db").exists()
    (tmp_path / "lifting_hub.db").write_bytes(b"stale")
    assert core.migrate_database_name() is False  # compass.db exists: leave both alone
    assert (tmp_path / "compass.db").read_bytes() == b"old"


def test_core_pages_respond(client):
    for path in ["/api/today", "/api/overview", "/api/garmin/dashboard", "/api/history", "/api/progress", "/api/review/week",
                 "/api/review/week?offset=-1", "/api/races", "/api/run-types", "/api/templates", "/api/settings", "/api/exercises/catalog?q=bench"]:
        response = client.get(path)
        assert response.status_code == 200, path
    assert client.get("/").headers["cache-control"].startswith("no-store")
    assert client.get("/api/exercises/catalog?q=bench").json()["results"][0]["name"] == "Barbell Bench Press"


def test_build_and_edit_a_workout_over_http(client):
    today = date.today().isoformat()
    session = client.post("/api/sessions", json={"session_date": today, "title": "Push", "workout": ["Barbell Bench Press — 3 × 5"]}).json()
    sid = session["id"]
    assert client.post(f"/api/sessions/{sid}/exercises", json={"name": "Cable Fly", "sets": 2, "reps": "12"}).status_code == 200
    assert client.put(f"/api/sessions/{sid}/sets", json={"exercise_index": 0, "set_number": 1, "weight": 225, "reps": 5}).status_code == 200
    detail = client.patch(f"/api/sessions/{sid}/exercises/1", json={"sets": 3, "weight": 30}).json()
    assert detail["exercises"][1]["target_sets"] == 3 and detail["exercises"][1]["target_weight"] == 30
    assert client.delete(f"/api/sessions/{sid}/exercises/5").status_code == 404
    assert client.post(f"/api/sessions/{sid}/exercises", json={"name": "Dips", "replace_index": 0}).status_code == 409  # sets logged
    assert client.post(f"/api/sessions/{sid}/complete", json={"minutes": 40}).status_code == 200
    assert client.get("/api/history").json()["sessions"][0]["sets_logged"] == 1


def test_validation_errors_are_clear(client):
    assert client.post("/api/sessions", json={"session_date": "not-a-date", "title": "Push"}).status_code == 400
    bad_goal = client.post("/api/races/preview", json={"name": "X race", "race_date": (date.today() + timedelta(days=200)).isoformat(), "distance": "half", "goal_time": "fast"})
    assert bad_goal.status_code == 400 and "goal time" in bad_goal.json()["detail"].lower()
    assert client.put("/api/settings", json={"bodyweight_lbs": 20}).status_code == 422
    assert client.get("/api/sessions/does-not-exist").status_code == 404


def test_bodyweight_setting_feeds_pullup_progress(client):
    today = date.today().isoformat()
    sid = client.post("/api/sessions", json={"session_date": today, "title": "Pull", "workout": ["Pull-up — 3 × 8"]}).json()["id"]
    client.put(f"/api/sessions/{sid}/sets", json={"exercise_index": 0, "set_number": 1, "weight": 0, "reps": 10})
    assert client.get("/api/progress").json()["exercises"][0]["best_set"]["e1rm"] == 0
    assert client.put("/api/settings", json={"bodyweight_lbs": 180}).json()["effective_bodyweight"] == 180
    assert client.get("/api/progress").json()["exercises"][0]["best_set"]["e1rm"] == 240.0  # 180 × (1 + 10/30)
