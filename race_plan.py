"""Race training plan generator (5K to marathon).

Pure functions: given a race date, distance, current weekly mileage, and the
days you can run, build a week-by-week plan that works back from race day:

  base   — easy mileage, slow growth, strides (only when the race is far away)
  build  — mileage climbs toward peak, one tempo run a week
  peak   — highest mileage, longest long runs, race-specific workouts
  taper  — volume drops so you arrive fresh; race week ends with the race

Mileage never jumps more than ~12% week to week, and every 4th week is a
cutback (~80%). Paces come from the goal time (Riegel-equivalent marathon
pace) or, without a goal, from your recent easy pace.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from catalog import PACE_FACTORS, RUN_TYPES

MARATHON_MILES = 26.219
DISTANCES = {"5k": 3.107, "10k": 6.214, "half": 13.109, "marathon": MARATHON_MILES}
DISTANCE_LABELS = {"5k": "5K", "10k": "10K", "half": "Half marathon", "marathon": "Marathon"}
PROFILES = {
    "marathon": {"build_weeks": 18, "taper": [0.75, 0.55, 0.3], "peak": 40, "long_cap": 20, "specific": "marathon_pace"},
    "half": {"build_weeks": 12, "taper": [0.7, 0.4], "peak": 30, "long_cap": 13, "specific": "tempo"},
    "10k": {"build_weeks": 8, "taper": [0.45], "peak": 25, "long_cap": 10, "specific": "intervals"},
    "5k": {"build_weeks": 8, "taper": [0.45], "peak": 20, "long_cap": 8, "specific": "intervals"},
}
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def parse_goal(text: str | None, distance: str = "marathon") -> int | None:
    """'3:45:00' -> seconds. Two parts mean H:MM for a half/marathon and MM:SS for a 5K/10K."""
    if not text or not str(text).strip():
        return None
    try:
        parts = [int(part) for part in str(text).strip().split(":")]
    except ValueError as error:
        raise ValueError("Use H:MM:SS (or MM:SS for a 5K/10K) for the goal time.") from error
    if len(parts) == 3:
        seconds = parts[0] * 3600 + parts[1] * 60 + parts[2]
    elif len(parts) == 2:
        seconds = parts[0] * 3600 + parts[1] * 60 if distance in {"half", "marathon"} else parts[0] * 60 + parts[1]
    else:
        raise ValueError("Use H:MM:SS (or MM:SS for a 5K/10K) for the goal time.")
    if seconds <= 0:
        raise ValueError("The goal time must be greater than zero.")
    return seconds


def format_pace(seconds: float | None) -> str:
    if not seconds:
        return ""
    seconds = round(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def format_duration(seconds: float | None) -> str:
    if not seconds:
        return ""
    seconds = round(seconds)
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def marathon_pace(goal_seconds: int | None, distance_miles: float, easy_pace: float | None) -> float | None:
    """Seconds per mile at marathon effort, from the goal (Riegel) or from easy pace."""
    if goal_seconds:
        marathon_time = goal_seconds * (MARATHON_MILES / distance_miles) ** 1.06
        return marathon_time / MARATHON_MILES
    if easy_pace:
        return easy_pace / PACE_FACTORS["easy"]
    return None


def _round_half(value: float) -> float:
    return max(0.0, round(value * 2) / 2)


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def generate_plan(
    race_date: date,
    distance: str,
    current_weekly_miles: float,
    run_days: list[int],
    long_run_day: int,
    goal_seconds: int | None = None,
    peak_miles: float | None = None,
    easy_pace: float | None = None,
    today: date | None = None,
    race_name: str = "Race",
) -> dict[str, Any]:
    today = today or date.today()
    if distance not in DISTANCES:
        raise ValueError("Distance must be 5k, 10k, half, or marathon.")
    run_days = sorted({int(day) % 7 for day in run_days})
    if long_run_day not in run_days:
        run_days = sorted({*run_days, long_run_day})
    if len(run_days) < 2:
        raise ValueError("Pick at least two run days.")
    profile = PROFILES[distance]
    race_miles = DISTANCES[distance]
    start = _monday(today) if today.weekday() == 0 else _monday(today) + timedelta(days=7)
    race_week_start = _monday(race_date)
    total_weeks = (race_week_start - start).days // 7 + 1
    if total_weeks < 3:
        raise ValueError("The race is less than three weeks out — too soon to build a plan.")

    taper_factors = profile["taper"][: max(1, min(len(profile["taper"]), total_weeks - 2))]
    taper_weeks = len(taper_factors)
    build_weeks = min(profile["build_weeks"], total_weeks - taper_weeks)
    base_weeks = total_weeks - taper_weeks - build_weeks
    peak_split = build_weeks - max(2, round(build_weeks * 0.4))  # last ~40% of the build is "peak"

    start_miles = max(5.0, float(current_weekly_miles or 0))
    peak = float(peak_miles or max(profile["peak"], start_miles * 1.15))
    base_end = min(peak * 0.7, start_miles + 1.0 * base_weeks) if base_weeks else start_miles
    base_end = max(base_end, start_miles)

    mp = marathon_pace(goal_seconds, race_miles, easy_pace)
    weeks: list[dict[str, Any]] = []
    previous_full = start_miles
    for index in range(total_weeks):
        week_start = start + timedelta(days=index * 7)
        if index < base_weeks:
            phase = "Base"
            linear = start_miles + (base_end - start_miles) * ((index + 1) / base_weeks)
        elif index < base_weeks + build_weeks:
            phase = "Build" if index - base_weeks < peak_split else "Peak"
            step = index - base_weeks + 1
            linear = base_end + (peak - base_end) * (step / build_weeks)
        else:
            phase = "Taper" if index < total_weeks - 1 else "Race week"
            linear = peak * taper_factors[index - base_weeks - build_weeks]
        cutback = phase in {"Base", "Build", "Peak"} and (index + 1) % 4 == 0 and index < total_weeks - taper_weeks - 1
        if phase in {"Base", "Build", "Peak"}:
            target = min(linear, previous_full * 1.12 + 1)
            if not cutback:
                previous_full = target
            if cutback:
                target *= 0.8
        else:
            target = linear
        weeks.append({"index": index + 1, "start": week_start.isoformat(), "phase": phase, "cutback": cutback, "miles": _round_half(target)})

    # Long run: grows to the cap over the build, longest ~3 weeks before the race.
    long_cap = profile["long_cap"]
    build_start_long = None
    for week in weeks:
        miles = week["miles"]
        if week["phase"] == "Base":
            long = min(miles * 0.35, long_cap * 0.6)
        elif week["phase"] in {"Build", "Peak"}:
            # Ramp the long run on its own schedule so it reaches the cap in the last peak weeks.
            if build_start_long is None:
                build_start_long = max(4.0, min(weeks[max(0, week["index"] - 2)]["miles"] * 0.35, long_cap * 0.6))
            step = week["index"] - base_weeks
            ramp = build_start_long + (long_cap - build_start_long) * min(1.0, step / max(1, build_weeks - 1))
            long = min(ramp, long_cap, miles * 0.5)
            if week["cutback"]:
                long *= 0.8
        elif week["phase"] == "Taper":
            long = min(miles * 0.4, long_cap * 0.65)
        else:
            long = 0
        week["long"] = _round_half(max(long, 3 if week["phase"] != "Race week" else 0))

    plan_weeks = []
    for week in weeks:
        runs = _week_runs(week, run_days, long_run_day, distance, profile, race_date, race_miles, mp, race_name)
        week["runs"] = [run for run in runs if run["date"] >= today.isoformat()]
        week["miles"] = _round_half(sum(run["miles"] for run in runs))
        plan_weeks.append(week)

    phases = []
    for week in plan_weeks:
        if not phases or phases[-1]["name"] != week["phase"]:
            phases.append({"name": week["phase"], "start": week["start"], "weeks": 0})
        phases[-1]["weeks"] += 1
    return {
        "distance": distance, "distance_label": DISTANCE_LABELS[distance], "race_miles": race_miles,
        "race_date": race_date.isoformat(), "weeks": plan_weeks, "phases": phases,
        "summary": {
            "total_weeks": total_weeks, "start_miles": _round_half(start_miles), "peak_miles": max(w["miles"] for w in plan_weeks),
            "longest_run": max(w["long"] for w in plan_weeks), "run_days": [WEEKDAYS[d] for d in run_days], "long_run_day": WEEKDAYS[long_run_day],
            "paces": {name: format_pace(mp * factor) for name, factor in PACE_FACTORS.items()} if mp else None,
            "goal": format_duration(goal_seconds) if goal_seconds else None,
        },
    }


def _week_runs(week, run_days, long_day, distance, profile, race_date, race_miles, mp, race_name) -> list[dict[str, Any]]:
    week_start = date.fromisoformat(week["start"])
    phase = week["phase"]
    days = [week_start + timedelta(days=d) for d in run_days]
    runs: list[dict[str, Any]] = []

    if phase == "Race week":
        race_offset = (race_date - week_start).days
        easy_days = [d for d in run_days if d < race_offset][-2:]  # two easy shakeouts before the race
        for d in easy_days:
            runs.append(_run(week_start + timedelta(days=d), "easy" if d != easy_days[-1] else "strides", 3 if d != easy_days[-1] else 2, phase, mp))
        runs.append(_run(race_date, "race", round(race_miles, 1), phase, mp, title=f"Race: {race_name}"))
        return runs

    long_date = week_start + timedelta(days=long_day)
    others = [d for d in days if d != long_date]
    # Quality day: the first run day at least two days away from the long run.
    quality_date = next((d for d in others if abs((d - long_date).days) >= 2), others[0] if others else None)
    if week["cutback"] or phase == "Base":
        quality_type = "strides" if week["index"] > 2 else "easy"
    elif phase == "Build":
        quality_type = "tempo"
    elif phase == "Peak":
        quality_type = profile["specific"] if week["index"] % 2 == 0 else "tempo"
    else:  # Taper: keep a little intensity, cut the volume
        quality_type = profile["specific"] if profile["specific"] != "intervals" else "tempo"

    long_type = "long"
    if phase == "Peak" and distance == "marathon" and not week["cutback"] and week["index"] % 2 == 1:
        long_type = "progression"
    remaining = max(0.0, week["miles"] - week["long"])
    count = len(others)
    each = remaining / count if count else 0
    quality_miles = _round_half(max(each, 3)) if quality_type not in {"easy", "strides"} else _round_half(each)
    easy_share = (remaining - quality_miles) / max(1, count - 1) if count > 1 else 0

    runs.append(_run(long_date, long_type, week["long"], phase, mp))
    for d in others:
        if d == quality_date:
            runs.append(_run(d, quality_type, max(2.0, quality_miles), phase, mp))
        else:
            runs.append(_run(d, "recovery" if phase == "Taper" else "easy", max(2.0, _round_half(easy_share)), phase, mp))
    return sorted(runs, key=lambda run: run["date"])


def _run(day: date, run_type: str, miles: float, phase: str, mp: float | None, title: str | None = None) -> dict[str, Any]:
    spec = RUN_TYPES[run_type]
    pace = mp * PACE_FACTORS[spec["pace"]] if mp else None
    average = mp * PACE_FACTORS["easy"] if mp else 630  # default ~10:30/mi when no pace is known
    minutes = round(miles * (pace if run_type in {"race", "long", "recovery", "easy", "strides"} and pace else average) / 60)
    pace_text = f" · ~{format_pace(pace)}/mi" if pace else ""
    lines = list(spec["structure"])
    if pace and run_type in {"tempo", "intervals", "marathon_pace"}:
        easy = format_pace(mp * PACE_FACTORS["easy"])
        lines.append(f"Paces: {spec['label'].lower()} ~{format_pace(pace)}/mi, easy ~{easy}/mi")
    return {
        "date": day.isoformat(), "type": run_type, "title": title or spec["label"], "miles": float(miles),
        "minutes": max(15, minutes), "description": f"{phase}: {spec['label'].lower()} · target {miles:g} mi{pace_text}", "workout": lines,
    }
