"""Places, travel time between them, travel in the plan, and all-day events that block the day."""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import core  # noqa: E402
from assistant import availability, events, places, planner, work  # noqa: E402

TODAY = date.today()
TOMORROW = TODAY + timedelta(days=1)
NOW = datetime.combine(TODAY, datetime.min.time()).replace(hour=6)


def at(hour: int, minute: int = 0) -> int:
    return hour * 60 + minute


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    monkeypatch.setattr(core, "DATABASE", tmp_path / "test.db")
    monkeypatch.setattr(core, "LEGACY_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(core, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setenv("SCHOOL_TODO_APP", str(tmp_path / "no_school" / "app.py"))
    monkeypatch.setenv("SCHOOL_TODO_DATA", str(tmp_path / "no_school"))
    core.initialize()
    with core.connection() as connection:
        connection.execute("DELETE FROM sessions")
        connection.commit()
        yield connection


@pytest.fixture()
def campus(db):
    home = places.add_place(db, {"name": "Home", "kind": "home", "address": "100 Main St, East Lansing, MI 48823"})
    school = places.add_place(db, {"name": "Campus", "kind": "campus", "address": "220 Trowbridge Rd, East Lansing, MI 48824",
                                   "keywords": "Bessey, BCC, McDonel, Business College", "extra": 10})
    job = places.add_place(db, {"name": "Work", "kind": "work", "address": "1 Job Ave, Lansing, MI"})
    places.set_travel(db, home["id"], school["id"], 15)   # + 10 extra arriving at campus = 25
    places.set_travel(db, school["id"], job["id"], 20)
    places.set_travel(db, home["id"], job["id"], 25)
    return {"home": home, "campus": school, "work": job}


def feed_event(db, title, day, start, end, location="", all_day=False, busy=True):
    if not db.execute("SELECT 1 FROM calendar_feeds WHERE id = 'f1'").fetchone():
        db.execute("INSERT INTO calendar_feeds (id, name, kind, source, created_at) VALUES ('f1', 'School', 'school', 'file', 't')")
    s = f"{day.isoformat()}T00:00" if all_day else f"{day.isoformat()}T{start // 60:02d}:{start % 60:02d}"
    e = f"{(day + timedelta(days=1)).isoformat()}T00:00" if all_day else f"{day.isoformat()}T{end // 60:02d}:{end % 60:02d}"
    db.execute("INSERT INTO calendar_events (feed_id, uid, title, start, end, all_day, busy, location) VALUES ('f1', ?, ?, ?, ?, ?, ?, ?)",
               (f"{title}{s}", title, s, e, int(all_day), int(busy), location))


# ---------------------------------------------------------------- places

def test_locations_match_places_by_keyword(db, campus):
    plist = places.list_places(db)
    assert places.resolve("Bessey Hall 108", plist)["name"] == "Campus"
    assert places.resolve("BCC N17", plist)["name"] == "Campus"
    assert places.resolve("Microsoft Teams Meeting", plist) == "virtual"
    assert places.resolve("Somewhere unknown", plist) is None
    assert places.resolve("", plist) is None
    assert places.resolve("100 Main St", plist)["name"] == "Home"  # the street part of the address counts too
    with pytest.raises(places.PlaceError):
        places.add_place(db, {"name": "Second home", "kind": "home"})


def test_travel_minutes_include_arrival_extra(db, campus):
    home, school = campus["home"], campus["campus"]
    assert places.travel_minutes(db, home, school) == 25 and places.travel_minutes(db, school, home) == 15
    assert places.travel_minutes(db, home, home) == 0 and places.travel_minutes(db, None, school) == 0


def test_estimate_uses_the_map_but_keeps_your_numbers(db, campus):
    calls = []

    def fake(url):
        calls.append(url)
        if "nominatim" in url:
            return [{"lat": "42.7", "lon": "-84.5"}]
        return {"routes": [{"duration": 1260}]}  # 21 min -> 25
    places.set_travel(db, campus["home"]["id"], campus["work"]["id"], None)  # forget one manual number
    result = places.estimate(db, transport=fake, pause=0)
    assert result["estimated"] == 1 and result["problems"] == []
    by_pair = {(t["a_name"], t["b_name"]): (t["minutes"], t["manual"]) for t in places.travel_list(db)}
    assert by_pair[("Home", "Work")] == (25, False)
    assert by_pair[("Home", "Campus")] == (15, True)  # yours, untouched
    assert all("routed-car" in u for u in calls if "nominatim" not in u)


# ---------------------------------------------------------------- a day's travel

def test_day_travel_strips_warnings_and_your_own_drive_events(db, campus):
    feed_event(db, "CSE 231", TOMORROW, at(10), at(10, 50), "Bessey Hall 108")
    feed_event(db, "ITM 210", TOMORROW, at(11), at(12, 50), "BCC N17")           # same place: no travel
    feed_event(db, "Lunch call", TOMORROW, at(13, 30), at(14), "Microsoft Teams Meeting")  # online: no travel
    work.add_shift(db, "13:00", "17:00", shift_date=TOMORROW.isoformat())
    result = places.day_travel(db, TOMORROW, availability.fixed_events(db, TOMORROW))
    strips = [(s["start"], s["end"], s["from"], s["to"]) for s in result["strips"]]
    assert strips[0] == (at(9, 35), at(10), "Home", "Campus")
    assert strips[1] == (at(12, 50), at(13), "Campus", "Work")            # only 10 of the 20 minutes fit…
    assert strips[2] == (at(17), at(17, 25), "Work", "Home")
    assert len(result["warnings"]) == 1 and "about 20 min apart, you have 10" in result["warnings"][0]["message"]  # …so you're warned

    feed_event(db, "Drive to campus", TOMORROW, at(9, 30), at(10))  # you planned the drive yourself
    result = places.day_travel(db, TOMORROW, availability.fixed_events(db, TOMORROW))
    assert (at(9, 35), at(10), "Home", "Campus") not in [(s["start"], s["end"], s["from"], s["to"]) for s in result["strips"]]


def test_planner_keeps_travel_time_free_and_pads_gym_workouts(db, campus):
    feed_event(db, "CSE 231", TOMORROW, at(10), at(10, 50), "Bessey Hall 108")
    gym = places.add_place(db, {"name": "IM East", "kind": "gym", "address": "IM East, East Lansing", "keywords": "IM East"})
    places.set_travel(db, campus["home"]["id"], gym["id"], 10)
    places._set(db, "lift_place", gym["id"])
    db.execute("INSERT INTO sessions (id, session_date, kind, title, minutes, status, locked, source) VALUES ('lift', ?, 'strength', 'Chest', 60, 'planned', 0, 'x')",
               (TOMORROW.isoformat(),))
    events.add_todo(db, {"title": "Email advisor", "minutes": 30, "due_date": TOMORROW.isoformat(), "due_time": "18:00"})
    plan = planner.build(db, TOMORROW, 1, now=NOW)
    blocks = {b["kind"]: b for b in plan["days"][0]["blocks"]}
    task = blocks["task"]  # 9:00 would run into the drive to campus (9:35, plus a 15-min buffer), so it goes after class
    assert task["start"] >= at(10, 50) or task["end"] <= at(9, 20)
    lift = blocks["workout"]
    assert lift["start"] == at(15)                   # the workout itself still starts at 3 PM; you leave at 2:50
    db.commit()
    with TestClient(app.app) as client:
        items = client.get(f"/api/assistant/calendar?start={TOMORROW.isoformat()}&days=1").json()["days"][0]["items"]
        travel = [(i["start"], i["end"]) for i in items if i["source"] == "travel"]
        assert (at(9, 35), at(10)) in travel and (at(14, 50), at(15)) in travel and (at(16), at(16, 10)) in travel
        lecture = next(i for i in items if i["title"] == "CSE 231")
        assert lecture["place"] == "Campus" and lecture["directions"] == "Bessey Hall 108, East Lansing, MI 48824"


# ---------------------------------------------------------------- all-day events

def test_busy_all_day_events_block_the_day_and_can_be_switched(db):
    feed_event(db, "Christmas at Steve's", TOMORROW, 0, 0, all_day=True, busy=False)
    feed_event(db, "Pay rent", TOMORROW, 0, 0, all_day=True, busy=False)
    events.add_todo(db, {"title": "Laundry", "minutes": 60, "due_date": (TOMORROW + timedelta(days=1)).isoformat()})

    def todo_days():
        return {b["block_date"] for d in planner.build(db, TOMORROW, 2, now=NOW)["days"] for b in d["blocks"] if b["kind"] == "task"}
    assert todo_days() == {TOMORROW.isoformat()}  # a free all-day event is just a label

    key = next(e["allday_key"] for e in availability.fixed_events(db, TOMORROW) if e["title"].startswith("Christmas"))
    db.commit()
    with TestClient(app.app) as client:
        assert client.post("/api/assistant/allday", json={"key": key, "busy": True}).json()["busy"] is True
        item = next(i for i in client.get(f"/api/assistant/calendar?start={TOMORROW.isoformat()}&days=1").json()["days"][0]["items"] if i["title"].startswith("Christmas"))
        assert item["blocked_here"] and item["busy"] and item["allday_key"] == key
    assert todo_days() == {(TOMORROW + timedelta(days=1)).isoformat()}  # the whole day is kept clear
    assert availability.day_summary(db, TOMORROW)["busy_minutes"] == availability.DAY_END - availability.DAY_START
    availability.set_day_override(db, key, None)
    assert todo_days() == {TOMORROW.isoformat()}


def test_places_api(db):
    with TestClient(app.app) as client:
        data = client.post("/api/assistant/places", json={"name": "Home", "kind": "home", "address": "1 A St, Town"}).json()
        home_id = data["places"][0]["id"]
        data = client.post("/api/assistant/places", json={"name": "Campus", "kind": "campus", "keywords": "Bessey"}).json()
        campus_id = next(p["id"] for p in data["places"] if p["name"] == "Campus")
        assert client.post("/api/assistant/places", json={"name": "", "kind": "home"}).status_code == 400
        data = client.put("/api/assistant/places/travel", json={"a": campus_id, "b": home_id, "minutes": 18}).json()
        assert data["travel"][0]["minutes"] == 18 and data["travel"][0]["manual"]
        assert client.put("/api/assistant/places/settings", json={"mode": "walk", "lift_place": campus_id}).json()["mode"] == "walk"
        assert client.put("/api/assistant/places/settings", json={"lift_place": "nope"}).status_code == 400
        data = client.delete(f"/api/assistant/places/{campus_id}").json()
        assert [p["name"] for p in data["places"]] == ["Home"] and data["lift_place"] == "" and data["travel"] == []
