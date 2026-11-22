"""Outlook write-back against a fake Microsoft Graph (no network, no real sign-in)."""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import core  # noqa: E402
from assistant import outlook  # noqa: E402

TODAY = date.today()
# Microsoft Graph's calendarColor enum (there is no purple).
GRAPH_CALENDAR_COLORS = {"auto", "lightBlue", "lightGreen", "lightOrange", "lightGray", "lightYellow",
                         "lightTeal", "lightPink", "lightBrown", "lightRed", "maxColor"}


class FakeGraph:
    """Just enough of Graph's calendar API: calendars, events, 404s."""

    def __init__(self):
        self.calendars: dict[str, str] = {}
        self.events: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.counter = 0
        self.default_calendar: list[dict] = []  # your normal Outlook calendar (read-only for Compass)

    def __call__(self, method, url, headers, body):
        assert headers["Authorization"] == "Bearer test-token"
        path = url.split("/v1.0", 1)[1].split("?", 1)[0]
        self.calls.append((method, path))
        payload = json.loads(body) if body else None
        parts = path.strip("/").split("/")
        if parts == ["me", "calendarView"] and method == "GET":
            return 200, json.dumps({"value": self.default_calendar}).encode()
        if parts[:2] == ["me", "calendars"]:
            if len(parts) == 2 and method == "GET":
                return 200, json.dumps({"value": [{"id": i, "name": n} for i, n in self.calendars.items()]}).encode()
            if len(parts) == 2 and method == "POST":
                if payload.get("color", "auto") not in GRAPH_CALENDAR_COLORS:  # Graph rejects unknown enum values
                    return 400, json.dumps({"error": {"message": f"Requested value '{payload['color']}' was not found."}}).encode()
                self.counter += 1
                cid = f"cal{self.counter}"
                self.calendars[cid] = payload["name"]
                return 201, json.dumps({"id": cid}).encode()
            if len(parts) == 3:
                return (200, b'{"id": "x"}') if parts[2] in self.calendars else (404, b"")
            if len(parts) == 4 and parts[3] == "events" and method == "POST":
                self.counter += 1
                eid = f"ev{self.counter}"
                self.events[eid] = {**payload, "calendar": parts[2]}
                return 201, json.dumps({"id": eid}).encode()
        if parts[:2] == ["me", "events"] and len(parts) == 3:
            eid = parts[2]
            if eid not in self.events:
                return 404, b""
            if method == "PATCH":
                self.events[eid].update(payload)
                return 200, b"{}"
            if method == "DELETE":
                del self.events[eid]
                return 204, b""
        return 400, json.dumps({"error": {"message": f"unexpected {method} {path}"}}).encode()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    monkeypatch.setattr(core, "DATABASE", tmp_path / "test.db")
    monkeypatch.setattr(core, "LEGACY_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(core, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(outlook, "_access_token", lambda db: "test-token")
    core.initialize()
    with core.connection() as connection:
        outlook._set(connection, "outlook_client_id", "00000000-0000-0000-0000-000000000000")
        connection.commit()  # visible to the app's own connections
        yield connection


def add_block(db, block_id, offset, start, end, kind="study", title="CSE 231: Project 07"):
    db.execute("INSERT INTO plan_blocks (id, block_date, start, end, kind, ref, title, course, accepted_at) VALUES (?, ?, ?, ?, ?, 'ref', ?, '', 't')",
               (block_id, (TODAY + timedelta(days=offset)).isoformat(), start, end, kind, title))


def test_sync_creates_calendar_and_events_then_updates_and_deletes(db):
    graph = FakeGraph()
    add_block(db, "b1", 1, 9 * 60, 10 * 60 + 30)
    add_block(db, "b2", 2, 17 * 60, 18 * 60, kind="workout", title="Legs")
    add_block(db, "far", 40, 9 * 60, 10 * 60)  # outside the 3-week window
    first = outlook.sync(db, transport=graph)
    assert first == {"created": 2, "updated": 0, "deleted": 0, "unchanged": 0, "read": 0}
    assert list(graph.calendars.values()) == ["Compass"]
    subjects = sorted(e["subject"] for e in graph.events.values())
    assert subjects == ["Study: CSE 231: Project 07", "Workout: Legs"]
    assert all(e["calendar"] == "cal1" and e["start"]["timeZone"] == "UTC" for e in graph.events.values())

    assert outlook.sync(db, transport=graph) == {"created": 0, "updated": 0, "deleted": 0, "unchanged": 2, "read": 0}
    db.execute("UPDATE plan_blocks SET start = ?, end = ? WHERE id = 'b1'", (11 * 60, 12 * 60))
    db.execute("DELETE FROM plan_blocks WHERE id = 'b2'")
    assert outlook.sync(db, transport=graph) == {"created": 0, "updated": 1, "deleted": 1, "unchanged": 0, "read": 0}
    assert len(graph.events) == 1


def test_calendar_deleted_in_outlook_is_recreated_and_events_reposted(db):
    graph = FakeGraph()
    add_block(db, "b1", 1, 9 * 60, 10 * 60)
    outlook.sync(db, transport=graph)
    graph.calendars.clear(); graph.events.clear()  # you deleted the Compass calendar in Outlook
    result = outlook.sync(db, transport=graph)
    assert result["created"] == 1 and list(graph.calendars.values()) == ["Compass"]


def test_event_edited_away_in_outlook_is_recreated(db):
    graph = FakeGraph()
    add_block(db, "b1", 1, 9 * 60, 10 * 60)
    outlook.sync(db, transport=graph)
    graph.events.clear()  # event deleted in Outlook
    db.execute("UPDATE plan_blocks SET end = ? WHERE id = 'b1'", (10 * 60 + 30,))
    assert outlook.sync(db, transport=graph)["created"] == 1


def test_only_the_compass_calendar_is_touched(db):
    graph = FakeGraph()
    graph.calendars["personal"] = "Calendar"
    add_block(db, "b1", 1, 9 * 60, 10 * 60)
    outlook.sync(db, transport=graph)
    assert all(e["calendar"] != "personal" for e in graph.events.values())
    assert not any(method in ("PATCH", "DELETE") and "personal" in path for method, path in graph.calls)


def test_errors_are_readable_and_never_contain_the_token(db):
    def refuse(method, url, headers, body):
        return 401, b'{"error": {"message": "token test-token expired"}}'
    add_block(db, "b1", 1, 9 * 60, 10 * 60)
    with pytest.raises(outlook.OutlookError) as error:
        outlook.sync(db, transport=refuse)
    assert "test-token" not in str(error.value) and "Calendars.ReadWrite" in str(error.value)


def test_token_cache_is_encrypted_at_rest(db, tmp_path):
    secret = b'{"AccessToken": {"x": {"secret": "abc123"}}}'
    protected = outlook._dpapi(secret, protect=True)
    if sys.platform == "win32":
        assert b"abc123" not in protected
    assert outlook._dpapi(protected, protect=False) == secret


def test_api_validation_status_and_disconnect(db):
    with TestClient(app.app) as client:
        assert client.post("/api/assistant/outlook/connect", json={"client_id": "not-a-guid-but-long-enough-xxxxxxxxx"}).status_code == 400
        assert client.get("/api/assistant/outlook").json()["connected"] is False  # no token file yet
        outlook._token_path().write_bytes(outlook._dpapi(b"{}", protect=True))
        assert client.get("/api/assistant/outlook").json()["connected"] is True
        assert client.post("/api/assistant/outlook/disconnect").json()["connected"] is False
        assert not outlook._token_path().exists()
