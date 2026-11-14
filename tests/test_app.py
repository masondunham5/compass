"""Dependency-light checks for Health Hub's core planning helpers."""

from __future__ import annotations

import sys
from datetime import date
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


def test_deterministic_recommendation_prioritizes_recovery_when_signals_are_low():
    result = app.deterministic_training_recommendation(
        [{"id": "today", "date": date.today().isoformat(), "title": "Long Run", "kind": "run", "minutes": 70, "status": "planned", "locked": False}],
        [{"calendar_date": date.today().isoformat(), "sleep_seconds": 5 * 3600, "hrv_ms": 40, "resting_hr": 65, "stress_avg": 70}, {"calendar_date": "2026-01-01", "hrv_ms": 60, "resting_hr": 52}],
        {"energy": 3, "soreness": 8},
    )
    assert result["mode"] == "recovery"
    assert result["score"] <= 50
