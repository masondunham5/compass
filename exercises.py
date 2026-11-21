"""Exercise names, workout-line parsing, rest times, 1RM estimates, and double-progression suggestions."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

def cap_sets(line: str, cap: int) -> str:
    return re.sub(r"(\d+)(\s*[×x]\s*)", lambda m: f"{min(int(m.group(1)), cap)}{m.group(2)}", line, count=1)


def add_set(line: str, maximum: int = 4) -> str:
    return re.sub(r"(\d+)(\s*[×x]\s*)", lambda m: f"{min(int(m.group(1)) + 1, max(maximum, int(m.group(1))))}{m.group(2)}", line, count=1)


# Different names for the same exercise, mapped to the name used in the lifting log.
# Progress, last-weight suggestions, and planned workouts all use the right-hand name.
EXERCISE_ALIASES = {
    "triceps pressdown": "Rope Pushdown",
    "overhead cable extension": "Overhead Cable Tricep Extension",
    "overhead press": "Barbell Overhead Press",
    "pull-up or lat pulldown": "Lat Pulldown",
    "cable chest fly": "Cable Fly",
}


def canonical_exercise(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", name.strip())
    return EXERCISE_ALIASES.get(cleaned.lower(), cleaned)


def canonical_workout_line(line: str) -> str:
    name, separator, prescription = line.partition("—")
    canonical = canonical_exercise(name)
    return f"{canonical} —{prescription}" if separator else canonical


def normalize_exercise(name: str) -> str:
    return canonical_exercise(name).lower()


def prescription_details(prescription: str) -> tuple[int, str, str]:
    """Extract target sets/reps and a transparent no-baseline starting rule."""
    match = re.search(r"(\d+)\s*[×x]\s*(\d+)(?:\s*[–-]\s*(\d+))?", prescription)
    if not match:
        return 3, "RPE 7–8", "No baseline yet: choose a load that leaves about 2–3 reps in reserve."
    sets, low, high = int(match.group(1)), int(match.group(2)), int(match.group(3) or match.group(2))
    reps = f"{low}–{high}" if high != low else str(low)
    if high <= 6:
        guide = "~75–85% 1RM · RPE 7–8"
    elif high <= 10:
        guide = "~65–78% 1RM · RPE 7–8"
    else:
        guide = "~55–70% 1RM · RPE 7–8"
    return sets, reps, f"No baseline yet: use {guide}; stop with 2–3 reps in reserve."


def line_target_weight(prescription: str) -> float | None:
    match = re.search(r"@\s*(\d+(?:\.\d+)?)\s*lb", prescription or "")
    return float(match.group(1)) if match else None


def format_workout_line(name: str, sets: int, reps: str, weight: float | None = None) -> str:
    reps = re.sub(r"\s+", "", str(reps or "8–12")).replace("-", "–")
    line = f"{canonical_exercise(name)} — {int(sets)} × {reps}"
    return f"{line} @ {weight:g} lb" if weight else line


def exercise_name_from_session(session: dict[str, Any], exercise_index: int) -> str | None:
    workout = session.get("workout", [])
    if exercise_index >= len(workout):
        return None
    return workout[exercise_index].partition("—")[0].strip()


# Lifts where the logged weight is *added* to bodyweight (0 = bodyweight only).
BODYWEIGHT_EXERCISES = {"pull-up", "chin-up", "weighted dips", "dips", "push-up"}


def is_bodyweight(name: str) -> bool:
    return canonical_exercise(name).lower() in BODYWEIGHT_EXERCISES


def estimate_1rm(name: str, weight: float, reps: int, bodyweight: float | None = None) -> float:
    """Epley estimate; bodyweight lifts count bodyweight + added load when bodyweight is known."""
    load = weight + (bodyweight or 0) if is_bodyweight(name) else weight
    return round(load * (1 + reps / 30), 1) if reps and load else 0.0


def rest_seconds(name: str, target_reps: str) -> int:
    """Rest between sets: heavy compounds 3 min, other compounds 2.5 min, isolation 90 s, core 60 s."""
    from catalog import EXERCISES
    kinds = {exercise.lower(): kind for exercise, _, _, kind in EXERCISES}
    kind = kinds.get(canonical_exercise(name).lower())
    if kind == "core":
        return 60
    if kind == "isolation":
        return 90
    if kind == "compound":
        return 180 if rep_bounds(target_reps)[1] <= 6 else 150
    return 120


def rep_bounds(target_reps: str) -> tuple[int, int]:
    numbers = [int(n) for n in re.findall(r"\d+", target_reps or "")]
    return (numbers[0], numbers[-1]) if numbers else (8, 12)


def weight_step(weight: float) -> float:
    return 5.0 if weight >= 50 else 2.5


def progression_suggestion(history: list[dict[str, Any]], target_reps: str) -> dict[str, Any] | None:
    """Double progression from the most recent full (non-deload) session of this exercise.

    Work at the heaviest weight used last time. If every set there reached the top of the
    rep range, add a small step; if any fell below the bottom, hold (or drop a step if
    well short); otherwise keep the weight and aim for more reps.
    """
    if not history:
        return None
    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in history:
        by_day.setdefault(row["day"], []).append(row)
    days = sorted(by_day, reverse=True)
    latest = days[0]
    is_deload = lambda day: any(r.get("deload") for r in by_day[day])
    source_day = next((day for day in days if not is_deload(day)), latest)
    sets = by_day[source_day]
    top = max(row["weight"] for row in sets)
    reps_at_top = [row["reps"] for row in sets if row["weight"] == top]
    low, high = rep_bounds(target_reps)
    last = f"{top:g} × {', '.join(str(r) for r in reps_at_top)}"
    prefix = "Back from deload. " if is_deload(latest) and source_day != latest else ""
    if top <= 0:  # bodyweight-only: progress reps
        return {"weight": 0, "reps": min(high, max(reps_at_top) + 1), "change": 0, "note": f"{prefix}Bodyweight last time ({', '.join(map(str, reps_at_top))} reps). Add a rep or some weight."}
    step = weight_step(top)
    if all(r >= high for r in reps_at_top):
        return {"weight": top + step, "reps": low, "change": step, "note": f"{prefix}Hit {high}+ reps on every set at {top:g} lb ({last}). Go up to {top + step:g} lb."}
    if min(reps_at_top) <= low - 3:
        return {"weight": max(step, top - step), "reps": low, "change": -step, "note": f"{prefix}Well short of {low} reps at {top:g} lb ({last}). Drop to {max(step, top - step):g} lb and rebuild."}
    if min(reps_at_top) < low:
        return {"weight": top, "reps": low, "change": 0, "note": f"{prefix}Below {low} reps at {top:g} lb ({last}). Stay here and get every set to {low}."}
    return {"weight": top, "reps": min(high, max(reps_at_top) + 1), "change": 0, "note": f"{prefix}Stay at {top:g} lb ({last}). Aim for {high} reps on every set, then add weight."}


def exercises_for(session: dict[str, Any], rows: list[sqlite3.Row], previous_rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Turn stable workout outline lines into a usable in-gym set logger."""
    by_exercise: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_exercise.setdefault(row["exercise_index"], []).append(dict(row))
    exercises = []
    for index, outline in enumerate(session.get("workout", [])):
        name, _, prescription = outline.partition("—")
        logged = sorted(by_exercise.get(index, []), key=lambda item: item["set_number"])
        same_exercise = [dict(row) for row in previous_rows if normalize_exercise(row["exercise_name"]) == normalize_exercise(name)]
        target_sets, target_reps, starting_guide = prescription_details(prescription.strip() or "Working sets")
        target_weight = line_target_weight(prescription)
        history = [{"day": row.get("session_date") or row["completed_at"][:10], "weight": row["weight"], "reps": row["reps"],
                    "deload": "deload" in (row.get("note") or "").lower() or (row.get("session_description") or "").startswith("Deload")}
                   for row in same_exercise]
        suggestion = progression_suggestion(history, target_reps)
        if suggestion and suggestion["weight"] and (session.get("description") or "").startswith("Deload"):
            # Deload week: about 85% of the working weight, rounded to the plate step, for easy, crisp reps.
            step = weight_step(suggestion["weight"])
            lighter = max(step, round(suggestion["weight"] * 0.85 / step) * step)
            suggestion = {**suggestion, "weight": lighter, "change": lighter - suggestion["weight"],
                          "note": f"Deload week: ~85% of {suggestion['weight']:g} lb. Keep every rep crisp and leave plenty in reserve."}
        exercises.append({
            "rest_seconds": rest_seconds(name, target_reps),
            "index": index, "name": name.strip(), "prescription": prescription.strip() or "Working sets",
            "sets": logged, "target_sets": target_sets, "target_reps": target_reps, "target_weight": target_weight,
            "suggested_weight": target_weight if target_weight is not None else (suggestion["weight"] if suggestion else None),
            "suggested_reps": suggestion["reps"] if suggestion else None,
            "progression": suggestion,
            "starting_guide": suggestion["note"] if suggestion else starting_guide,
        })
    return exercises


def workout_lines(session: dict[str, Any], cap_sets: int | None = None) -> list[dict[str, Any]]:
    """Split 'Exercise — 3 × 8–12' lines into display rows, optionally capping sets."""
    rows = []
    for line in session.get("workout", []):
        name, _, prescription = line.partition("—")
        match = re.search(r"(\d+)\s*[×x]\s*(\d+(?:\s*[–-]\s*\d+)?)", prescription)
        sets = int(match.group(1)) if match else None
        reps = re.sub(r"\s+", "", match.group(2)).replace("-", "–") if match else None
        shown_sets = min(sets, cap_sets) if sets and cap_sets else sets
        rows.append({"name": name.strip(), "sets": shown_sets, "planned_sets": sets, "reps": reps, "note": None if match else prescription.strip() or None})
    return rows
