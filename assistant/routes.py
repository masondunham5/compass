"""`/api/assistant/*`: calendar feeds, agenda, and week outlook."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from assistant import availability, calendars
from core import connection

router = APIRouter(prefix="/api/assistant")


class FeedRequest(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    url: str = Field(min_length=8, max_length=2000)
    kind: str = "school"
    fixed: bool = True


@router.get("/calendars")
def list_calendars() -> dict[str, Any]:
    with connection() as db:
        return {"calendars": calendars.list_feeds(db)}


@router.post("/calendars")
def add_calendar(payload: FeedRequest) -> dict[str, Any]:
    """Add a calendar link. It is fetched right away; nothing is saved if it can't be read."""
    with connection() as db:
        try:
            feed = calendars.add_feed(db, payload.name, payload.kind, url=payload.url, fixed=payload.fixed)
        except calendars.CalendarError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"calendar": feed, "calendars": calendars.list_feeds(db)}


@router.post("/calendars/upload")
async def upload_calendar(file: UploadFile = File(...), name: str = Form("Calendar"), kind: str = Form("school")) -> dict[str, Any]:
    content = await file.read(calendars.MAX_BYTES + 1)
    if len(content) > calendars.MAX_BYTES:
        raise HTTPException(status_code=400, detail="That file is larger than 10 MB.")
    with connection() as db:
        try:
            feed = calendars.add_feed(db, name, kind, content=content)
        except calendars.CalendarError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"calendar": feed, "calendars": calendars.list_feeds(db)}


@router.post("/calendars/{feed_id}/sync")
def sync_calendar(feed_id: str) -> dict[str, Any]:
    with connection() as db:
        try:
            calendars.sync_feed(db, feed_id)
        except calendars.CalendarError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"calendars": calendars.list_feeds(db)}


@router.delete("/calendars/{feed_id}")
def delete_calendar(feed_id: str) -> dict[str, Any]:
    with connection() as db:
        if not calendars.remove_feed(db, feed_id):
            raise HTTPException(status_code=404, detail="Calendar not found.")
        return {"calendars": calendars.list_feeds(db)}


def _workouts(db, start: date, end: date) -> list[dict[str, Any]]:
    rows = db.execute("SELECT id, session_date, kind, title, minutes, status FROM sessions WHERE session_date BETWEEN ? AND ? AND status != 'cancelled' ORDER BY session_date",
                      (start.isoformat(), end.isoformat())).fetchall()
    return [dict(row) for row in rows]


@router.get("/agenda")
def agenda(day: str | None = None) -> dict[str, Any]:
    """One day: calendar events, free time, and the best slot for that day's workout."""
    try:
        target = date.fromisoformat(day) if day else date.today()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Use YYYY-MM-DD.") from error
    with connection() as db:
        workouts = _workouts(db, target, target)
        planned = [w for w in workouts if w["status"] == "planned"]
        minutes = max((w["minutes"] for w in planned), default=60)
        return {**availability.day_summary(db, target, minutes), "workouts": workouts}


@router.get("/week")
def week(start: str | None = None, days: int = 7) -> dict[str, Any]:
    try:
        first = date.fromisoformat(start) if start else date.today()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Use YYYY-MM-DD.") from error
    days = max(1, min(days, 28))
    with connection() as db:
        workouts = _workouts(db, first, first + timedelta(days=days - 1))
        out = []
        for offset in range(days):
            day = first + timedelta(days=offset)
            summary = availability.day_summary(db, day)
            out.append({key: summary[key] for key in ("date", "busy_minutes", "exams", "heavy", "best_slot")} | {
                "event_count": len(summary["events"]), "workouts": [w for w in workouts if w["session_date"] == day.isoformat()]})
        return {"days": out}


@router.get("/alerts")
def alerts() -> dict[str, Any]:
    with connection() as db:
        return {"alerts": availability.exam_alerts(db)}
