"""Run spacing checks on reschedules, and Fuel & prep cards that follow the real schedule."""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import core  # noqa: E402
from assistant import runprep, schedule  # noqa: E402

TODAY = date.today()


def day(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    monkeypatch.setattr(core, "DATABASE", tmp_path / "test.db")
    monkeypatch.setattr(core, "LEGACY_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(core, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setenv("SCHOOL_TODO_APP", str(tmp_path / "no_school" / "app.py"))
    monkeypatch.setenv("SCHOOL_TODO_DATA", str(tmp_path / "no_school"))
    core.initialize()
    with core.connection() as connection:
        connection.execute("DELETE FROM sessions")
        connection.commit()
        yield connection


def session(db, sid, offset, kind, title, minutes=60, status="planned", description=""):
    db.execute("INSERT INTO sessions (id, session_date, kind, title, minutes, description, status, locked, source) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'x')",
               (sid, day(offset), kind, title, minutes, description, status))


def test_classify():
    class Row(dict):  # stands in for sqlite3.Row
        def keys(self):
            return super().keys()
    assert runprep.classify(Row(kind="run", title="W5 · Long Run", description="")) == {"run", "long"}
    assert runprep.classify(Row(kind="run", title="Tempo", description="Build: tempo run")) == {"run", "hard"}
    assert runprep.classify(Row(kind="run", title="W5 · Easy Run", description="easy effort")) == {"run"}
    assert runprep.classify(Row(kind="strength", title="W5 · Legs", description="")) == {"legs"}
    assert runprep.classify(Row(kind="strength", title="W3 · Lower · Hinge Focus", description="")) == {"legs"}
    assert runprep.classify(Row(kind="strength", title="W5 · Chest, Triceps & Shoulders", description="")) == set()


def test_moving_the_long_run_after_legs_warns_and_shows_in_the_preview(db):
    session(db, "legs", 1, "strength", "W5 · Legs")
    session(db, "long", 4, "run", "W5 · Long Run", 30)
    session(db, "tempo", 6, "run", "W5 · Tempo", 40, description="Build: tempo run")
    plan = schedule.move_session(db, "long", day(2))
    assert any("Long Run" in issue and "day after Legs" in issue for issue in plan["spacing"])
    plan = schedule.move_session(db, "long", day(5))  # the tempo would be the day after
    assert any("Tempo" in issue and "day after the long run" in issue for issue in plan["spacing"])
    assert schedule.move_session(db, "long", day(3))["spacing"] == []  # fine
    db.commit()
    with TestClient(app.app) as client:
        preview = client.post("/api/schedule/preview", json={"action": "move", "session_id": "long", "to_date": day(2)}).json()
        assert preview["spacing"] and preview["changes"]


def test_existing_problems_are_not_blamed_on_an_unrelated_move(db):
    session(db, "legs", 1, "strength", "W5 · Legs")
    session(db, "long", 2, "run", "W5 · Long Run", 30)       # already the day after legs
    session(db, "chest", 5, "strength", "W5 · Chest")
    assert schedule.move_session(db, "chest", day(6))["spacing"] == []


def test_completed_sessions_count(db):
    session(db, "done-legs", 0, "strength", "Legs", status="completed")  # "I just did legs today"
    session(db, "long", 3, "run", "W5 · Long Run", 30)
    assert schedule.move_session(db, "long", day(1))["spacing"]


def test_fuel_cards_follow_the_run_and_scale_with_length(db):
    now = datetime.combine(TODAY, datetime.min.time()).replace(hour=7)
    session(db, "easy", 0, "run", "W5 · Easy Run", 25)
    session(db, "long", 1, "run", "W12 · Long Run", 100)
    db.execute("INSERT INTO plan_blocks (id, block_date, start, end, kind, ref, title, course, accepted_at) VALUES ('blk', ?, 450, 550, 'workout', 'long', 'Long Run', '', 't')",
               (day(1),))
    cards = {c["when"]: c for c in runprep.cards(db, now=now)}
    assert "no special fuel" in " ".join(cards["Today"]["items"])                 # 25 min easy: keep it simple
    tonight = cards["Tonight"]
    assert tonight["title"] == "Tomorrow: Long Run at 7:30 AM · 100 min"
    text = " ".join(tonight["items"])
    assert "Carb-forward dinner" in text and "bed by about 11:00 PM" in text and "fuel" in text

    # Move the long run three days out: tonight's card goes, a heads-up appears instead.
    db.execute("UPDATE sessions SET session_date = ? WHERE id = 'long'", (day(3),))
    db.execute("DELETE FROM plan_blocks")
    cards = {c["when"]: c for c in runprep.cards(db, now=now)}
    assert "Tonight" not in cards
    ahead = cards[(TODAY + timedelta(days=3)).strftime("%A")]
    assert "gels or chews" in " ".join(ahead["items"])


def test_short_long_run_does_not_get_carb_loading(db):
    now = datetime.combine(TODAY, datetime.min.time()).replace(hour=7)
    session(db, "long", 1, "run", "W5 · Long Run", 30)
    tonight = next(c for c in runprep.cards(db, now=now) if c["when"] == "Tonight")
    assert "Normal dinner is fine" in tonight["items"][0]
    db.commit()
    with TestClient(app.app) as client:
        assert client.get("/api/assistant/fuel").json()["cards"]


def test_week_view_lists_spacing_problems_already_in_the_plan(db):
    session(db, "legs", 2, "strength", "W5 · Legs")
    session(db, "long", 2, "run", "W5 · Long Run", 30)       # same day, already planned
    assert any("same day" in issue for issue in runprep.spacing_issues(db, [], window=(TODAY, TODAY + timedelta(days=6))))
    db.commit()
    with TestClient(app.app) as client:
        warnings = client.get(f"/api/assistant/calendar?start={day(0)}&days=7").json()["warnings"]
        assert any("Legs and Long Run on the same day" in w for w in warnings)
