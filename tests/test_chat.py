"""Chat assistant: built-in commands, the AI tool loop (fake provider), proposals, confirm and undo."""

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
from assistant import chat, commands, events  # noqa: E402

TODAY = date.today()
NOW = datetime.combine(TODAY, datetime.min.time()).replace(hour=8)
KEY = "gsk_" + "x" * 40


def day(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


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
        rows = [("chest1", 1, "W5 · Chest, Triceps & Shoulders"), ("back1", 2, "W5 · Back & Biceps"), ("run1", 3, "W5 · Easy Run"),
                ("chest2", 4, "W5 · Chest, Triceps & Shoulders · Volume"), ("back2", 5, "W5 · Back & Biceps · Volume"), ("legs1", 6, "W5 · Legs")]
        for sid, offset, title in rows:
            kind = "run" if "Run" in title else "strength"
            connection.execute("INSERT INTO sessions (id, session_date, kind, title, minutes, status, locked, source) VALUES (?, ?, ?, ?, 60, 'planned', 0, 'plan:x')",
                               (sid, day(offset), kind, title))
        connection.commit()
        yield connection


def dates(db) -> dict[str, str]:
    return {row[0]: row[1] for row in db.execute("SELECT id, session_date FROM sessions")}


class FakeAI:
    """An OpenAI-compatible endpoint that replays scripted assistant messages."""

    def __init__(self, script=None, models=("llama-3.1-8b-instant", "openai/gpt-oss-120b", "whisper-large-v3")):
        self.script, self.models, self.requests = list(script or []), models, []

    def __call__(self, method, url, headers, body):
        assert headers["Authorization"] == f"Bearer {KEY}"
        payload = json.loads(body) if body else None
        self.requests.append((method, url, payload))
        if url.endswith("/models"):
            return 200, json.dumps({"data": [{"id": m} for m in self.models]}).encode()
        message = self.script.pop(0)
        return 200, json.dumps({"choices": [{"message": message}]}).encode()


def tool_call(name, args, call_id="c1"):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def connect(db, monkeypatch, fake):
    monkeypatch.setattr(chat, "_http", fake)
    chat.save_key(db, KEY)


# ---------------------------------------------------------------- key + provider

def test_provider_from_key_prefix_and_model_choice(db, monkeypatch):
    assert chat.provider_for("gsk_abc") == "groq" and chat.provider_for("xai-abc") == "xai" and chat.provider_for("AIzaabc") == "gemini"
    with pytest.raises(chat.ChatError):
        chat.provider_for("sk-something-else")
    assert chat.pick_model("groq", ["llama-3.1-8b-instant", "openai/gpt-oss-120b"]) == "openai/gpt-oss-120b"
    assert chat.pick_model("xai", ["grok-2-image", "grok-4-fast", "grok-3-mini"]) == "grok-4-fast"
    assert chat.pick_model("gemini", ["models/gemini-2.5-pro", "models/gemini-2.5-flash", "models/text-embedding-004"]) == "gemini-2.5-flash"
    fake = FakeAI()
    connect(db, monkeypatch, fake)
    assert chat.status(db) == {"connected": True, "provider": "Groq", "model": "openai/gpt-oss-120b"}
    assert KEY.encode() not in chat._key_path().read_bytes() or sys.platform != "win32"  # encrypted at rest on Windows
    assert chat.forget_key(db)["connected"] is False and not chat._key_path().exists()


def test_bad_key_and_rate_limit_are_readable_and_never_leak_the_key(db, monkeypatch):
    monkeypatch.setattr(chat, "_http", lambda *a: (401, b'{"error": {"message": "bad"}}'))
    with pytest.raises(chat.ChatError) as error:
        chat.save_key(db, KEY)
    assert "API key" in str(error.value) and KEY not in str(error.value)
    assert not chat._key_path().exists()
    connect(db, monkeypatch, FakeAI())
    monkeypatch.setattr(chat, "_http", lambda *a: (429, b"{}"))
    with pytest.raises(chat.ChatError) as error:
        chat.reply(db, [{"role": "user", "content": "plan my week please"}], now=NOW)
    assert "limit" in str(error.value)


# ---------------------------------------------------------------- built-in commands (no AI)

def test_day_parsing():
    monday = date(2026, 11, 23)
    assert commands.parse_day("friday", monday) == date(2026, 11, 27)
    assert commands.parse_day("monday", monday) == date(2026, 11, 30)  # next one, not today
    assert commands.parse_day("tomorrow", monday) == date(2026, 11, 24)
    assert commands.parse_day("12/5", monday) == date(2026, 12, 5)
    assert commands.parse_day("someday", monday) is None


def test_swap_command_confirm_and_undo_without_any_key(db):
    before = dates(db)
    result = chat.reply(db, [{"role": "user", "content": "Flip chest and back and bi this week and next"}], now=NOW)
    assert result["source"] == "built-in" and len(result["proposals"]) == 1
    card = result["proposals"][0]
    assert "next 1 pair" in card["summary"] and len(card["details"]) == 4
    assert dates(db) == before  # nothing changes until confirmed
    applied = chat.apply(db, card["id"])
    after = dates(db)
    assert (after["chest1"], after["back1"], after["chest2"], after["back2"]) == (day(2), day(1), day(5), day(4))
    with pytest.raises(chat.ChatError):
        chat.apply(db, card["id"])  # one use only
    chat.apply(db, applied["undo"]["id"])
    assert dates(db) == before


def test_move_push_and_todo_commands(db):
    move = chat.reply(db, [{"role": "user", "content": "move legs to " + (TODAY + timedelta(days=8)).strftime("%A")}], now=NOW)
    assert move["proposals"] and move["proposals"][0]["summary"].startswith("Move W5 · Legs")
    push = chat.reply(db, [{"role": "user", "content": "push training back 2 days"}], now=NOW)
    assert "Push training back 2 days" in push["proposals"][0]["summary"]
    todo = chat.reply(db, [{"role": "user", "content": "add to-do: call insurance, 20 min, due tomorrow"}], now=NOW)
    card = todo["proposals"][0]
    assert card["summary"].startswith("Add to-do “call insurance” (20 min, due")
    result = chat.apply(db, card["id"])
    assert [t["title"] for t in events.open_todos(db)] == ["call insurance"]
    chat.apply(db, result["undo"]["id"])
    assert events.open_todos(db) == []


def test_unmatched_text_without_a_key_explains_what_works(db):
    result = chat.reply(db, [{"role": "user", "content": "what does my week look like?"}], now=NOW)
    assert result["proposals"] == [] and "Settings" in result["reply"]
    assert commands.match(db, "move dentist to friday", NOW) is None  # not a workout: left for the AI


# ---------------------------------------------------------------- the AI path

def test_ai_proposes_an_event_and_confirm_adds_it(db, monkeypatch):
    fake = FakeAI([tool_call("add_event", {"title": "Dentist", "date": day(2), "start_time": "15:00", "end_time": "16:00"}),
                   {"role": "assistant", "content": "I set up the dentist for you to confirm."}])
    connect(db, monkeypatch, fake)
    result = chat.reply(db, [{"role": "user", "content": "dentist in two days at 3"}], now=NOW)
    assert result["source"] == "Groq" and result["reply"].startswith("I set up")
    card = result["proposals"][0]
    assert card["summary"] == f"Add “Dentist” on {chat._nice(day(2))}, 3:00 PM–4:00 PM"
    request = fake.requests[1][2]
    assert request["model"] == "openai/gpt-oss-120b" and any(t["function"]["name"] == "swap_workouts" for t in request["tools"])
    system = request["messages"][0]["content"]
    assert "W5 · Back & Biceps" in system and "[back1]" in system and "read-only" in system
    assert fake.requests[2][2]["messages"][-1]["role"] == "tool"
    assert events.occurrences(db, TODAY, TODAY + timedelta(days=7)) == []
    applied = chat.apply(db, card["id"])
    assert [o["title"] for o in events.occurrences(db, TODAY, TODAY + timedelta(days=7))] == ["Dentist"]
    chat.apply(db, applied["undo"]["id"])
    assert events.occurrences(db, TODAY, TODAY + timedelta(days=7)) == []


def test_ai_cannot_change_read_only_events_and_gets_told_why(db, monkeypatch):
    fake = FakeAI([tool_call("change_event", {"event_ref": "school:abc", "start_time": "10:00", "end_time": "11:00"}),
                   {"role": "assistant", "content": "That's a school event, so I can't move it."}])
    connect(db, monkeypatch, fake)
    result = chat.reply(db, [{"role": "user", "content": "move my lecture to 10"}], now=NOW)
    assert result["proposals"] == []
    assert "read-only" in fake.requests[2][2]["messages"][-1]["content"]


def test_ai_moves_a_personal_outlook_event_with_undo(db, monkeypatch):
    from assistant import outlook
    db.execute("INSERT INTO outlook_events (id, title, start, end, all_day, busy, location) VALUES ('long-outlook-id', 'Haircut', ?, ?, 0, 1, '')",
               (f"{day(1)}T16:00", f"{day(1)}T16:30"))
    ref = chat.alias("o", "long-outlook-id")
    edits = []
    monkeypatch.setattr(outlook, "edit_event", lambda db, event_id, changes: edits.append((event_id, changes)) or {"title": "Haircut"})
    fake = FakeAI([tool_call("change_event", {"event_ref": ref, "new_date": day(2), "start_time": "17:00", "end_time": "17:30"}),
                   {"role": "assistant", "content": "Moved to confirm."}])
    connect(db, monkeypatch, fake)
    result = chat.reply(db, [{"role": "user", "content": "push my haircut to the next day at 5"}], now=NOW)
    assert ref in fake.requests[1][2]["messages"][0]["content"]  # short alias, not the long Outlook id
    applied = chat.apply(db, result["proposals"][0]["id"])
    assert edits == [("long-outlook-id", {"date": day(2), "start": 17 * 60, "end": 17 * 60 + 30})]
    chat.apply(db, applied["undo"]["id"])
    assert edits[-1] == ("long-outlook-id", {"date": day(1), "start": 16 * 60, "end": 16 * 60 + 30, "title": "Haircut"})


# ---------------------------------------------------------------- API

def test_chat_api(db):
    with TestClient(app.app) as client:
        assert client.get("/api/chat/status").json()["connected"] is False
        assert client.post("/api/chat/key", json={"key": "not-a-real-provider-key-1234"}).status_code == 400
        result = client.post("/api/chat", json={"messages": [{"role": "user", "content": "swap chest and back"}]}).json()
        card = result["proposals"][0]
        assert client.post(f"/api/chat/apply/{card['id']}").json()["summary"] == "Moved 2 sessions."
        assert client.post(f"/api/chat/apply/{card['id']}").status_code == 400
        assert client.post("/api/chat", json={"messages": [{"role": "assistant", "content": "hi"}]}).status_code == 400
