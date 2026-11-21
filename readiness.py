"""Deterministic readiness/training recommendation rules (no AI; never changes the schedule)."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field

class CheckIn(BaseModel):
    available_minutes: int = Field(ge=0, le=360)
    readiness: int = Field(ge=1, le=10)
    soreness: int = Field(ge=1, le=10)
    location: str = "Full gym"


def recommendation(checkin: CheckIn, sessions: list[dict[str, Any]]) -> tuple[str, str | None]:
    candidates = [session for session in sessions if session["status"] == "planned"]
    candidates.sort(key=lambda item: (0 if item["kind"] == "strength" else 1, item["date"]))
    if checkin.readiness <= 3 or checkin.soreness >= 8:
        return "Recovery first: take 20–30 easy minutes of walking and mobility. No catch-up volume today.", None
    if not candidates:
        return "No planned training remains in the next two weeks. A relaxed walk or mobility is enough today.", None
    target = candidates[0]
    if checkin.available_minutes < max(20, target["minutes"] * 0.6):
        if target["kind"] == "strength" and checkin.location == "Apartment gym":
            return "Apartment backup: do a short back-and-biceps session today. Keep the main gym session for another day.", target["id"]
        return f"Short version: do the first 2–3 movements of {target['title']} in {checkin.available_minutes} minutes.", target["id"]
    return f"Do {target['title']} today. It protects your lifting consistency and weekly mileage plan.", target["id"]


def health_recommendation(record: dict[str, Any] | None) -> str:
    """A conservative coaching prompt, not medical advice or an automatic schedule change."""
    if not record:
        return "Log a quick health check-in to give today’s training recommendation some context."
    if record["sleep_hours"] < 5.5 or record["energy"] <= 3 or record["soreness"] >= 8:
        return "Recovery first: keep movement easy today. A walk, mobility, or a shortened session is more useful than forced catch-up."
    if record["sleep_hours"] < 6.5 or record["stress"] >= 8:
        return "Protect recovery today: keep intensity controlled and leave a few reps in reserve."
    if record["protein_goal_met"]:
        return "You have a solid recovery foundation today. Follow the planned session and use RPE to guide any load changes."
    return "Readiness looks workable. Train as planned, then prioritize a protein-focused meal after your session."


def mean_metric(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [float(row[key]) for row in rows if row.get(key) is not None]
    return round(sum(values) / len(values), 1) if values else None


def deterministic_training_recommendation(
    sessions: list[dict[str, Any]], daily_metrics: list[dict[str, Any]], manual_health: dict[str, Any] | None = None,
    recent_training_days: int = 0,
) -> dict[str, Any]:
    """Explainable rule engine. It never writes, moves, or cancels a session."""
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    has_night = lambda row: row and (row.get("sleep_seconds") is not None or row.get("hrv_ms") is not None)
    # Garmin files last night's sleep under today's date. Until it syncs, fall back to the
    # previous night (never older) and say so; anything older is only baseline history.
    current = next((row for row in daily_metrics if row.get("calendar_date") == today), None)
    stale_note = None
    if not has_night(current):
        previous = next((row for row in daily_metrics if row.get("calendar_date") == yesterday), None)
        if has_night(previous):
            current = previous
            stale_note = f"Today's sleep hasn't synced from Garmin yet, so this uses the previous night ({yesterday})."
    used_date = current.get("calendar_date") if current else None
    baseline_rows = [row for row in daily_metrics if row.get("calendar_date") not in {today, used_date}][:28]
    baseline_hrv = mean_metric(baseline_rows, "hrv_ms")
    baseline_rhr = mean_metric(baseline_rows, "resting_hr")
    score = 100
    reasons: list[str] = []
    headlines: list[str] = []  # Short, plain-language version of each triggered rule for the Today banner.
    if current and current.get("sleep_seconds") is not None:
        sleep_hours = float(current["sleep_seconds"]) / 3600
        slept = f"You slept {int(sleep_hours)}h {round((sleep_hours % 1) * 60):02d}m."
        if sleep_hours < 6:
            score -= 30; reasons.append(f"Sleep was {sleep_hours:.1f}h (under the 6h recovery floor)."); headlines.append(slept)
        elif sleep_hours < 7:
            score -= 12; reasons.append(f"Sleep was {sleep_hours:.1f}h (below the 7h target)."); headlines.append(slept)
        sleep_score = current.get("sleep_score")
        if sleep_hours >= 6 and sleep_score is not None and float(sleep_score) < 50:
            score -= 8; reasons.append(f"Garmin sleep score was {round(float(sleep_score))} (poor quality)."); headlines.append("Sleep quality was poor.")
    hrv_flagged = False
    if current and baseline_hrv and current.get("hrv_ms") is not None and float(current["hrv_ms"]) <= baseline_hrv * 0.85:
        drop = round((1 - float(current["hrv_ms"]) / baseline_hrv) * 100)
        score -= 20; reasons.append(f"HRV is {drop}% below your rolling baseline."); headlines.append(f"HRV is {drop}% below baseline.")
        hrv_flagged = True
    hrv_status = str((current or {}).get("hrv_status") or "").upper()
    if not hrv_flagged and hrv_status in {"LOW", "POOR"}:
        score -= 15; reasons.append("Garmin HRV status is low."); headlines.append("HRV status is low.")
    elif not hrv_flagged and hrv_status == "UNBALANCED":
        score -= 8; reasons.append("Garmin HRV status is unbalanced."); headlines.append("HRV status is unbalanced.")
    wake_battery = (current or {}).get("body_battery_at_wake")
    if wake_battery is not None and float(wake_battery) < 25:
        score -= 12; reasons.append(f"Body Battery was only {round(float(wake_battery))} at wake."); headlines.append(f"Body Battery woke at {round(float(wake_battery))}.")
    elif wake_battery is not None and float(wake_battery) < 40:
        score -= 6; reasons.append(f"Body Battery was {round(float(wake_battery))} at wake (below 40).")
    if recent_training_days >= 3:
        score -= 5; reasons.append("You trained each of the last 3 days."); headlines.append("Three training days in a row.")
    if current and baseline_rhr and current.get("resting_hr") is not None and float(current["resting_hr"]) >= baseline_rhr * 1.08:
        rise = round((float(current["resting_hr"]) / baseline_rhr - 1) * 100)
        score -= 15; reasons.append(f"Resting HR is {rise}% above your rolling baseline."); headlines.append(f"Resting HR is {rise}% above baseline.")
    if current and current.get("stress_avg") is not None and float(current["stress_avg"]) >= 65:
        score -= 10; reasons.append("Garmin stress average is elevated (65 or above)."); headlines.append("Stress is running high.")
    if manual_health:
        if int(manual_health.get("soreness", 0)) >= 8:
            score -= 30; reasons.append("Manual soreness is 8/10 or higher."); headlines.append("You logged high soreness.")
        elif int(manual_health.get("soreness", 0)) >= 6:
            score -= 8; reasons.append("Manual soreness is 6/10 or higher."); headlines.append("You logged some soreness.")
        if int(manual_health.get("energy", 10)) <= 3:
            score -= 25; reasons.append("Manual energy is 3/10 or lower."); headlines.append("You logged low energy.")
        elif int(manual_health.get("energy", 10)) <= 5:
            score -= 8; reasons.append("Manual energy is 5/10 or lower."); headlines.append("You logged lowish energy.")
    score = max(0, score)
    planned = [session for session in sessions if session.get("status") == "planned"]
    planned.sort(key=lambda session: (0 if session.get("date") == today else 1, session.get("date", ""), session.get("title", "")))
    target = next((session for session in planned if session.get("locked")), None) or (planned[0] if planned else None)
    target_name = re.sub(r"^W\d+\s*·\s*", "", target["title"]) if target else "your next training block"
    adjusted_sets: int | None = None
    adjusted_minutes: int | None = None
    if score <= 50:
        title, mode = "Recovery-first day", "recovery"
        guidance = "Take 20–30 easy minutes of walking and mobility, or rest. Do not add catch-up volume today."
        action = "Take an easy walk or mobility today, or rest."
    elif score <= 75:
        title, mode = "Use the short version", "shorten"
        if target and target.get("kind") == "run":
            adjusted_minutes = max(20, round(target.get("minutes", 30) * 0.6))
            guidance = f"Keep {target_name} easy and cap it around {adjusted_minutes} minutes. Skip intensity."
            action = f"Keep the run easy and cap it around {adjusted_minutes} min."
        elif target:
            # Keep every exercise for consistency; trim volume by capping each at two working sets.
            adjusted_sets = 2
            adjusted_minutes = max(25, round(target.get("minutes", 45) * 0.6))
            guidance = f"Keep every lift in {target_name} but cap each at 2 working sets (about {adjusted_minutes} minutes). Leave 2–3 reps in reserve."
            action = f"Keep the lifts, cut each to 2 sets (about {adjusted_minutes} min)."
        else:
            guidance = "Keep activity easy today and reassess after a check-in tomorrow."
            action = "Keep activity easy today."
    elif target:
        title, mode = "Planned session is appropriate", "train"
        guidance = f"Do {target_name} as planned. Keep the opening working sets controlled and adjust load by effort."
        action = "Train as planned."
    else:
        title, mode = "No session needs moving", "rest"
        guidance = "You have no planned session in the next week. A relaxed walk or mobility is enough today."
        action = "No session planned. An easy walk is plenty."
    if not reasons:
        reasons.append("No recovery flag crossed a conservative threshold; recommendation is based on the planned session.")
    if stale_note:
        reasons.insert(0, stale_note)
    elif not current:
        reasons.insert(0, "No Garmin sleep or HRV for last night yet; the score uses only your check-in.")
    return {
        "engine": "deterministic-v2", "score": score, "mode": mode, "title": title, "guidance": guidance,
        "data_date": used_date, "data_is_stale": bool(stale_note),
        "headline": " ".join([*headlines[:1], action]),
        "adjusted_sets": adjusted_sets, "adjusted_minutes": adjusted_minutes,
        "target_session": {"id": target["id"], "title": target["title"], "date": target.get("date"), "kind": target.get("kind"), "minutes": target.get("minutes"), "locked": bool(target.get("locked"))} if target else None,
        "reasons": reasons,
        "signals": {"sleep_hours": round(float(current["sleep_seconds"]) / 3600, 1) if current and current.get("sleep_seconds") is not None else None, "hrv": current.get("hrv_ms") if current else None, "hrv_baseline": baseline_hrv, "resting_hr": current.get("resting_hr") if current else None, "resting_hr_baseline": baseline_rhr, "manual_energy": manual_health.get("energy") if manual_health else None, "manual_soreness": manual_health.get("soreness") if manual_health else None},
    }
