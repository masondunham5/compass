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


# ---------------------------------------------------------------- school (read-only from School To Do)

@router.get("/school")
def school_tasks(include_done: bool = False) -> dict[str, Any]:
    from datetime import datetime

    from assistant import planner, school
    status = school.status()
    if not status["connected"]:
        return {"status": status, "tasks": []}
    now = datetime.now()
    with connection() as db:
        forced = planner.forced_overdue(db)
        planned = {}
        for row in db.execute("SELECT ref, SUM(end - start) AS minutes FROM plan_blocks WHERE kind IN ('study', 'review') GROUP BY ref"):
            planned[row["ref"]] = row["minutes"]
    tasks = []
    for task in school.load_tasks():
        if task.done and not include_done:
            continue
        if task.kind == "prep" and task.due < now:
            continue  # past weekly budgets lapse
        tasks.append({"key": task.key, "due_date": task.due_date, "due_time": task.due_time, "course": task.course, "kind": task.kind,
                      "title": task.title, "minutes": task.minutes, "done": task.done, "overdue": task.due < now,
                      "planned_minutes": planned.get(task.key, 0), "forced": task.key in forced})
    return {"status": status, "tasks": tasks}


class ForceRequest(BaseModel):
    key: str = Field(min_length=3, max_length=400)
    on: bool = True


@router.post("/school/force")
def force_overdue(payload: ForceRequest) -> dict[str, Any]:
    from assistant import planner
    with connection() as db:
        return {"forced": sorted(planner.set_forced(db, payload.key, payload.on))}


# ---------------------------------------------------------------- work schedule

class ShiftRequest(BaseModel):
    start: str
    end: str
    weekday: int | None = Field(default=None, ge=0, le=6)
    shift_date: str | None = None
    applies: str = "school"
    note: str = Field(default="", max_length=60)


class TermRequest(BaseModel):
    name: str = Field(default="", max_length=60)
    kind: str = "school"
    start_date: str
    end_date: str


@router.get("/work")
def work_schedule() -> dict[str, Any]:
    from assistant import work
    with connection() as db:
        return work.schedule(db)


@router.post("/work/shifts")
def add_shift(payload: ShiftRequest) -> dict[str, Any]:
    from assistant import work
    with connection() as db:
        try:
            work.add_shift(db, payload.start, payload.end, payload.weekday, payload.shift_date or None, payload.applies, payload.note)
        except (work.WorkError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error) if isinstance(error, work.WorkError) else "Use a valid date.") from error
        return work.schedule(db)


@router.delete("/work/shifts/{shift_id}")
def delete_shift(shift_id: int) -> dict[str, Any]:
    from assistant import work
    with connection() as db:
        if not db.execute("DELETE FROM work_shifts WHERE id = ?", (shift_id,)).rowcount:
            raise HTTPException(status_code=404, detail="Shift not found.")
        return work.schedule(db)


@router.post("/work/terms")
def add_term(payload: TermRequest) -> dict[str, Any]:
    from assistant import work
    with connection() as db:
        try:
            work.add_term(db, payload.name, payload.kind, payload.start_date, payload.end_date)
        except (work.WorkError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error) if isinstance(error, work.WorkError) else "Use valid dates.") from error
        return work.schedule(db)


@router.delete("/work/terms/{term_id}")
def delete_term(term_id: int) -> dict[str, Any]:
    from assistant import work
    with connection() as db:
        if not db.execute("DELETE FROM terms WHERE id = ?", (term_id,)).rowcount:
            raise HTTPException(status_code=404, detail="Term not found.")
        return work.schedule(db)


# ---------------------------------------------------------------- planner

class AcceptRequest(BaseModel):
    blocks: list[dict[str, Any]] = Field(min_length=1, max_length=200)


class UnacceptRequest(BaseModel):
    block_id: str | None = None
    from_date: str | None = None


@router.get("/plan")
def plan(start: str | None = None, days: int = 7) -> dict[str, Any]:
    from assistant import planner
    try:
        first = date.fromisoformat(start) if start else None
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Use YYYY-MM-DD.") from error
    with connection() as db:
        return planner.build(db, first, max(1, min(days, 14)))


@router.post("/plan/accept")
def accept_plan(payload: AcceptRequest) -> dict[str, Any]:
    from assistant import outlook, planner
    with connection() as db:
        try:
            saved = planner.accept(db, payload.blocks)
        except (KeyError, ValueError, TypeError) as error:
            raise HTTPException(status_code=400, detail="Those plan blocks are malformed. Rebuild the plan and try again.") from error
    outlook.sync_soon()
    return {"accepted": saved}


@router.post("/plan/unaccept")
def unaccept_plan(payload: UnacceptRequest) -> dict[str, Any]:
    from assistant import outlook, planner
    with connection() as db:
        removed = planner.unaccept(db, payload.block_id, payload.from_date)
    outlook.sync_soon()
    return {"removed": removed}


# ---------------------------------------------------------------- personal Outlook (write-back)

class OutlookConnect(BaseModel):
    client_id: str = Field(max_length=200)  # start_sign_in checks the GUID shape with a readable message


@router.get("/outlook")
def outlook_status() -> dict[str, Any]:
    from assistant import outlook
    with connection() as db:
        return outlook.status(db)


@router.post("/outlook/connect")
def outlook_connect(payload: OutlookConnect) -> dict[str, Any]:
    """Start Microsoft's device sign-in; returns the code to enter at microsoft.com/devicelogin."""
    from assistant import outlook
    try:
        return outlook.start_sign_in(payload.client_id)
    except outlook.OutlookError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/outlook/sync")
def outlook_sync() -> dict[str, Any]:
    from assistant import outlook
    with connection() as db:
        try:
            counts = outlook.sync(db)
        except outlook.OutlookError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {**outlook.status(db), "result": counts}


@router.post("/outlook/disconnect")
def outlook_disconnect() -> dict[str, Any]:
    """Forget the sign-in on this computer. The Compass calendar stays in Outlook (delete it there if you want)."""
    from assistant import outlook
    with connection() as db:
        outlook.disconnect(db)
        return outlook.status(db)
