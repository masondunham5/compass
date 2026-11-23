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
    assert all(e["location"]["displayName"] == "Library" and e["calendar"] == "main" for e in graph.events.values())
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


# ---------------------------------------------------------------- changing your personal Outlook events

def personal(eid, subject, day, start_hour, end_hour, **extra):
    return {"id": eid, "subject": subject, "start": {"dateTime": utc(day, start_hour), "timeZone": "UTC"},
            "end": {"dateTime": utc(day, end_hour), "timeZone": "UTC"}, "isAllDay": False, "showAs": "busy", **extra}


def test_personal_outlook_events_can_be_edited_and_deleted(db):
    graph = FakeGraph()
    graph.default_calendar = [personal("p1", "Errand", TOMORROW, 14, 15), personal("p2", "Gym", TOMORROW, 7, 8, type="occurrence")]
    outlook.sync(db, transport=graph)
    moved = outlook.edit_event(db, "p1", {"title": "Errand (bank)", "start": at(16), "end": at(17, 30)}, transport=graph)
    assert (moved["title"], moved["start"][11:], moved["end"][11:]) == ("Errand (bank)", "16:00", "17:30")
    assert graph.default_calendar[0]["subject"] == "Errand (bank)" and graph.default_calendar[0]["start"]["dateTime"] == utc(TOMORROW, 16)[:19]
    assert outlook.events_between(db, TOMORROW, TOMORROW)[-1]["title"] == "Errand (bank)"  # local copy updated right away
    with pytest.raises(outlook.OutlookError):
        outlook.edit_event(db, "p1", {"start": at(18), "end": at(17)}, transport=graph)
    assert [e["series"] for e in outlook.events_between(db, TOMORROW, TOMORROW)] == [True, False]
    outlook.delete_event(db, "p2", transport=graph)
    assert [e["id"] for e in graph.default_calendar] == ["p1"]
    with pytest.raises(outlook.OutlookError):
        outlook.delete_event(db, "nope", transport=graph)  # only events Compass read from your Calendar


def test_only_your_own_short_future_events_are_movable(db):
    graph = FakeGraph()
    graph.default_calendar = [personal("a", "Errand", TOMORROW, 14, 15), personal("b", "Invite", TOMORROW, 10, 11, isOrganizer=False),
                              personal("c", "Trip", TOMORROW, 8, 20), personal("d", "Reminder", TOMORROW, 12, 13, showAs="free")]
    outlook.sync(db, transport=graph)
    assert {e["title"]: e["movable"] for e in outlook.events_between(db, TOMORROW, TOMORROW)} == {"Trip": False, "Invite": False, "Reminder": False, "Errand": True}


def test_planner_suggests_moving_a_personal_event_to_meet_a_deadline(db, monkeypatch):
    graph = FakeGraph()
    after = TOMORROW + timedelta(days=1)
    graph.default_calendar = [personal("busy1", "Shift swap", TOMORROW, 7, 14, isOrganizer=False), personal("errand", "Errand", TOMORROW, 14, 16),
                              personal("busy2", "Family", TOMORROW, 16, 23, isOrganizer=False)]
    outlook.sync(db, transport=graph)
    events.add_todo(db, {"title": "Pay rent", "minutes": 60, "due_date": TOMORROW.isoformat(), "due_time": "18:00"})
    plan = planner.build(db, TOMORROW, 2, now=NOW)
    drafts = [b for day in plan["days"] for b in day["blocks"] if b["status"] == "draft"]
    task = next(b for b in drafts if b["kind"] == "task")
    move = next(b for b in drafts if b["kind"] == "move")
    assert (task["block_date"], task["start"]) == (TOMORROW.isoformat(), at(14, 15))
    assert (move["ref"], move["from_date"], move["from_start"], move["block_date"], move["start"], move["end"]) == \
        ("errand", TOMORROW.isoformat(), at(14), after.isoformat(), at(14), at(16))
    assert not any("Pay rent" in w for w in plan["warnings"])

    # Ignoring the suggestion changes nothing; accepting it moves the event in Outlook.
    assert graph.default_calendar[1]["start"]["dateTime"].startswith(utc(TOMORROW, 14)[:16])
    monkeypatch.setattr(outlook, "_urllib_transport", graph)
    db.commit()  # visible to the app's own connections
    with TestClient(app.app) as client:
        grid = client.get(f"/api/assistant/calendar?start={TOMORROW.isoformat()}&days=2").json()
        errand = next(i for i in grid["days"][0]["items"] if i["source"] == "outlook" and i["title"] == "Errand")
        assert errand["movable"] and errand["moving"] and errand["outlook_id"] == "errand"
        clean = {k: v for k, v in move.items() if k not in ("status", "start_label", "end_label")}
        assert client.post("/api/assistant/plan/accept", json={"blocks": [clean, {k: v for k, v in task.items() if k not in ("status", "start_label", "end_label")}]}).json() == {"accepted": 2, "moved": 1}
        assert graph.default_calendar[1]["start"]["dateTime"] == utc(after, 14)[:19]
        assert client.patch("/api/assistant/outlook/events/errand", json={"title": "Errand!"}).json()["event"]["title"] == "Errand!"
        assert client.patch("/api/assistant/outlook/events/missing", json={"title": "x"}).status_code == 400
        assert client.delete("/api/assistant/outlook/events/errand").json() == {"deleted": True}


# ---------------------------------------------------------------- check-in: done / missed

NOON = datetime.combine(TODAY, datetime.min.time()).replace(hour=12)


def accept_task_block(db, todo_id, title, start, end, day=TODAY):
    block = {"id": f"task:{todo_id}:{day.isoformat()}:{start}", "block_date": day.isoformat(), "start": start, "end": end,
             "kind": "task", "ref": todo_id, "title": title, "course": ""}
    planner.accept(db, [block])
    return block["id"]


def test_missed_block_is_rescheduled_automatically(db):
    todo = events.add_todo(db, {"title": "Call insurance", "minutes": 60})
    block_id = accept_task_block(db, todo["id"], "Call insurance", at(9), at(10))
    assert [b["id"] for b in planner.checkin(db, now=NOON)] == [block_id]
    result = planner.mark(db, block_id, "missed", now=NOON)
    assert result["outcome"] == "missed" and len(result["rescheduled"]) == 1 and result["message"].startswith("Rescheduled to")
    new = result["rescheduled"][0]
    assert new["block_date"] == TODAY.isoformat() and new["start"] >= at(12) and new["end"] - new["start"] == 60
    rows = {row["id"]: row["outcome"] for row in db.execute("SELECT id, outcome FROM plan_blocks")}
    assert rows == {block_id: "missed", new["id"]: ""}
    assert planner.checkin(db, now=NOON) == []  # marked; the new block hasn't ended yet
    # The new block covers the to-do, so the next draft adds nothing for it.
    drafts = [b for d in planner.build(db, TODAY, 7, now=NOON)["days"] for b in d["blocks"] if b["status"] == "draft" and b["ref"] == todo["id"]]
    assert drafts == []


def test_done_block_counts_and_future_blocks_cant_be_checked_in(db):
    todo = events.add_todo(db, {"title": "Taxes", "minutes": 60})
    block_id = accept_task_block(db, todo["id"], "Taxes", at(9), at(10))
    assert planner.mark(db, block_id, "done", now=NOON)["rescheduled"] == []
    assert not [b for d in planner.build(db, TODAY, 7, now=NOON)["days"] for b in d["blocks"] if b["status"] == "draft" and b["ref"] == todo["id"]]
    later = accept_task_block(db, todo["id"], "Taxes", at(15), at(16))
    with pytest.raises(ValueError):
        planner.mark(db, later, "missed", now=NOON)
    with pytest.raises(KeyError):
        planner.mark(db, "nope", "done", now=NOON)
    assert planner.mark(db, block_id, "", now=NOON)["outcome"] == ""  # undo


def test_missed_with_no_time_left_explains_why(db):
    due = TODAY.isoformat()
    todo = events.add_todo(db, {"title": "Mail form", "minutes": 60, "due_date": due, "due_time": "12:20"})  # only 20 min left
    block_id = accept_task_block(db, todo["id"], "Mail form", at(9), at(10))
    result = planner.mark(db, block_id, "missed", now=NOON)
    assert result["rescheduled"] == [] and "Mail form" in result["message"]


def test_outlook_shows_done_and_drops_missed(db):
    graph = FakeGraph()
    done = accept_task_block(db, "t1", "Groceries", at(9), at(10))
    missed = accept_task_block(db, "t2", "Laundry", at(10, 30), at(11))
    outlook.sync(db, transport=graph)
    db.execute("UPDATE plan_blocks SET outcome = 'done' WHERE id = ?", (done,))
    db.execute("UPDATE plan_blocks SET outcome = 'missed' WHERE id = ?", (missed,))
    assert outlook.sync(db, transport=graph)["deleted"] == 1
    assert [e["subject"] for e in graph.events.values()] == ["✓ To-do: Groceries"]


def test_checkin_api(db):
    todo = events.add_todo(db, {"title": "Return package", "minutes": 30})
    past = accept_task_block(db, todo["id"], "Return package", 0, 30)  # midnight: always in the past
    future = accept_task_block(db, todo["id"], "Return package", 23 * 60, 23 * 60 + 30)
    db.commit()
    with TestClient(app.app) as client:
        assert past in [b["id"] for b in client.get("/api/assistant/plan/checkin").json()["blocks"]]
        assert client.post(f"/api/assistant/plan/blocks/{future}/outcome", json={"outcome": "missed"}).status_code == 400
        assert client.post(f"/api/assistant/plan/blocks/{past}/outcome", json={"outcome": "maybe"}).status_code == 422
        result = client.post(f"/api/assistant/plan/blocks/{past}/outcome", json={"outcome": "done"}).json()
        assert result["outcome"] == "done" and past not in [b["id"] for b in result["checkin"]]
        grid = client.get(f"/api/assistant/calendar?start={TODAY.isoformat()}&days=1").json()
        assert next(i for i in grid["days"][0]["items"] if i["id"] == past)["outcome"] == "done"
