"""Rescheduling: move, skip, push back, protections, conflicts, and undo."""

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

TODAY = date.today()


def day(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    monkeypatch.setattr(core, "DATABASE", tmp_path / "test.db")
    monkeypatch.setattr(core, "LEGACY_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(core, "BACKUP_DIR", tmp_path / "backups")
    core.initialize()
    with core.connection() as db:
        rows = [
            ("chest", 0, "strength", "planned", 0, "plan:x"), ("run", 1, "run", "planned", 0, "plan:x"),
            ("back", 2, "strength", "planned", 0, "plan:x"), ("locked", 3, "strength", "planned", 1, "plan:x"),
            ("done", -1, "strength", "completed", 0, "plan:x"), ("legs", 5, "strength", "planned", 0, "plan:x"),
        ]
        for sid, offset, kind, status, locked, source in rows:
            db.execute("INSERT INTO sessions (id, session_date, kind, title, minutes, status, locked, source) VALUES (?, ?, ?, ?, 60, ?, ?, ?)",
                       (sid, day(offset), kind, sid.title(), status, locked, source))
    with TestClient(app.app) as test_client:
        yield test_client


def dates(client) -> dict[str, str]:
    with core.connection() as db:
        return {row[0]: row[1] for row in db.execute("SELECT id, session_date FROM sessions")}


def test_preview_changes_nothing_and_push_keeps_spacing(client):
    before = dates(client)
    preview = client.post("/api/schedule/preview", json={"action": "push", "days": 2}).json()
    assert dates(client) == before
    assert [c["id"] for c in preview["changes"]] == ["chest", "run", "back", "legs"]
    assert {s["id"]: s["reason"] for s in preview["skipped"]} == {"locked": "locked"}
    applied = client.post("/api/schedule/apply", json={"action": "push", "days": 2}).json()
    after = dates(client)
    assert after["chest"] == day(2) and after["back"] == day(4) and after["legs"] == day(7)
    assert after["locked"] == day(3) and after["done"] == day(-1)  # locked and completed stay
    assert applied["summary"] == "Moved 4 sessions. 1 stayed put."


def test_push_without_runs_and_conflict_warning(client):
    preview = client.post("/api/schedule/preview", json={"action": "push", "days": 1, "include_runs": False}).json()
    assert "run" not in [c["id"] for c in preview["changes"]]
    # chest moves onto tomorrow; back moves onto the locked session's day -> two strength sessions there
    assert any(conflict["date"] == day(3) for conflict in preview["conflicts"])


def test_skip_to_tomorrow_and_undo(client):
    client.post("/api/schedule/apply", json={"action": "skip", "session_id": "chest", "mode": "tomorrow"})
    assert dates(client)["chest"] == day(1) and dates(client)["back"] == day(2)
    assert client.get("/api/schedule/last").json()["summary"] == "Moved 1 session."
    undo = client.post("/api/schedule/undo").json()
    assert undo["restored"] == 1 and dates(client)["chest"] == day(0)
    assert client.post("/api/schedule/undo").json()["restored"] == 0


def test_skip_and_push_moves_everything_after(client):
    client.post("/api/schedule/apply", json={"action": "skip", "session_id": "back", "mode": "push"})
    after = dates(client)
    assert after["back"] == day(3) and after["legs"] == day(6) and after["chest"] == day(0)


def test_cannot_move_completed_locked_or_logged(client):
    client.put("/api/sessions/legs/sets", json={"exercise_index": 0, "set_number": 1, "weight": 100, "reps": 5})
    for sid, reason in (("done", "already completed"), ("locked", "locked"), ("legs", "has logged sets")):
        result = client.post("/api/schedule/preview", json={"action": "move", "session_id": sid, "to_date": day(10)}).json()
        assert not result["changes"] and result["skipped"][0]["reason"] == reason
    assert client.post("/api/schedule/preview", json={"action": "move", "session_id": "chest", "to_date": "soon"}).status_code == 400
    assert client.post("/api/schedule/preview", json={"action": "move", "session_id": "chest"}).status_code == 400


def test_race_runs_never_cross_race_day(client):
    with core.connection() as db:
        db.execute("INSERT INTO races (id, name, race_date, distance, created_at) VALUES ('r1', 'Test', ?, 'half', 't')", (day(4),))
        db.execute("INSERT INTO sessions (id, session_date, kind, title, minutes, status, source) VALUES ('shakeout', ?, 'run', 'Easy run', 20, 'planned', 'race:r1')", (day(3),))
        db.execute("INSERT INTO sessions (id, session_date, kind, title, minutes, status, source) VALUES ('raceday', ?, 'run', 'Race: Test', 120, 'planned', 'race:r1')", (day(4),))
    preview = client.post("/api/schedule/preview", json={"action": "push", "days": 1}).json()
    reasons = {s["id"]: s["reason"] for s in preview["skipped"]}
    assert reasons["shakeout"] == "would land on or after race day" and reasons["raceday"] == "race day"


def test_workout_type_ignores_week_tag_and_variant():
    from assistant.schedule import workout_type
    assert workout_type("W5 · Chest, Triceps & Shoulders · Volume") == workout_type("Chest, Triceps & Shoulders") == "chest, triceps & shoulders"
    assert workout_type("W6 · Back & Biceps") == "back & biceps"


def test_swap_two_workouts_and_the_next_pairs_then_undo(client):
    with core.connection() as db:
        rows = [("c1", 10, "W5 · Chest"), ("b1", 11, "W5 · Back"), ("r1", 12, "W5 · Easy Run"), ("c2", 13, "W5 · Chest · Volume"),
                ("b2", 14, "W5 · Back · Volume"), ("c3", 17, "W6 · Chest"), ("b3", 18, "W6 · Back")]
        for sid, offset, title in rows:
            kind = "run" if "Run" in title else "strength"
            db.execute("INSERT INTO sessions (id, session_date, kind, title, minutes, status, locked, source) VALUES (?, ?, ?, ?, 60, 'planned', 0, 'plan:x')",
                       (sid, day(offset), kind, title))
    options = client.get("/api/schedule/swap-options/c1").json()["sessions"]
    assert "b1" in [s["id"] for s in options] and "c1" not in [s["id"] for s in options]
    before = dates(client)
    preview = client.post("/api/schedule/preview", json={"action": "swap", "session_id": "c1", "other_id": "b1", "repeat": 1}).json()
    assert dates(client) == before and len(preview["changes"]) == 4
    client.post("/api/schedule/apply", json={"action": "swap", "session_id": "c1", "other_id": "b1", "repeat": 1})
    after = dates(client)
    assert (after["b1"], after["c1"], after["b2"], after["c2"]) == (day(10), day(11), day(13), day(14))
    assert (after["c3"], after["b3"], after["r1"]) == (day(17), day(18), day(12))  # only one extra pair
    assert client.post("/api/schedule/undo").json()["restored"] == 4 and dates(client) == before


def test_swap_with_a_locked_workout_moves_neither(client):
    preview = client.post("/api/schedule/preview", json={"action": "swap", "session_id": "back", "other_id": "locked"}).json()
    assert preview["changes"] == [] and {s["id"] for s in preview["skipped"]} == {"back", "locked"}
    assert client.post("/api/schedule/preview", json={"action": "swap", "session_id": "back"}).status_code == 400
