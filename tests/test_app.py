"""Dependency-light checks for Health Hub's core planning helpers."""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402


def test_prescription_details_extracts_sets_and_rep_range():
    sets, reps, guide = app.prescription_details("Barbell Bench Press — 4 × 5–8")
    assert sets == 4
    assert reps == "5–8"
    assert "RPE" in guide


def test_health_recommendation_prioritizes_recovery():
    message = app.health_recommendation({
        "sleep_hours": 5.0,
        "energy": 6,
        "soreness": 4,
        "stress": 4,
        "protein_goal_met": False,
    })
    assert "Recovery first" in message


def test_garmin_csv_parser_keeps_runs_and_converts_kilometers():
    parsed = app.parse_garmin_csv(
        "Activity Type,Date,Title,Distance,Time,Avg HR,Max HR\n"
        "Running,2026-01-10T08:00:00,Demo easy run,5 km,00:30:00,145,166\n"
        "Cycling,2026-01-11T08:00:00,Demo ride,20 km,01:00:00,130,150\n"
    )
    assert len(parsed) == 1
    assert parsed[0]["activity_type"] == "Run"
    assert round(parsed[0]["distance_miles"], 2) == 3.11
    assert parsed[0]["activity_date"] == "2026-01-10"


def test_normalize_activity_creates_stable_identifier():
    activity = {"activity_date": date.today().isoformat(), "activity_type": "Run", "title": "Easy run"}
    assert app.normalize_activity(activity, "demo")["id"] == app.normalize_activity(activity, "demo")["id"]


def test_deterministic_recommendation_preserves_a_good_training_day():
    result = app.deterministic_training_recommendation(
        [{"id": "today", "date": date.today().isoformat(), "title": "Back & Biceps", "kind": "strength", "minutes": 60, "status": "planned", "locked": False}],
        [{"calendar_date": date.today().isoformat(), "sleep_seconds": 7.5 * 3600, "hrv_ms": 60, "resting_hr": 52, "stress_avg": 30}],
        {"energy": 7, "soreness": 3},
    )
    assert result["mode"] == "train"
    assert result["target_session"]["id"] == "today"


def test_short_day_keeps_every_lift_at_two_sets():
    result = app.deterministic_training_recommendation(
        [{"id": "today", "date": date.today().isoformat(), "title": "Chest", "kind": "strength", "minutes": 60, "status": "planned", "locked": False, "workout": []}],
        [{"calendar_date": date.today().isoformat(), "sleep_seconds": 5.6 * 3600, "hrv_ms": 60, "resting_hr": 52, "stress_avg": 30}],
        None,
    )
    assert result["mode"] == "shorten"
    assert result["score"] == 70
    assert result["adjusted_sets"] == 2
    assert result["adjusted_minutes"] == 36
    assert result["headline"] == "You slept 5h 36m. Keep the lifts, cut each to 2 sets (about 36 min)."
    lines = app.workout_lines({"workout": ["Dumbbell Bench Press — 3 × 8–12", "Cable Fly — 1 × 15", "Easy effort — conversational pace"]}, cap_sets=2)
    assert [(line["name"], line["sets"], line["reps"]) for line in lines] == [("Dumbbell Bench Press", 2, "8–12"), ("Cable Fly", 1, "15"), ("Easy effort", None, None)]


def test_double_progression_suggestions():
    history = [{"day": "2026-11-10", "weight": 225, "reps": r, "deload": False} for r in (6, 6, 6)]
    assert app.progression_suggestion(history, "4–6")["weight"] == 230
    short = [{"day": "2026-11-10", "weight": 225, "reps": r, "deload": False} for r in (6, 5, 3)]
    assert app.progression_suggestion(short, "4–6")["weight"] == 225
    way_short = [{"day": "2026-11-10", "weight": 60, "reps": 5, "deload": False}]
    assert app.progression_suggestion(way_short, "8–10")["weight"] == 55
    # After a deload, progress from the last full session instead.
    with_deload = history + [{"day": "2026-11-17", "weight": 185, "reps": 6, "deload": True}]
    suggestion = app.progression_suggestion(with_deload, "4–6")
    assert suggestion["weight"] == 230 and suggestion["note"].startswith("Back from deload")
    assert app.progression_suggestion([{"day": "d", "weight": 15, "reps": 12, "deload": False}], "10–12")["change"] == 2.5


def test_readiness_uses_previous_night_until_today_syncs():
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    result = app.deterministic_training_recommendation(
        [{"id": "s", "date": date.today().isoformat(), "title": "Lift", "kind": "strength", "minutes": 60, "status": "planned", "locked": False}],
        [{"calendar_date": date.today().isoformat(), "sleep_seconds": None, "hrv_ms": None},
         {"calendar_date": yesterday, "sleep_seconds": 5.5 * 3600, "hrv_ms": 50, "hrv_status": "LOW", "body_battery_at_wake": 20}],
        None, recent_training_days=3,
    )
    assert result["data_is_stale"] and result["data_date"] == yesterday
    assert result["score"] == 100 - 30 - 15 - 12 - 5
    assert any("hasn't synced" in reason for reason in result["reasons"])


def test_build_phase_adds_sets_to_main_lifts():
    lines = ["Barbell Bench Press — 3 × 4–6", "Incline Dumbbell Press — 3 × 6–8", "Pec Dec Fly — 3 × 8–10"]
    assert app.phase_lines(lines, "Build") == ["Barbell Bench Press — 4 × 4–6", "Incline Dumbbell Press — 4 × 6–8", "Pec Dec Fly — 3 × 8–10"]
    assert app.phase_lines(lines, "Deload")[0] == "Barbell Bench Press — 2 × 4–6"


def test_exercise_aliases_use_logged_names():
    assert app.canonical_workout_line("Triceps Pressdown — 3 × 10–15") == "Rope Pushdown — 3 × 10–15"
    assert app.canonical_workout_line("Pull-up or Lat Pulldown — 4 × 6–10") == "Lat Pulldown — 4 × 6–10"
    assert app.canonical_workout_line("Barbell Bench Press — 4 × 5–8") == "Barbell Bench Press — 4 × 5–8"
    assert app.normalize_exercise("Cable Chest Fly") == app.normalize_exercise("cable  fly")
    draft = app.build_draft(app.BlockDraftRequest())
    lines = [line for session in draft["sessions"] for line in session["workout"]]
    assert not any(line.startswith(("Triceps Pressdown", "Overhead Press ", "Pull-up or Lat Pulldown")) for line in lines)


def test_deterministic_recommendation_prioritizes_recovery_when_signals_are_low():
    result = app.deterministic_training_recommendation(
        [{"id": "today", "date": date.today().isoformat(), "title": "Long Run", "kind": "run", "minutes": 70, "status": "planned", "locked": False}],
        [{"calendar_date": date.today().isoformat(), "sleep_seconds": 5 * 3600, "hrv_ms": 40, "resting_hr": 65, "stress_avg": 70}, {"calendar_date": "2026-01-01", "hrv_ms": 60, "resting_hr": 52}],
        {"energy": 3, "soreness": 8},
    )
    assert result["mode"] == "recovery"
    assert result["score"] <= 50
