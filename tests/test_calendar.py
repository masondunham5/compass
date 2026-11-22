"""Your own events (repeats, edits), to-dos in the planner, personal Outlook as busy time, and the calendar grid API."""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import core  # noqa: E402
from assistant import availability, events, outlook, planner  # noqa: E402
from tests.test_outlook import FakeGraph  # noqa: E402

TODAY = date.today()
TOMORROW = TODAY + timedelta(days=1)
NOW = datetime.combine(TODAY, datetime.min.time()).replace(hour=8)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    monkeypatch.setattr(core, "DATABASE", tmp_path / "test.db")
    monkeypatch.setattr(core, "LEGACY_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(core, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setenv("SCHOOL_TODO_APP", str(tmp_path / "no_school" / "app.py"))  # no schoolwork in these tests
    monkeypatch.setenv("SCHOOL_TODO_DATA", str(tmp_path / "no_school"))
    monkeypatch.setattr(outlook, "_access_token", lambda db: "test-token")
    core.initialize()
    with core.connection() as connection:
        connection.execute("DELETE FROM sessions")  # demo/legacy workouts would compete for slots
        connection.commit()
        yield connection


def at(hour: int, minute: int = 0) -> int:
    return hour * 60 + minute


# ---------------------------------------------------------------- events

def test_weekly_event_repeats_and_can_skip_split_or_end(db):
    event = events.add_event(db, {"title": "Club", "start_date": TODAY.isoformat(), "start": at(18), "end": at(19), "repeat": "weekly"})
    dates = [o["occurrence"] for o in events.occurrences(db, TODAY, TODAY + timedelta(days=27))]
    assert dates == [(TODAY + timedelta(days=7 * n)).isoformat() for n in range(4)]

    week2 = (TODAY + timedelta(days=7)).isoformat()
    events.delete_event(db, event["id"], "one", week2)
    assert week2 not in [o["occurrence"] for o in events.occurrences(db, TODAY, TODAY + timedelta(days=27))]

    week3 = (TODAY + timedelta(days=14)).isoformat()
    moved = events.update_event(db, event["id"], {"start": at(20), "end": at(21)}, "one", week3)
    assert moved["repeat"] == "none" and moved["start_date"] == week3
    on_week3 = events.occurrences(db, date.fromisoformat(week3), date.fromisoformat(week3))
    assert [(o["title"], o["start_minutes"]) for o in on_week3] == [("Club", at(20))]

    events.delete_event(db, event["id"], "future", (TODAY + timedelta(days=21)).isoformat())
    later = events.occurrences(db, TODAY + timedelta(days=21), TODAY + timedelta(days=60))
    assert later == []


def test_event_validation_is_readable(db):
    with pytest.raises(events.EventError, match="end after it starts"):
        events.add_event(db, {"title": "x", "start_date": TODAY.isoformat(), "start": at(10), "end": at(9)})
    with pytest.raises(events.EventError, match="name"):
        events.add_event(db, {"title": " ", "start_date": TODAY.isoformat(), "start": at(9), "end": at(10)})


def test_weekdays_and_all_day_events(db):
    events.add_event(db, {"title": "Gym class", "start_date": TODAY.isoformat(), "start": at(7), "end": at(8), "repeat": "weekdays"})
    events.add_event(db, {"title": "Trip", "start_date": TOMORROW.isoformat(), "all_day": True})
    week = events.occurrences(db, TODAY, TODAY + timedelta(days=6))
    assert all(date.fromisoformat(o["occurrence"]).weekday() < 5 for o in week if o["title"] == "Gym class")
    trip = [o for o in week if o["title"] == "Trip"][0]
    assert trip["all_day"] and trip["start"].endswith("T00:00")


# ---------------------------------------------------------------- planner

def test_planner_works_around_your_events_and_schedules_todos(db):
    events.add_event(db, {"title": "Dentist", "start_date": TOMORROW.isoformat(), "start": at(9), "end": at(12)})
    todo = events.add_todo(db, {"title": "Renew license", "minutes": 45, "due_date": TOMORROW.isoformat(), "due_time": "17:00"})
    plan = planner.build(db, TOMORROW, 1, now=NOW)
    tasks = [b for day in plan["days"] for b in day["blocks"] if b["kind"] == "task"]
    assert len(tasks) == 1 and tasks[0]["ref"] == todo["id"] and tasks[0]["end"] - tasks[0]["start"] == 45
    assert tasks[0]["start"] >= at(12) + planner.SETTINGS["gap"] and tasks[0]["end"] <= at(17)


def test_accepted_todo_time_counts_and_checking_it_off_frees_upcoming_blocks(db):
    todo = events.add_todo(db, {"title": "Taxes", "minutes": 120})
    plan = planner.build(db, TOMORROW, 3, now=NOW)
    drafts = [b for day in plan["days"] for b in day["blocks"] if b["kind"] == "task"]
    assert sum(b["end"] - b["start"] for b in drafts) == 120
    planner.accept(db, drafts)
    again = planner.build(db, TOMORROW, 3, now=NOW)
    assert not [b for day in again["days"] for b in day["blocks"] if b["kind"] == "task" and b["status"] == "draft"]
    events.update_todo(db, todo["id"], {"done": True})
    planner.cleanup(db)
    assert db.execute("SELECT COUNT(*) FROM plan_blocks WHERE kind = 'task'").fetchone()[0] == 0


def test_overdue_todo_is_still_scheduled(db):
    events.add_todo(db, {"title": "Call landlord", "minutes": 15, "due_date": (TODAY - timedelta(days=2)).isoformat()})
    plan = planner.build(db, TOMORROW, 2, now=NOW)
    assert [b["title"] for day in plan["days"] for b in day["blocks"] if b["kind"] == "task"] == ["Call landlord"]


# ---------------------------------------------------------------- personal Outlook

def utc(day: date, hour: int) -> str:
    local = datetime.combine(day, datetime.min.time()).replace(hour=hour).astimezone()
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000")


def test_outlook_events_are_read_as_busy_time(db):
    graph = FakeGraph()
    graph.default_calendar = [
        {"id": "o1", "subject": "Coffee with Sam", "start": {"dateTime": utc(TOMORROW, 9), "timeZone": "UTC"},
         "end": {"dateTime": utc(TOMORROW, 21), "timeZone": "UTC"}, "isAllDay": False, "showAs": "busy", "location": {"displayName": "Cafe"}},
        {"id": "o2", "subject": "Cancelled thing", "isCancelled": True, "start": {"dateTime": utc(TOMORROW, 8), "timeZone": "UTC"},
         "end": {"dateTime": utc(TOMORROW, 9), "timeZone": "UTC"}},
    ]
    assert outlook.sync(db, transport=graph)["read"] == 1
    cached = outlook.events_between(db, TOMORROW, TOMORROW)
    assert [(e["title"], e["start"][11:], e["location"]) for e in cached] == [("Coffee with Sam", "09:00", "Cafe")]
    summary = availability.day_summary(db, TOMORROW)
    assert summary["busy_minutes"] >= 12 * 60 - 60  # 9 AM–9 PM, clipped to the planning day
    events.add_todo(db, {"title": "Laundry", "minutes": 60, "due_date": TOMORROW.isoformat()})
    blocks = [b for day in planner.build(db, TOMORROW, 1, now=NOW)["days"] for b in day["blocks"] if b["kind"] == "task"]
    assert all(b["start"] >= at(21) or b["end"] <= at(9) for b in blocks)


def test_your_events_are_written_to_outlook_and_removed_when_deleted(db):
    graph = FakeGraph()
    event = events.add_event(db, {"title": "Book club", "start_date": TOMORROW.isoformat(), "start": at(19), "end": at(20), "repeat": "daily",
                                  "repeat_until": (TOMORROW + timedelta(days=2)).isoformat(), "location": "Library"})
    assert outlook.sync(db, transport=graph)["created"] == 3
    assert {e["subject"] for e in graph.events.values()} == {"Book club"}
    assert all(e["location"]["displayName"] == "Library" and e["calendar"] == "cal1" for e in graph.events.values())
    events.delete_event(db, event["id"], "one", TOMORROW.isoformat())
    assert outlook.sync(db, transport=graph)["deleted"] == 1
    events.update_event(db, event["id"], {"title": "Book club (moved)"})
    assert outlook.sync(db, transport=graph)["updated"] == 2
    events.delete_event(db, event["id"])
    assert outlook.sync(db, transport=graph)["deleted"] == 2 and not graph.events


# ---------------------------------------------------------------- API

def test_calendar_api_combines_every_source(db):
    db.execute("INSERT INTO calendar_feeds (id, name, kind, source, created_at) VALUES ('f1', 'School', 'school', 'file', 't')")
    db.execute("INSERT INTO calendar_events (feed_id, uid, title, start, end, all_day, busy, location) VALUES ('f1', 'u1', 'CSE 231 Lecture', ?, ?, 0, 1, 'EB')",
               (f"{TOMORROW.isoformat()}T10:20", f"{TOMORROW.isoformat()}T11:40"))
    db.execute("INSERT INTO outlook_events (id, title, start, end, all_day, busy) VALUES ('o1', 'Haircut', ?, ?, 0, 1)",
               (f"{TOMORROW.isoformat()}T16:00", f"{TOMORROW.isoformat()}T16:30"))
    db.commit()
    with TestClient(app.app) as client:
        created = client.post("/api/assistant/events", json={"title": "Dinner", "start_date": TOMORROW.isoformat(), "start": at(18), "end": at(19, 30)})
        assert created.status_code == 200
        assert client.post("/api/assistant/todos", json={"title": "Email advisor", "minutes": 20}).status_code == 200
        assert client.post("/api/assistant/events", json={"title": "Bad", "start_date": TOMORROW.isoformat(), "start": at(9), "end": at(8)}).status_code == 400
        grid = client.get(f"/api/assistant/calendar?start={TOMORROW.isoformat()}&days=1").json()
        items = grid["days"][0]["items"]
        by_source = {item["source"]: item for item in items}
        assert by_source["feed"]["title"] == "CSE 231 Lecture" and by_source["feed"]["start"] == at(10, 20)
        assert by_source["outlook"]["title"] == "Haircut"
        assert by_source["mine"]["title"] == "Dinner" and by_source["mine"]["event_id"] == created.json()["event"]["id"]
        assert by_source["draft"]["kind"] == "task" and by_source["draft"]["title"] == "Email advisor"
        todo_id = client.get("/api/assistant/todos").json()["todos"][0]["id"]
        assert client.patch(f"/api/assistant/todos/{todo_id}", json={"done": True}).json()["todos"] == []
        event_id = created.json()["event"]["id"]
        assert client.patch(f"/api/assistant/events/{event_id}", json={"start": at(19), "end": at(20)}).json()["event"]["title"] == "Dinner"
        assert client.delete(f"/api/assistant/events/{event_id}").status_code == 200
        assert client.get(f"/api/assistant/events/{event_id}").status_code == 404
