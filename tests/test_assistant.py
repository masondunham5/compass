"""Assistant layer: ICS parsing (Outlook-style), feeds, availability, exam alerts, reschedule heads-ups."""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import core  # noqa: E402
from assistant import availability, calendars  # noqa: E402

TODAY = date.today()
MONDAY = TODAY - timedelta(days=TODAY.weekday())


def ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


def outlook_ics() -> bytes:
    """Floating local times (as Outlook publishes with its own VTIMEZONE) — a weekly class, a skipped week, a free
    block, an all-day exam, and a cancelled meeting."""
    first = MONDAY - timedelta(days=7)
    exam_day = TODAY + timedelta(days=1)
    skipped = MONDAY + timedelta(days=7)
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Microsoft Corporation//Outlook 16.0 MIMEDIR//EN",
        "BEGIN:VEVENT", "UID:class-1", "SUMMARY:BIOL 201 Lecture", "LOCATION:Science Hall 104",
        f"DTSTART:{ymd(first)}T090000", f"DTEND:{ymd(first)}T105000",
        "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR", f"EXDATE:{ymd(skipped)}T090000", "END:VEVENT",
        "BEGIN:VEVENT", "UID:office-hours", "SUMMARY:Office hours (optional)", "TRANSP:TRANSPARENT",
        f"DTSTART:{ymd(TODAY)}T130000", f"DTEND:{ymd(TODAY)}T140000", "END:VEVENT",
        "BEGIN:VEVENT", "UID:exam-1", "SUMMARY:CHEM 110 Midterm Exam",
        f"DTSTART;VALUE=DATE:{ymd(exam_day)}", f"DTEND;VALUE=DATE:{ymd(exam_day + timedelta(days=1))}", "END:VEVENT",
        "BEGIN:VEVENT", "UID:cancelled", "SUMMARY:Cancelled meeting", "STATUS:CANCELLED",
        f"DTSTART:{ymd(TODAY)}T150000", f"DTEND:{ymd(TODAY)}T160000", "END:VEVENT",
        "END:VCALENDAR",
    ]
    return ("\r\n".join(lines) + "\r\n").encode()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    monkeypatch.setattr(core, "DATABASE", tmp_path / "test.db")
    monkeypatch.setattr(core, "LEGACY_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(core, "BACKUP_DIR", tmp_path / "backups")
    core.initialize()
    with TestClient(app.app) as test_client:
        yield test_client


def test_parse_expands_recurrence_and_respects_exdate_transp_cancelled():
    events = calendars.parse_events(outlook_ics(), TODAY)
    lectures = [e for e in events if e["title"] == "BIOL 201 Lecture"]
    skipped = (MONDAY + timedelta(days=7)).isoformat()
    assert lectures and not any(e["start"].startswith(skipped) for e in lectures)
    assert all(e["start"].endswith("T09:00") and e["end"].endswith("T10:50") for e in lectures)
    assert not any(e["title"] == "Cancelled meeting" for e in events)
    office = next(e for e in events if e["uid"] == "office-hours")
    assert office["busy"] is False
    exam = next(e for e in events if e["uid"] == "exam-1")
    assert exam["all_day"] is True
    with pytest.raises(calendars.CalendarError):
        calendars.parse_events(b"not a calendar", TODAY)


def test_outlook_windows_timezone_names_parse():
    ics = "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "BEGIN:VTIMEZONE", "TZID:Eastern Standard Time",
        "BEGIN:STANDARD", "DTSTART:16010101T020000", "TZOFFSETFROM:-0400", "TZOFFSETTO:-0500", "RRULE:FREQ=YEARLY;BYDAY=1SU;BYMONTH=11", "END:STANDARD",
        "BEGIN:DAYLIGHT", "DTSTART:16010101T020000", "TZOFFSETFROM:-0500", "TZOFFSETTO:-0400", "RRULE:FREQ=YEARLY;BYDAY=2SU;BYMONTH=3", "END:DAYLIGHT",
        "END:VTIMEZONE",
        "BEGIN:VEVENT", "UID:tz-1", "SUMMARY:Lab",
        f"DTSTART;TZID=Eastern Standard Time:{ymd(TODAY)}T140000", f"DTEND;TZID=Eastern Standard Time:{ymd(TODAY)}T153000", "END:VEVENT",
        "END:VCALENDAR", "",
    ]).encode()
    lab = calendars.parse_events(ics, TODAY)[0]
    from datetime import datetime
    length = datetime.fromisoformat(lab["end"]) - datetime.fromisoformat(lab["start"])
    assert length == timedelta(minutes=90) and lab["all_day"] is False


def test_url_is_normalized_masked_and_never_returned(client, monkeypatch):
    secret = "webcal://outlook.office365.com/owa/calendar/abc123secret/reachable/calendar.ics"
    fetched = []
    monkeypatch.setattr(calendars, "fetch", lambda url: fetched.append(url) or outlook_ics())
    response = client.post("/api/assistant/calendars", json={"name": "School", "url": secret, "kind": "school"})
    assert response.status_code == 200
    assert fetched == ["https://outlook.office365.com/owa/calendar/abc123secret/reachable/calendar.ics"]
    assert "abc123secret" not in response.text
    feed = response.json()["calendar"]
    assert feed["url_hint"] == "outlook.office365.com/…/calendar.ics" and feed["event_count"] > 5 and feed["fixed"] is True
    assert "abc123secret" not in client.get("/api/assistant/calendars").text
    assert client.post("/api/assistant/calendars", json={"name": "Again", "url": secret}).status_code == 400  # duplicate
    assert client.post("/api/assistant/calendars", json={"name": "Bad", "url": "ftp://example.com/x.ics"}).status_code == 400


def test_fetch_errors_never_include_the_url(monkeypatch):
    import urllib.error

    def refuse(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(calendars.urllib.request, "urlopen", refuse)
    with pytest.raises(calendars.CalendarError) as error:
        calendars.fetch("https://example.com/private/secret-token/calendar.ics")
    assert "secret-token" not in str(error.value) and "403" in str(error.value)


def test_upload_agenda_free_slots_and_exam_alerts(client):
    upload = client.post("/api/assistant/calendars/upload", files={"file": ("school.ics", outlook_ics(), "text/calendar")}, data={"name": "School", "kind": "school"})
    assert upload.status_code == 200 and upload.json()["calendar"]["source"] == "file"
    monday = client.get(f"/api/assistant/agenda?day={MONDAY.isoformat()}").json()
    assert monday["busy_minutes"] == 110
    assert monday["free_slots"][0]["start"] == "6:00 AM" and monday["free_slots"][0]["end"] == "9:00 AM"
    assert monday["best_slot"]["start"] == "3:00 PM"  # prefers late afternoon
    exam_day = TODAY + timedelta(days=1)
    with core.connection() as db:
        db.execute("INSERT INTO sessions (id, session_date, kind, title, minutes, status) VALUES ('lift', ?, 'strength', 'W1 · Legs', 60, 'planned')", (exam_day.isoformat(),))
        alerts = availability.exam_alerts(db)
    assert alerts[0]["kind"] == "exam" and alerts[0]["level"] == "warn" and "Legs" in alerts[0]["message"]
    assert len(alerts) == 1  # an all-day event ending at midnight must not spill into the next day
    day_after = client.get(f"/api/assistant/agenda?day={(exam_day + timedelta(days=1)).isoformat()}").json()
    assert not day_after["exams"]
    week = client.get(f"/api/assistant/week?start={MONDAY.isoformat()}").json()["days"]
    assert len(week) == 7 and week[0]["event_count"] >= 1
    # Rescheduling onto the exam day carries a heads-up.
    with core.connection() as db:
        db.execute("INSERT INTO sessions (id, session_date, kind, title, minutes, status) VALUES ('move-me', ?, 'strength', 'Push', 60, 'planned')", ((TODAY + timedelta(days=5)).isoformat(),))
    preview = client.post("/api/schedule/preview", json={"action": "move", "session_id": "move-me", "to_date": exam_day.isoformat()}).json()
    assert preview["busy_days"] and preview["busy_days"][0]["exams"] == ["CHEM 110 Midterm Exam"]
    assert client.post(f"/api/assistant/calendars/{upload.json()['calendar']['id']}/sync").status_code == 400  # uploads don't refresh
    assert client.delete(f"/api/assistant/calendars/{upload.json()['calendar']['id']}").json()["calendars"] == []


def test_sync_stale_records_errors_without_raising(client, monkeypatch):
    monkeypatch.setattr(calendars, "fetch", lambda url: outlook_ics())
    client.post("/api/assistant/calendars", json={"name": "School", "url": "https://example.com/c.ics"})
    def boom(url):
        raise calendars.CalendarError("Couldn't reach the calendar server.")
    monkeypatch.setattr(calendars, "fetch", boom)
    with core.connection() as db:
        db.execute("UPDATE calendar_feeds SET last_synced = '2000-01-01T00:00:00+00:00'")
        assert calendars.sync_stale(db) == 0
        feed = calendars.list_feeds(db)[0]
    assert feed["last_error"] == "Couldn't reach the calendar server." and feed["event_count"] > 0  # old events kept
