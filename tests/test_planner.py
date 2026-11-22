"""School To Do adapter (read-only), work schedule, and the draft/accept planner."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import core  # noqa: E402
from assistant import planner, school, work  # noqa: E402

TODAY = date.today()
NOW = datetime.combine(TODAY, datetime.min.time()).replace(hour=8)


def d(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


@pytest.fixture()
def school_todo(tmp_path, monkeypatch):
    """A fake School To Do: app.py with RAW + a prep loop, plus its data folder."""
    app_dir, data_dir = tmp_path / "school_todo", tmp_path / "School To Do"
    app_dir.mkdir(); data_dir.mkdir()
    first_sunday = TODAY - timedelta(days=(TODAY.weekday() + 1) % 7)
    (app_dir / "app.py").write_text(f'''import tkinter as tk  # never imported by the adapter
from datetime import date, timedelta
RAW = """
{d(-20)}|CSE 231|assignment|Old Project
{d(1)}|CSE 231|lab|Lab 11
{d(2)}|CSE 260|quiz|Quiz 12
{d(4)}|ITM 210|assignment|Big Report
{d(3)}|CHEM 110|exam|Midterm Exam
{d(5)}|CSE 231|assignment|Edited Away
"""
ITEMS = [tuple(line.split("|", 3)) for line in RAW.strip().splitlines()]
for sunday in (date({first_sunday.year}, {first_sunday.month}, {first_sunday.day}) + timedelta(days=7*n) for n in range(2)):
    ITEMS.append((sunday.isoformat(), "CSE 260", "prep", "Weekly prep"))
ESTIMATE_MINUTES = {{"assignment": 120, "quiz": 45, "exam": 0, "lab": 120, "exercise": 60, "prep": 60}}
raise SystemExit("the adapter must not execute this")
''', encoding="utf-8")
    (data_dir / "completed.json").write_text(json.dumps([f"{d(2)}|CSE 260|quiz|Quiz 12"]), encoding="utf-8")
    (data_dir / "manual_tasks.json").write_text(json.dumps([[d(6), "ISS 310-V", "quiz", "Imported Quiz"]]), encoding="utf-8")
    (data_dir / "task_overrides.json").write_text(json.dumps({"edits": {f"{d(4)}|ITM 210|assignment|Big Report": [d(4), "ITM 210", "assignment", "Big Report (final)"]},
                                                              "hidden": [f"{d(5)}|CSE 231|assignment|Edited Away"]}), encoding="utf-8")
    (data_dir / "task_details.json").write_text(json.dumps({f"{d(4)}|ITM 210|assignment|Big Report": {"minutes": 300, "notes": "long"},
                                                            f"{d(1)}|CSE 231|lab|Lab 11": {"time": "2:40 PM"}}), encoding="utf-8")
    monkeypatch.setenv("SCHOOL_TODO_APP", str(app_dir / "app.py"))
    monkeypatch.setenv("SCHOOL_TODO_DATA", str(data_dir))
    return data_dir


@pytest.fixture()
def db(tmp_path, monkeypatch, school_todo):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    monkeypatch.setattr(core, "DATABASE", tmp_path / "test.db")
    monkeypatch.setattr(core, "LEGACY_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(core, "BACKUP_DIR", tmp_path / "backups")
    core.initialize()
    with core.connection() as connection:
        yield connection


def test_adapter_reads_school_todo_without_running_it(school_todo):
    before = {p.name: p.read_bytes() for p in school_todo.iterdir()}
    tasks = {task.title: task for task in school.load_tasks()}
    assert "Edited Away" not in tasks and "Big Report" not in tasks  # hidden / edited
    report = tasks["Big Report (final)"]
    assert report.minutes == 300 and report.key == f"{d(4)}|ITM 210|assignment|Big Report"
    assert tasks["Quiz 12"].done is True and tasks["Lab 11"].due_time == "14:40"
    assert tasks["Imported Quiz"].minutes == 45 and tasks["Imported Quiz"].due_time == "23:59"
    assert sum(1 for t in school.load_tasks() if t.kind == "prep") == 2
    assert {p.name: p.read_bytes() for p in school_todo.iterdir()} == before  # read-only


def test_work_shifts_follow_terms(db):
    tuesday = TODAY + timedelta(days=(1 - TODAY.weekday()) % 7)
    work.add_shift(db, "12:00", "17:00", weekday=1)
    work.add_shift(db, "09:00", "13:00", weekday=1, applies="break")
    assert [s["start"] for s in work.shifts_on(db, tuesday)] == ["12:00"]  # no terms -> school
    work.add_term(db, "Winter break", "break", tuesday.isoformat(), (tuesday + timedelta(days=20)).isoformat())
    assert [s["start"] for s in work.shifts_on(db, tuesday)] == ["09:00"]
    work.add_shift(db, "18:00", "22:00", shift_date=tuesday.isoformat())
    assert len(work.shifts_on(db, tuesday)) == 2
    with pytest.raises(work.WorkError):
        work.add_shift(db, "17:00", "12:00", weekday=2)
    with pytest.raises(work.WorkError):
        work.add_shift(db, "12:00", "13:00")


def test_planner_fits_work_around_fixed_time(db):
    db.execute("INSERT INTO sessions (id, session_date, kind, title, minutes, status) VALUES ('lift', ?, 'strength', 'W5 · Legs', 60, 'planned')", (d(1),))
    work.add_shift(db, "12:00", "17:00", weekday=(TODAY + timedelta(days=1)).weekday())
    plan = planner.build(db, TODAY, 7, now=NOW)
    blocks = [b for day in plan["days"] for b in day["blocks"]]
    workout = next(b for b in blocks if b["kind"] == "workout")
    assert workout["block_date"] == d(1) and workout["start"] >= 17 * 60  # after the shift, in the afternoon window
    for b in blocks:  # nothing overlaps the work shift
        if b["block_date"] == d(1):
            assert b["end"] <= 12 * 60 or b["start"] >= 17 * 60
    lab = [b for b in blocks if b["title"] == "CSE 231: Lab 11"]
    assert sum(b["end"] - b["start"] for b in lab) == 120 and all(b["block_date"] <= d(1) for b in lab)
    assert all(b["block_date"] < d(1) or b["end"] <= 14 * 60 + 40 for b in lab)  # due 2:40 PM on its day
    report = [b for b in blocks if "Big Report" in b["title"]]
    assert sum(b["end"] - b["start"] for b in report) == 300 and all(b["block_date"] < d(4) for b in report)  # a day early
    assert all(b["end"] - b["start"] <= 90 and b["start"] >= 9 * 60 for b in blocks if b["kind"] == "study")
    reviews = [b for b in blocks if b["kind"] == "review"]
    assert len(reviews) == 2 and all(b["block_date"] < d(3) for b in reviews)
    for day in plan["days"]:
        assert day["study_minutes"] <= planner.SETTINGS["daily_study_cap"]
    assert any("overdue" in w for w in plan["warnings"]) and not any("Quiz 12" in b["title"] for b in blocks)  # done


def test_future_window_ignores_work_due_before_it(db):
    later = planner.build(db, TODAY + timedelta(days=2), 7, now=NOW)
    titles = {b["title"] for day in later["days"] for b in day["blocks"]}
    assert "CSE 231: Lab 11" not in titles  # due tomorrow, before this window
    assert not any("Lab 11" in w for w in later["warnings"])


def test_accepting_makes_blocks_fixed_and_forced_overdue(db):
    plan = planner.build(db, TODAY, 7, now=NOW)
    drafts = [b for day in plan["days"] for b in day["blocks"] if b["status"] == "draft"]
    lab_blocks = [b for b in drafts if b["title"] == "CSE 231: Lab 11"]
    assert planner.accept(db, lab_blocks) == len(lab_blocks)
    assert planner.accept(db, lab_blocks) == 0  # idempotent
    again = planner.build(db, TODAY, 7, now=NOW)
    labs = [b for day in again["days"] for b in day["blocks"] if b["title"] == "CSE 231: Lab 11"]
    assert labs and all(b["status"] == "accepted" for b in labs)  # nothing new drafted for it
    old_key = f"{d(-20)}|CSE 231|assignment|Old Project"
    planner.set_forced(db, old_key, True)
    forced = planner.build(db, TODAY, 7, now=NOW)
    old = [b for day in forced["days"] for b in day["blocks"] if b["title"] == "CSE 231: Old Project"]
    assert sum(b["end"] - b["start"] for b in old) == 120 and all(b["block_date"] <= d(1) for b in old)
    assert planner.unaccept(db, from_date=TODAY.isoformat()) == len(lab_blocks)


def test_planner_api_and_agenda_include_work_and_plan(db, monkeypatch):
    with TestClient(app.app) as client:
        assert client.get("/api/assistant/school").json()["status"]["connected"]
        client.post("/api/assistant/work/shifts", json={"start": "13:00", "end": "17:00", "weekday": TODAY.weekday()})
        assert client.post("/api/assistant/work/shifts", json={"start": "9", "end": "10", "weekday": 1}).status_code == 400
        plan = client.get("/api/assistant/plan?days=7").json()
        today_blocks = [b for b in plan["days"][0]["blocks"] if b["status"] == "draft"]
        if today_blocks:
            assert client.post("/api/assistant/plan/accept", json={"blocks": today_blocks[:1]}).json()["accepted"] == 1
        agenda = client.get(f"/api/assistant/agenda?day={d(0)}").json()
        assert any(e["feed_kind"] == "work" for e in agenda["events"]) and agenda["has_calendar"]
        assert client.post("/api/assistant/plan/accept", json={"blocks": [{"id": "x"}]}).status_code == 400
        forced = client.post("/api/assistant/school/force", json={"key": f"{d(-20)}|CSE 231|assignment|Old Project"}).json()
        assert forced["forced"]
