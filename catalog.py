"""Exercise catalog and run workout types.

The catalog is a static list of common gym exercises, merged at query time with
every exercise in the lifting log, so anything you have done is searchable and
ranked by how often you do it.
"""

from __future__ import annotations

import re
from typing import Any

# (name, muscle group, equipment, type) — type sets the default prescription.
EXERCISES: list[tuple[str, str, str, str]] = [
    # Chest
    ("Barbell Bench Press", "Chest", "Barbell", "compound"),
    ("Incline Barbell Press", "Chest", "Barbell", "compound"),
    ("Close Grip Bench Press", "Triceps", "Barbell", "compound"),
    ("Dumbbell Bench Press", "Chest", "Dumbbell", "compound"),
    ("Incline Dumbbell Press", "Chest", "Dumbbell", "compound"),
    ("Dumbbell Floor Press", "Chest", "Dumbbell", "compound"),
    ("Smith Incline Press", "Chest", "Smith machine", "compound"),
    ("Smith Close Grip Bench Press", "Triceps", "Smith machine", "compound"),
    ("Machine Chest Press", "Chest", "Machine", "compound"),
    ("Weighted Dips", "Chest", "Bodyweight", "compound"),
    ("Push-up", "Chest", "Bodyweight", "compound"),
    ("Pec Dec Fly", "Chest", "Machine", "isolation"),
    ("Cable Fly", "Chest", "Cable", "isolation"),
    ("Low To High Cable Fly", "Chest", "Cable", "isolation"),
    ("Dumbbell Fly", "Chest", "Dumbbell", "isolation"),
    # Shoulders
    ("Barbell Overhead Press", "Shoulders", "Barbell", "compound"),
    ("Standing Push Press", "Shoulders", "Barbell", "compound"),
    ("Seated Dumbbell Shoulder Press", "Shoulders", "Dumbbell", "compound"),
    ("Standing Dumbbell Shoulder Press", "Shoulders", "Dumbbell", "compound"),
    ("Smith Seated Overhead Press", "Shoulders", "Smith machine", "compound"),
    ("Machine Shoulder Press", "Shoulders", "Machine", "compound"),
    ("Arnold Press", "Shoulders", "Dumbbell", "compound"),
    ("Cable Lateral Raise", "Shoulders", "Cable", "isolation"),
    ("Dumbbell Lateral Raise", "Shoulders", "Dumbbell", "isolation"),
    ("Machine Lateral Raise", "Shoulders", "Machine", "isolation"),
    ("Rear-Delt Fly", "Shoulders", "Dumbbell", "isolation"),
    ("Reverse Pec Dec", "Shoulders", "Machine", "isolation"),
    ("Face Pull", "Shoulders", "Cable", "isolation"),
    ("Front Raise", "Shoulders", "Dumbbell", "isolation"),
    ("Upright Row", "Shoulders", "Cable", "isolation"),
    ("Shrug", "Back", "Dumbbell", "isolation"),
    # Triceps
    ("Rope Pushdown", "Triceps", "Cable", "isolation"),
    ("V-Grip Pushdown", "Triceps", "Cable", "isolation"),
    ("Straight Bar Pushdown", "Triceps", "Cable", "isolation"),
    ("Overhead Cable Tricep Extension", "Triceps", "Cable", "isolation"),
    ("Tricep Extension", "Triceps", "Cable", "isolation"),
    ("EZ Bar Skull Crusher", "Triceps", "EZ bar", "isolation"),
    ("Dumbbell Skull Crusher", "Triceps", "Dumbbell", "isolation"),
    ("Single-Arm Cable Extension", "Triceps", "Cable", "isolation"),
    # Back
    ("Lat Pulldown", "Back", "Cable", "compound"),
    ("Close Grip Lat Pulldown", "Back", "Cable", "compound"),
    ("Pull-up", "Back", "Bodyweight", "compound"),
    ("Chin-up", "Back", "Bodyweight", "compound"),
    ("Assisted Pull-up", "Back", "Machine", "compound"),
    ("Seated Row", "Back", "Cable", "compound"),
    ("Chest-Supported Row", "Back", "Machine", "compound"),
    ("Barbell Row", "Back", "Barbell", "compound"),
    ("Dumbbell Single Arm Row", "Back", "Dumbbell", "compound"),
    ("T-Bar Row", "Back", "Barbell", "compound"),
    ("Machine Row", "Back", "Machine", "compound"),
    ("Straight-Arm Pulldown", "Back", "Cable", "isolation"),
    ("Single-Arm Cable Row", "Back", "Cable", "compound"),
    ("Back Extension", "Back", "Bodyweight", "isolation"),
    # Biceps
    ("Dumbbell Curl", "Biceps", "Dumbbell", "isolation"),
    ("Hammer Curl", "Biceps", "Dumbbell", "isolation"),
    ("Reverse Dumbbell Curl", "Biceps", "Dumbbell", "isolation"),
    ("Incline Dumbbell Curl", "Biceps", "Dumbbell", "isolation"),
    ("Curl Bar Curl", "Biceps", "EZ bar", "isolation"),
    ("Barbell Curl", "Biceps", "Barbell", "isolation"),
    ("Cable Curl", "Biceps", "Cable", "isolation"),
    ("Preacher Curl", "Biceps", "Machine", "isolation"),
    ("Concentration Curl", "Biceps", "Dumbbell", "isolation"),
    # Legs
    ("Back Squat", "Legs", "Barbell", "compound"),
    ("Front Squat", "Legs", "Barbell", "compound"),
    ("Pendulum Squat", "Legs", "Machine", "compound"),
    ("Hack Squat", "Legs", "Machine", "compound"),
    ("Leg Press", "Legs", "Machine", "compound"),
    ("Goblet Squat", "Legs", "Dumbbell", "compound"),
    ("Side Squat", "Legs", "Dumbbell", "compound"),
    ("Walking Lunge", "Legs", "Dumbbell", "compound"),
    ("Reverse Lunge", "Legs", "Dumbbell", "compound"),
    ("Step-up", "Legs", "Dumbbell", "compound"),
    ("Leg Extension", "Legs", "Machine", "isolation"),
    ("Lying Hamstring Curl", "Hamstrings", "Machine", "isolation"),
    ("Seated Hamstring Curl", "Hamstrings", "Machine", "isolation"),
    ("Romanian Deadlift", "Hamstrings", "Barbell", "compound"),
    ("Dumbbell Romanian Deadlift", "Hamstrings", "Dumbbell", "compound"),
    ("Deadlift", "Hamstrings", "Barbell", "compound"),
    ("Trap-Bar Deadlift", "Hamstrings", "Trap bar", "compound"),
    ("Hip Thrust", "Glutes", "Barbell", "compound"),
    ("Glute Bridge", "Glutes", "Bodyweight", "isolation"),
    ("Hip Abduction", "Glutes", "Machine", "isolation"),
    ("Standing Calf Raise", "Calves", "Machine", "isolation"),
    ("Seated Calf Raise", "Calves", "Machine", "isolation"),
    # Core
    ("Cable Crunch", "Core", "Cable", "core"),
    ("Russian Twist", "Core", "Dumbbell", "core"),
    ("Hanging Leg Raise", "Core", "Bodyweight", "core"),
    ("Plank", "Core", "Bodyweight", "core"),
    ("Ab Wheel Rollout", "Core", "Bodyweight", "core"),
    ("Pallof Press", "Core", "Cable", "core"),
    ("Dead Bug", "Core", "Bodyweight", "core"),
]

DEFAULT_PRESCRIPTION = {"compound": (3, "6–10"), "isolation": (3, "10–15"), "core": (2, "12–15")}

GROUP_HINTS = {
    "Chest": ("bench", "press", "fly", "dip", "push-up", "chest"),
    "Shoulders": ("shoulder", "lateral", "overhead", "military", "delt", "face pull", "arnold"),
    "Triceps": ("tricep", "pushdown", "skull", "extension"),
    "Back": ("row", "pulldown", "pull-up", "pull up", "chin", "lat "),
    "Biceps": ("curl",),
    "Legs": ("squat", "lunge", "leg press", "leg extension", "step-up"),
    "Hamstrings": ("deadlift", "hamstring", "rdl"),
    "Glutes": ("hip thrust", "glute", "abduction"),
    "Core": ("crunch", "twist", "plank", "leg raise", "ab "),
}


def _key(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def guess_group(name: str) -> str:
    lowered = f"{name.lower()} "
    for group, hints in GROUP_HINTS.items():
        if any(hint in lowered for hint in hints):
            return group
    return "Other"


def build_catalog(history: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge the static list with logged exercises. `history` maps lowercase name -> usage stats."""
    items: dict[str, dict[str, Any]] = {}
    for name, group, equipment, kind in EXERCISES:
        sets, reps = DEFAULT_PRESCRIPTION[kind]
        items[_key(name)] = {"name": name, "group": group, "equipment": equipment, "default_sets": sets, "default_reps": reps,
                             "times": 0, "last_date": None, "last_weight": None, "last_reps": None}
    for key, stats in history.items():
        entry = items.setdefault(key, {"name": stats["name"], "group": guess_group(stats["name"]), "equipment": "", "default_sets": 3, "default_reps": "8–12"})
        entry.update({"times": stats["times"], "last_date": stats["last_date"], "last_weight": stats["last_weight"], "last_reps": stats["last_reps"]})
    return list(items.values())


def search_catalog(catalog: list[dict[str, Any]], query: str = "", group: str = "", limit: int = 40) -> list[dict[str, Any]]:
    """Every query word must appear in the name, group, or equipment; logged lifts rank first."""
    words = [word for word in _key(query).split(" ") if word]
    results = []
    for item in catalog:
        if group and item["group"] != group:
            continue
        haystack = f"{item['name']} {item['group']} {item['equipment']}".lower()
        if not all(word in haystack for word in words):
            continue
        name = item["name"].lower()
        score = (min(item.get("times", 0), 30) * 10
                 + (15 if words and name.startswith(words[0]) else 0)
                 + (8 if words and any(part.startswith(words[0]) for part in name.split()) else 0))
        results.append((-score, item["name"], item))
    results.sort(key=lambda entry: (entry[0], entry[1]))
    return [item for _, _, item in results[:limit]]


# ---------------------------------------------------------------- running

# Run workout types. `pace` is a multiplier on goal/estimated race pace per mile
# (1.0 = race pace); `structure` describes the session in plain language.
RUN_TYPES: dict[str, dict[str, Any]] = {
    "easy": {"label": "Easy run", "pace": "easy", "structure": ["Conversational effort the whole way", "Finish feeling like you could keep going"]},
    "recovery": {"label": "Recovery run", "pace": "recovery", "structure": ["Very easy — slower than easy pace", "Keep heart rate low; walk breaks are fine"]},
    "long": {"label": "Long run", "pace": "long", "structure": ["Steady, easy effort", "Practice fueling every 45 minutes on runs over 90 minutes"]},
    "strides": {"label": "Easy + strides", "pace": "easy", "structure": ["Easy run", "Finish with 6 × 20-second strides, full recovery between"]},
    "tempo": {"label": "Tempo run", "pace": "tempo", "structure": ["1 mi easy warm-up", "Middle miles at tempo — comfortably hard", "1 mi easy cool-down"]},
    "intervals": {"label": "Intervals", "pace": "interval", "structure": ["1 mi easy warm-up", "6 × 800 m at interval pace, 400 m jog between", "1 mi easy cool-down"]},
    "hills": {"label": "Hill repeats", "pace": "easy", "structure": ["1 mi easy warm-up", "8 × 60 s uphill at hard effort, jog down to recover", "Easy cool-down"]},
    "progression": {"label": "Progression run", "pace": "long", "structure": ["Start easy", "Each third a little faster", "Last third near tempo effort"]},
    "marathon_pace": {"label": "Marathon-pace run", "pace": "race", "structure": ["2 mi easy warm-up", "Middle miles at goal marathon pace", "1 mi easy cool-down"]},
    "race": {"label": "Race", "pace": "race", "structure": ["Race day — trust the training", "Start conservatively; even splits"]},
}

PACE_FACTORS = {"recovery": 1.30, "easy": 1.22, "long": 1.15, "race": 1.0, "tempo": 0.94, "interval": 0.88}
