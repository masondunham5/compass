"""Exercise catalog, on-the-fly workout editing, routine templates, and race plans."""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import catalog  # noqa: E402
import race_plan  # noqa: E402


@pytest.fixture(autouse=True)
def temp_database(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app, "DATABASE", tmp_path / "test.db")
    monkeypatch.setattr(app, "LEGACY_FILE", tmp_path / "missing.json")
    app.initialize()


def test_catalog_search_ranks_logged_exercises_first():
    items = catalog.build_catalog({"rope pushdown": {"name": "Rope Pushdown", "times": 16, "last_date": "2026-11-19", "last_weight": 57.5, "last_reps": 10},
                                   "my odd lift": {"name": "My Odd Lift", "times": 1, "last_date": "2026-11-01", "last_weight": 20, "last_reps": 8}})
    results = catalog.search_catalog(items, "push")
    assert results[0]["name"] == "Rope Pushdown"
    assert any(item["name"] == "My Odd Lift" for item in catalog.search_catalog(items, "odd"))
    assert all(item["group"] == "Back" for item in catalog.search_catalog(items, "", "Back"))
    assert {item["name"] for item in catalog.search_catalog(items, "incline dumbbell")} == {"Incline Dumbbell Press", "Incline Dumbbell Curl"}


def test_add_swap_edit_and_remove_exercises_keep_logged_sets_aligned():
    session = app.create_session(app.SessionCreate(session_date=date.today().isoformat(), title="Quick push", workout=["Barbell Bench Press — 3 × 5"]))
    sid = session["id"]
    app.add_or_swap_exercise(sid, app.ExerciseInput(name="Pec Dec Fly", sets=3, reps="10-12"))
    app.add_or_swap_exercise(sid, app.ExerciseInput(name="Triceps Pressdown", sets=2, reps="12", weight=50))
    detail = app.session_detail(sid)
    assert [e["name"] for e in detail["exercises"]] == ["Barbell Bench Press", "Pec Dec Fly", "Rope Pushdown"]  # alias applied
    assert detail["exercises"][2]["suggested_weight"] == 50 and detail["exercises"][1]["target_reps"] == "10–12"

    app.save_set(sid, app.SetLog(exercise_index=0, set_number=1, weight=225, reps=5))
    app.save_set(sid, app.SetLog(exercise_index=2, set_number=1, weight=50, reps=12))
    with pytest.raises(HTTPException):  # can't swap an exercise that already has sets
        app.add_or_swap_exercise(sid, app.ExerciseInput(name="Dumbbell Bench Press", replace_index=0))
    app.add_or_swap_exercise(sid, app.ExerciseInput(name="Cable Fly", sets=2, reps="12", replace_index=1))
    app.update_exercise(sid, 2, app.ExerciseUpdate(sets=4))

    detail = app.remove_exercise(sid, 1)  # remove Cable Fly; Rope Pushdown's logged set must follow it to index 1
    assert [e["name"] for e in detail["exercises"]] == ["Barbell Bench Press", "Rope Pushdown"]
    assert detail["exercises"][1]["target_sets"] == 4
    assert [(s["weight"], s["reps"]) for s in detail["exercises"][1]["sets"]] == [(50, 12)]
    assert [(s["weight"], s["reps"]) for s in detail["exercises"][0]["sets"]] == [(225, 5)]


def test_routine_template_matches_log_and_deloads_to_two_sets():
    draft = app.build_draft(app.BlockDraftRequest(weeks=10, lifting_days=5))
    first_week = [s for s in draft["sessions"] if s["kind"] == "strength" and s["title"].startswith("W1 ·")]
    assert [s["title"] for s in first_week] == [
        "W1 · Chest, Triceps & Shoulders", "W1 · Back & Biceps", "W1 · Chest, Triceps & Shoulders · Volume", "W1 · Back & Biceps · Volume", "W1 · Legs"]
    deload = next(s for s in draft["sessions"] if s["kind"] == "strength" and s["description"].startswith("Deload"))
    assert all(" 2 × " in line or " 1 × " in line for line in deload["workout"])
    assert "Seated Row — 3 × 8–10" in next(s for s in first_week if s["title"].endswith("Back & Biceps"))["workout"]


def test_marathon_plan_progresses_safely_and_tapers():
    today = date(2026, 11, 20)
    plan = race_plan.generate_plan(date(2027, 10, 10), "marathon", 11.5, [1, 2, 5, 6], 6, goal_seconds=race_plan.parse_goal("3:45", "marathon"), today=today, race_name="Fall Marathon")
    weeks = plan["weeks"]
    assert [phase["name"] for phase in plan["phases"]] == ["Base", "Build", "Peak", "Taper", "Race week"]
    assert plan["summary"]["longest_run"] == 20 and 38 <= plan["summary"]["peak_miles"] <= 45
    full = [w["miles"] for w in weeks if not w["cutback"] and w["phase"] in {"Base", "Build", "Peak"}]
    assert all(b <= a * 1.15 + 1.5 for a, b in zip(full, full[1:])), "weekly mileage jumps too fast"
    assert weeks[-1]["runs"][-1]["type"] == "race" and weeks[-1]["runs"][-1]["date"] == "2027-10-10"
    assert weeks[-2]["miles"] < max(full)
    assert plan["summary"]["paces"]["race"] == "8:35"
    assert all(run["date"] >= today.isoformat() for week in weeks for run in week["runs"])
    with pytest.raises(ValueError):
        race_plan.generate_plan(today + timedelta(days=10), "half", 10, [2, 6], 6, today=today)


def test_applying_a_race_replaces_block_runs_but_not_logged_ones():
    today = date.today()
    soon = (today + timedelta(days=3)).isoformat()
    with app.connection() as db:
        db.execute("INSERT INTO sessions (id, session_date, kind, title, minutes, status, source) VALUES ('block-run', ?, 'run', 'W1 · Easy Run', 25, 'planned', 'plan:x')", (soon,))
        db.execute("INSERT INTO sessions (id, session_date, kind, title, minutes, status, source, locked) VALUES ('locked-run', ?, 'run', 'Locked', 25, 'planned', 'plan:x', 1)", (soon,))
    race = app.create_race(app.RaceRequest(name="Spring Half", race_date=(today + timedelta(days=120)).isoformat(), distance="half", goal_time="1:50", current_weekly_miles=12))
    result = app.apply_race(race["id"])
    assert result["runs_created"] > 20 and result["runs_replaced"] == 1
    with app.connection() as db:
        ids = {row[0] for row in db.execute("SELECT id FROM sessions")}
        assert "block-run" not in ids and "locked-run" in ids
        race_runs = db.execute("SELECT COUNT(*) FROM sessions WHERE source = ?", (f"race:{race['id']}",)).fetchone()[0]
    app.apply_race(race["id"])  # re-applying must not duplicate
    with app.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM sessions WHERE source = ?", (f"race:{race['id']}",)).fetchone()[0] == race_runs
    app.delete_race(race["id"])
    with app.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM sessions WHERE source = ?", (f"race:{race['id']}",)).fetchone()[0] == 0
