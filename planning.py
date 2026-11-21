"""Training-block generation: routine templates, phases, and re-templating an active block."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from core import monday_on_or_after
from exercises import cap_sets, add_set, canonical_workout_line

class BlockDraftRequest(BaseModel):
    name: str = Field(default="My next training block", min_length=2, max_length=80)
    weeks: int = Field(default=10, ge=4, le=24)
    goals: list[str] = Field(default_factory=lambda: ["Build strength", "Build muscle", "Improve endurance"])
    lifting_days: int = Field(default=5, ge=2, le=6)
    run_days: int = Field(default=3, ge=0, le=5)
    starting_miles: float = Field(default=6, ge=0, le=50)
    available_minutes: int = Field(default=60, ge=20, le=180)
    equipment: str = "Full gym"
    avoid: str = "Bulgarian split squats"
    split: str = "logged-routine"
    include_mobility: bool = False


def phase_for(week: int, weeks: int) -> tuple[str, str]:
    if week == 1:
        return "Re-entry", "Rebuild rhythm and leave a few reps in reserve."
    if week in {max(2, weeks // 2), weeks}:
        return "Deload", "Reduce volume and preserve technique, not momentum."
    if week <= max(2, weeks // 2 - 1):
        return "Foundation", "Build consistent volume with controlled effort."
    return "Build", "Progress working sets when form and effort support it."


# Built from the lifting log (Jun–Nov 2026): chest/triceps/shoulders twice a week,
# back/biceps once or twice, legs about once, using the exercises actually logged.
ROUTINE_DAYS: dict[str, list[str]] = {
    "Chest, Triceps & Shoulders": ["Barbell Bench Press — 3 × 4–6", "Incline Dumbbell Press — 3 × 6–8", "Seated Dumbbell Shoulder Press — 2 × 8–10", "Pec Dec Fly — 3 × 8–10", "Rope Pushdown — 3 × 8–12"],
    "Back & Biceps": ["Lat Pulldown — 3 × 6–10", "Seated Row — 3 × 8–10", "Face Pull — 2 × 10–12", "Hammer Curl — 2 × 6–10", "Reverse Dumbbell Curl — 2 × 8–12"],
    "Chest, Triceps & Shoulders · Volume": ["Barbell Bench Press — 3 × 6–8", "Incline Dumbbell Press — 3 × 6–8", "Cable Fly — 2 × 10–12", "Low To High Cable Fly — 2 × 10–12", "Rope Pushdown — 2 × 8–10", "Tricep Extension — 2 × 8–12", "Cable Lateral Raise — 2 × 10–12"],
    "Back & Biceps · Volume": ["Lat Pulldown — 3 × 8–10", "Seated Row — 3 × 8–10", "Face Pull — 2 × 10–12", "Dumbbell Curl — 2 × 8–10", "Reverse Dumbbell Curl — 2 × 10–12"],
    "Legs": ["Pendulum Squat — 3 × 6–8", "Leg Extension — 3 × 8–10", "Lying Hamstring Curl — 3 × 8–10", "Hip Thrust — 3 × 8–12", "Cable Crunch — 2 × 12–15", "Russian Twist — 2 × 15"],
    "Legs · Mobility": ["Goblet Squat — 2 × 10", "Side Squat — 2 × 10", "Dumbbell Romanian Deadlift — 2 × 10"],
}


# Which days to use for each lifting frequency, in weekly order (Mon, Tue, Thu, Fri, Sun, Sat).
ROUTINE_BY_DAYS = {
    2: ["Chest, Triceps & Shoulders", "Back & Biceps"],
    3: ["Chest, Triceps & Shoulders", "Back & Biceps", "Legs"],
    4: ["Chest, Triceps & Shoulders", "Back & Biceps", "Chest, Triceps & Shoulders · Volume", "Legs"],
    5: ["Chest, Triceps & Shoulders", "Back & Biceps", "Chest, Triceps & Shoulders · Volume", "Back & Biceps · Volume", "Legs"],
    6: ["Chest, Triceps & Shoulders", "Back & Biceps", "Chest, Triceps & Shoulders · Volume", "Back & Biceps · Volume", "Legs", "Legs · Mobility"],
}


LIFT_DAY_OFFSETS = [0, 1, 3, 4, 6, 5]  # Mon, Tue, Thu, Fri, Sun, Sat


def logged_routine_workouts(days: int = 6) -> list[tuple[str, list[str]]]:
    return [(title, ROUTINE_DAYS[title]) for title in ROUTINE_BY_DAYS[max(2, min(6, days))]]


def phase_lines(lines: list[str], phase: str) -> list[str]:
    """Volume across the block: Deload caps every lift at 2 sets; Build adds a set to the
    first two (main) lifts; Re-entry and Foundation use the template as written."""
    if phase == "Deload":
        return [cap_sets(line, 2) for line in lines]
    if phase == "Build":
        return [add_set(line) if index < 2 else line for index, line in enumerate(lines)]
    return list(lines)


def strength_workouts(split: str, avoid: str) -> list[tuple[str, list[str]]]:
    lower_single_leg = "Leg Press — 3 × 10–12" if "bulgarian" in avoid.lower() else "Bulgarian Split Squat — 3 × 8–10"
    body_part = [
        ("Chest, Triceps & Shoulders", ["Barbell Bench Press — 4 × 5–8", "Incline Dumbbell Press — 3 × 8–12", "Cable Lateral Raise — 3 × 12–15", "Triceps Pressdown — 3 × 10–15", "Overhead Cable Extension — 2 × 10–15"]),
        ("Lower · Squat Focus", ["Back Squat — 4 × 5–8", "Romanian Deadlift — 3 × 6–10", lower_single_leg, "Leg Curl — 3 × 10–15", "Standing Calf Raise — 3 × 10–15"]),
        ("Back & Biceps", ["Pull-up or Lat Pulldown — 4 × 6–10", "Chest-Supported Row — 4 × 6–10", "Rear-Delt Fly — 3 × 12–15", "Cable Curl — 3 × 8–12", "Hammer Curl — 3 × 10–15"]),
        ("Chest, Triceps & Shoulders · Volume", ["Dumbbell Bench Press — 3 × 8–12", "Overhead Press — 3 × 6–10", "Cable Fly — 3 × 10–15", "Cable Lateral Raise — 3 × 12–15", "Triceps Pressdown — 3 × 10–15"]),
        ("Lower · Hinge Focus", ["Deadlift or Trap-Bar Deadlift — 3 × 3–6", "Front Squat or Leg Press — 3 × 8–12", "Hip Thrust — 3 × 8–12", "Leg Extension — 2 × 12–15", "Seated Calf Raise — 3 × 10–15"]),
        ("Back & Biceps · Volume", ["Lat Pulldown — 3 × 8–12", "Single-Arm Cable Row — 3 × 10–12", "Face Pull — 3 × 12–15", "Incline Dumbbell Curl — 3 × 10–12", "Hammer Curl — 3 × 10–15"]),
    ]
    if split == "logged-routine":
        return logged_routine_workouts()
    if split == "body-part-balanced":
        return body_part
    if split == "push-pull-legs":
        return [
            ("Push", ["Barbell Bench Press — 4 × 5–8", "Overhead Press — 3 × 6–10", "Cable Fly — 3 × 10–15", "Cable Lateral Raise — 3 × 12–15", "Triceps Pressdown — 3 × 10–15"]),
            ("Pull", ["Pull-up or Lat Pulldown — 4 × 6–10", "Chest-Supported Row — 4 × 6–10", "Rear-Delt Fly — 3 × 12–15", "Cable Curl — 3 × 8–12", "Hammer Curl — 3 × 10–15"]),
            ("Legs", ["Back Squat — 4 × 5–8", "Romanian Deadlift — 3 × 6–10", lower_single_leg, "Leg Curl — 3 × 10–15", "Standing Calf Raise — 3 × 10–15"]),
        ] * 2
    if split == "full-body":
        return [
            ("Full Body A", ["Back Squat — 3 × 5–8", "Barbell Bench Press — 3 × 5–8", "Chest-Supported Row — 3 × 8–12", "Cable Lateral Raise — 2 × 12–15"]),
            ("Full Body B", ["Romanian Deadlift — 3 × 6–10", "Overhead Press — 3 × 6–10", "Lat Pulldown — 3 × 8–12", "Cable Curl — 2 × 10–15"]),
            ("Full Body C", ["Leg Press — 3 × 10–12", "Dumbbell Bench Press — 3 × 8–12", "Single-Arm Cable Row — 3 × 10–12", "Triceps Pressdown — 2 × 10–15"]),
        ] * 2
    return [
        ("Upper A", ["Barbell Bench Press — 4 × 5–8", "Chest-Supported Row — 4 × 6–10", "Incline Dumbbell Press — 3 × 8–12", "Lat Pulldown — 3 × 8–12", "Cable Lateral Raise — 3 × 12–15"]),
        ("Lower A", ["Back Squat — 4 × 5–8", "Romanian Deadlift — 3 × 6–10", lower_single_leg, "Leg Curl — 3 × 10–15", "Standing Calf Raise — 3 × 10–15"]),
        ("Push", ["Overhead Press — 4 × 5–8", "Dumbbell Bench Press — 3 × 8–12", "Cable Fly — 3 × 10–15", "Cable Lateral Raise — 3 × 12–15", "Triceps Pressdown — 3 × 10–15"]),
        ("Pull", ["Pull-up or Lat Pulldown — 4 × 6–10", "Barbell or Chest-Supported Row — 4 × 6–10", "Rear-Delt Fly — 3 × 12–15", "Cable Curl — 3 × 8–12", "Hammer Curl — 3 × 10–15"]),
        ("Lower B", ["Deadlift or Trap-Bar Deadlift — 3 × 3–6", "Front Squat or Leg Press — 3 × 8–12", "Hip Thrust — 3 × 8–12", "Leg Extension — 2 × 12–15", "Seated Calf Raise — 3 × 10–15"]),
    ]


def build_draft(request: BlockDraftRequest) -> dict[str, Any]:
    start = monday_on_or_after(date.today())
    if request.split == "logged-routine":
        lifting = logged_routine_workouts(request.lifting_days)
    else:
        lifting = strength_workouts(request.split, request.avoid)[:request.lifting_days]
    week_template: list[tuple[int, str, str, int, str, list[str]]] = []
    lifting_days = LIFT_DAY_OFFSETS[:len(lifting)]
    for day_offset, (title, workout) in zip(lifting_days, lifting):
        week_template.append((day_offset, "strength", title, request.available_minutes, "Strength session", [canonical_workout_line(line) for line in workout]))
    if request.run_days:
        run_offsets = [2, 5, 6][:request.run_days]
        run_names = ["Easy Run", "Easy Run", "Long Run"]
        for index, day_offset in enumerate(run_offsets):
            fraction = 0.2 if index < 2 else 0.35
            miles = max(1.5, request.starting_miles * fraction)
            week_template.append((day_offset, "run", run_names[index], max(25, int(miles * 11)), f"Easy conversational effort · about {miles:.1f} mi in week 1", ["Easy effort — conversational pace", "Record distance, time, average HR, maximum HR, and effort"] ))
    if request.include_mobility:
        week_template.append((6, "mobility", "Mobility & recovery", 20, "Optional low-intensity mobility session", ["Mobility flow — 20 minutes", "Keep the effort restorative"] ))
    sessions: list[dict[str, Any]] = []
    phases: list[dict[str, Any]] = []
    for week in range(1, request.weeks + 1):
        phase, message = phase_for(week, request.weeks)
        phases.append({"week": week, "name": phase, "message": message})
        run_multiplier = 1 if phase == "Deload" else 1 + ((week - 1) * 0.06)
        for day_offset, kind, title, minutes, description, workout in week_template:
            scheduled = start + timedelta(days=(week - 1) * 7 + day_offset)
            session_title = f"W{week} · {title}"
            session_description = f"{phase}: {description}"
            if kind == "run":
                initial = request.starting_miles * (0.2 if title != "Long Run" else 0.35)
                target_miles = max(1.5, initial * run_multiplier)
                session_description = f"{phase}: easy effort · target {target_miles:.1f} mi"
                minutes = max(25, int(target_miles * 11))
            lines = phase_lines(workout, phase) if kind == "strength" else list(workout)
            sessions.append({"id": f"block-{uuid4().hex[:12]}", "date": scheduled.isoformat(), "kind": kind, "title": session_title, "minutes": minutes, "description": session_description, "workout": lines, "status": "planned", "locked": False})
    return {"id": f"plan-{uuid4().hex[:12]}", "name": request.name, "start_date": start.isoformat(), "goals": request.goals, "config": request.model_dump(), "phases": phases, "sessions": sessions}


def retemplate_future_strength(db: sqlite3.Connection, plan_id: str) -> int:
    """Switch an active block's future lift days to the logged routine, keeping its dates and phases.

    Only planned, unlocked sessions with no logged sets change; the weekday decides the workout.
    """
    row = db.execute("SELECT plan_json FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if not row:
        return 0
    plan = json.loads(row["plan_json"])
    days = int(plan.get("config", {}).get("lifting_days") or 5)
    titles = ROUTINE_BY_DAYS[max(2, min(6, days))]
    by_weekday = {offset: titles[index] for index, offset in enumerate(LIFT_DAY_OFFSETS[:len(titles)])}
    changed = 0
    for session in db.execute(
        """SELECT * FROM sessions s WHERE source = ? AND kind = 'strength' AND status = 'planned' AND locked = 0 AND session_date >= ?
           AND NOT EXISTS (SELECT 1 FROM set_logs sl WHERE sl.session_id = s.id)""",
        (f"plan:{plan_id}", date.today().isoformat()),
    ).fetchall():
        title = by_weekday.get(date.fromisoformat(session["session_date"]).weekday())
        if not title:
            continue
        week = re.match(r"^(W\d+)\s*·", session["title"] or "")
        phase = (session["description"] or "").split(":", 1)[0].strip()
        lines = phase_lines(ROUTINE_DAYS[title], phase)
        db.execute("UPDATE sessions SET title = ?, workout_json = ? WHERE id = ?",
                   (f"{week.group(1)} · {title}" if week else title, json.dumps(lines), session["id"]))
        changed += 1
    plan.setdefault("config", {})["split"] = "logged-routine"
    db.execute("UPDATE plans SET plan_json = ? WHERE id = ?", (json.dumps(plan), plan_id))
    return changed


def apply_exercise_aliases(db: sqlite3.Connection) -> int:
    """Rename aliased exercises in planned sessions (idempotent). Logged history keeps its original text."""
    changed = 0
    for row in db.execute("SELECT id, workout_json FROM sessions WHERE status = 'planned'").fetchall():
        lines = json.loads(row["workout_json"] or "[]")
        renamed = [canonical_workout_line(line) for line in lines]
        if renamed != lines:
            db.execute("UPDATE sessions SET workout_json = ? WHERE id = ?", (json.dumps(renamed), row["id"]))
            changed += 1
    return changed
