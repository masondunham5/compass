"""Two-way link with your personal Outlook calendar (the default "Calendar").

- Sign-in uses Microsoft's device-code flow (MSAL): Compass shows a short code, you enter it at
  microsoft.com/devicelogin and sign in there. Compass never sees your password.
- Needs an app registration's *client ID* (public, not a secret) with delegated
  Calendars.ReadWrite + User.Read and "Allow public client flows" enabled.
- The token cache is stored in data/outlook_token.bin, encrypted with Windows DPAPI (only this
  Windows user on this computer can read it). data/ is never committed.
- Writing: accepted plan blocks and your own Compass events go into your main Calendar, tagged
  with the "Compass" category and remembered in `outlook_links`. Compass only rewrites or deletes
  events it linked: ones that no longer exist in Compass are deleted; edits made to them in Outlook
  are overwritten on the next sync. (Older versions used a separate "Compass" calendar; the first
  sync moves those events into the main Calendar.)
- Reading: your other events in the main Calendar (things you add on your phone) are cached in
  `outlook_events` and count as busy time. Compass changes one only when you tell it to: editing,
  deleting, or accepting a planner suggestion to move it (`edit_event` / `delete_event`).
- School calendars are ICS feeds (calendars.py) and are read-only; Compass never writes to them.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import core

GRAPH = "https://graph.microsoft.com/v1.0"
AUTHORITY = "https://login.microsoftonline.com/consumers"
SCOPES = ["Calendars.ReadWrite", "User.Read"]
CATEGORY = "Compass"       # tag on every event Compass writes
TARGET = "main"            # outlook_target setting: where linked events live
MOVABLE_MAX = 4 * 60       # personal events longer than this aren't suggested for moving
WINDOW_BACK, WINDOW_AHEAD = 1, 21
READ_AHEAD = 60  # days of your Outlook calendar cached as busy time
KIND_LABEL = {"study": "Study", "review": "Review", "workout": "Workout", "task": "To-do"}


class OutlookError(RuntimeError):
    """User-facing; never contains tokens."""


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """CREATE TABLE IF NOT EXISTS outlook_links (
               block_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, fingerprint TEXT NOT NULL, synced_at TEXT NOT NULL
           );
           CREATE TABLE IF NOT EXISTS outlook_events (
               id TEXT PRIMARY KEY, title TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
               all_day INTEGER NOT NULL DEFAULT 0, busy INTEGER NOT NULL DEFAULT 1, location TEXT NOT NULL DEFAULT ''
           );
           CREATE INDEX IF NOT EXISTS outlook_events_start ON outlook_events (start);"""
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(outlook_events)")}
    if "organizer" not in columns:  # you organize it (so moving it doesn't fight an invitation)
        db.execute("ALTER TABLE outlook_events ADD COLUMN organizer INTEGER NOT NULL DEFAULT 1")
    if "series" not in columns:     # part of a repeating series (edits change just this occurrence)
        db.execute("ALTER TABLE outlook_events ADD COLUMN series INTEGER NOT NULL DEFAULT 0")


# ---------------------------------------------------------------- settings + encrypted token cache

def _setting(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _set(db: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        db.execute("DELETE FROM settings WHERE key = ?", (key,))
    else:
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def _token_path() -> Path:
    return core.DATA_DIR / "outlook_token.bin"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    """Encrypt/decrypt with Windows DPAPI (current user). Elsewhere, store as-is."""
    if sys.platform != "win32":
        return data
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(data, len(data))
    source, result = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), _Blob()
    call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not call(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result)):
        raise OutlookError("Windows couldn't protect the Outlook sign-in. Disconnect and connect again.")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(result.pbData)


def _load_cache():
    import msal
    cache = msal.SerializableTokenCache()
    path = _token_path()
    if path.exists():
        try:
            cache.deserialize(_dpapi(path.read_bytes(), protect=False).decode("utf-8"))
        except (OSError, OutlookError, UnicodeDecodeError, ValueError):
            pass  # unreadable cache = signed out
    return cache


def _save_cache(cache) -> None:
    if cache.has_state_changed:
        path = _token_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_dpapi(cache.serialize().encode("utf-8"), protect=True))


def _app(client_id: str, cache):
    import msal
    return msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=cache)


# ---------------------------------------------------------------- sign-in (device code)

_pending: dict[str, Any] = {}
_lock = threading.Lock()


def start_sign_in(client_id: str) -> dict[str, Any]:
    client_id = client_id.strip()
    if len(client_id) < 30 or any(ch not in "0123456789abcdefABCDEF-" for ch in client_id):
        raise OutlookError("That doesn't look like an Application (client) ID. It's a GUID like 1a2b3c4d-....")
    cache = _load_cache()
    app = _app(client_id, cache)
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        detail = flow.get("error_description", "").split("\r\n")[0][:200]
        raise OutlookError(f"Microsoft rejected the sign-in request. Check the app registration (personal accounts, public client flows). {detail}")
    with _lock:
        _pending.clear()
        _pending.update({"state": "waiting", "client_id": client_id, "code": flow["user_code"], "url": flow["verification_uri"],
                         "expires_at": time.time() + int(flow.get("expires_in", 900))})

    def wait() -> None:
        result = app.acquire_token_by_device_flow(flow)  # blocks until you finish signing in (or it expires)
        with _lock:
            if "access_token" in result:
                _save_cache(cache)
                account = (result.get("id_token_claims") or {}).get("preferred_username") or "your account"
                with core.connection() as db:
                    _set(db, "outlook_client_id", client_id)
                    _set(db, "outlook_account", account)
                _pending.update({"state": "connected", "account": account})
            else:
                _pending.update({"state": "error", "message": (result.get("error_description") or "Sign-in didn't finish.").split("\r\n")[0][:200]})

    threading.Thread(target=wait, name="outlook-sign-in", daemon=True).start()
    return sign_in_status()


def sign_in_status() -> dict[str, Any]:
    with _lock:
        return {key: value for key, value in _pending.items() if key != "client_id"}


def _access_token(db: sqlite3.Connection) -> str:
    client_id = _setting(db, "outlook_client_id")
    if not client_id:
        raise OutlookError("Outlook isn't connected.")
    cache = _load_cache()
    app = _app(client_id, cache)
    accounts = app.get_accounts()
    result = app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
    if not result or "access_token" not in result:
        raise OutlookError("Outlook sign-in expired. Connect again in More → Calendars.")
    _save_cache(cache)
    return result["access_token"]


def disconnect(db: sqlite3.Connection) -> None:
    _token_path().unlink(missing_ok=True)
    for key in ("outlook_client_id", "outlook_account", "outlook_calendar_id", "outlook_target", "outlook_last_sync", "outlook_last_error"):
        _set(db, key, None)
    db.execute("DELETE FROM outlook_links")


# ---------------------------------------------------------------- Microsoft Graph

Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


def _urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read() or b""
    except (urllib.error.URLError, TimeoutError) as error:
        raise OutlookError("Couldn't reach Microsoft. Check this computer's internet connection.") from error


class Graph:
    def __init__(self, token: str, transport: Transport | None = None):
        self.token, self.transport = token, transport or _urllib_transport

    def call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
        url = path if path.startswith("http") else f"{GRAPH}{path}"
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        for attempt in range(3):
            status, raw = self.transport(method, url, headers, body)
            if status == 429 or status >= 500:
                time.sleep(min(10, 2 ** attempt))
                continue
            break
        data = json.loads(raw) if raw else None
        if status in (401, 403):
            raise OutlookError("Microsoft refused access. Check the app has Calendars.ReadWrite, then connect again.")
        if status >= 400 and status != 404:
            message = ((data or {}).get("error") or {}).get("message", "") if isinstance(data, dict) else ""
            raise OutlookError(f"Outlook returned an error ({status}). {message[:160]}".strip())
        return status, data


# ---------------------------------------------------------------- sync

def _utc(block_date: str, minutes: int) -> str:
    local = datetime.combine(date.fromisoformat(block_date), datetime.min.time()) + timedelta(minutes=minutes)
    return local.astimezone().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


NOTE = "Planned in Compass. Change or remove it in Compass — edits made here are overwritten on the next sync."


def event_for(block: dict[str, Any]) -> dict[str, Any]:
    return {
        "subject": f"{'✓ ' if block.get('outcome') == 'done' else ''}{KIND_LABEL.get(block['kind'], 'Plan')}: {block['title']}",
        "start": {"dateTime": _utc(block["block_date"], block["start"]), "timeZone": "UTC"},
        "end": {"dateTime": _utc(block["block_date"], block["end"]), "timeZone": "UTC"},
        "body": {"contentType": "text", "content": NOTE},
        "showAs": "busy", "isReminderOn": True, "reminderMinutesBeforeStart": 10, "categories": [CATEGORY],
    }


def event_for_mine(occurrence: dict[str, Any]) -> dict[str, Any]:
    """One occurrence of your own Compass event (each occurrence is its own Outlook event)."""
    day = occurrence["occurrence"]
    if occurrence["all_day"]:
        nxt = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        start, end, extra = {"dateTime": f"{day}T00:00:00", "timeZone": "UTC"}, {"dateTime": f"{nxt}T00:00:00", "timeZone": "UTC"}, {"isAllDay": True, "isReminderOn": False}
    else:
        start = {"dateTime": _utc(day, occurrence["start_minutes"]), "timeZone": "UTC"}
        end = {"dateTime": _utc(day, occurrence["end_minutes"]), "timeZone": "UTC"}
        extra = {"isAllDay": False, "isReminderOn": True, "reminderMinutesBeforeStart": 15}
    content = f"{occurrence['notes']}\n\n{NOTE}".strip() if occurrence.get("notes") else NOTE
    return {"subject": occurrence["title"], "start": start, "end": end, "body": {"contentType": "text", "content": content},
            "location": {"displayName": occurrence.get("location") or ""}, "showAs": "busy" if occurrence["busy"] else "free",
            "categories": [CATEGORY], **extra}


def _fingerprint(event: dict[str, Any]) -> str:
    keys = [event["subject"], event["start"], event["end"], event.get("showAs"), event.get("location"), event.get("isAllDay"), event["body"]["content"], TARGET]
    return hashlib.sha256(json.dumps(keys, sort_keys=True).encode()).hexdigest()[:16]


def _move_to_main(db: sqlite3.Connection, graph: Graph) -> None:
    """One-time: events written to the old separate "Compass" calendar are deleted there and
    recreated in the main Calendar by this sync. The (now empty) Compass calendar is left alone."""
    if _setting(db, "outlook_target") == TARGET:
        return
    for row in db.execute("SELECT event_id FROM outlook_links").fetchall():
        graph.call("DELETE", f"/me/events/{urllib.parse.quote(row[0])}")
    db.execute("DELETE FROM outlook_links")
    _set(db, "outlook_target", TARGET)


def _wanted(db: sqlite3.Connection, start: date, end: date) -> dict[str, dict[str, Any]]:
    """Every Outlook event Compass should have written in the window, keyed by link id."""
    from assistant import events  # local import: events doesn't depend on outlook
    rows = db.execute("SELECT * FROM plan_blocks WHERE block_date BETWEEN ? AND ? AND outcome != 'missed'", (start.isoformat(), end.isoformat()))
    wanted = {row["id"]: event_for(dict(row)) for row in rows}
    wanted.update({occ["id"]: event_for_mine(occ) for occ in events.occurrences(db, start, end)})
    return wanted


def _stale(db: sqlite3.Connection, link_id: str, wanted: dict[str, Any], start: date) -> bool:
    """A linked Outlook event whose Compass item is gone. Past occurrences of your own events are left alone."""
    if link_id in wanted:
        return False
    if link_id.startswith("mine:"):
        return link_id.rsplit(":", 1)[-1] >= start.isoformat()
    return not db.execute("SELECT 1 FROM plan_blocks WHERE id = ? AND outcome != 'missed'", (link_id,)).fetchone()  # missed: removed


_sync_lock = threading.Lock()


def sync(db: sqlite3.Connection, transport: Transport | None = None, today: date | None = None) -> dict[str, Any]:
    """Make your Calendar's Compass events match accepted plan blocks and your own events (yesterday
    through three weeks out), then read the rest of the Calendar so its events count as busy time.

    Only one sync runs at a time (Sync now, the debounced sync after edits and the background loop
    can overlap), and each one commits before letting the next in — even after an error — so a
    created event's link is always visible to the next sync. Otherwise both would create it."""
    with _sync_lock:
        try:
            return _sync(db, transport, today)
        finally:
            db.commit()


def _sync(db: sqlite3.Connection, transport: Transport | None, today: date | None) -> dict[str, Any]:
    today = today or date.today()
    graph = Graph(_access_token(db), transport)
    _move_to_main(db, graph)
    start, end = today - timedelta(days=WINDOW_BACK), today + timedelta(days=WINDOW_AHEAD)
    wanted = _wanted(db, start, end)
    links = {row["block_id"]: dict(row) for row in db.execute("SELECT * FROM outlook_links")}
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    counts = {"created": 0, "updated": 0, "deleted": 0, "unchanged": 0, "read": 0}
    for link_id, link in links.items():  # removed in Compass -> remove from Outlook
        if _stale(db, link_id, wanted, start):
            graph.call("DELETE", f"/me/events/{urllib.parse.quote(link['event_id'])}")
            db.execute("DELETE FROM outlook_links WHERE block_id = ?", (link_id,))
            counts["deleted"] += 1
    for block_id, event in wanted.items():
        fingerprint = _fingerprint(event)
        link = links.get(block_id)
        if link and link["fingerprint"] == fingerprint:
            counts["unchanged"] += 1
            continue
        if link:
            status, _ = graph.call("PATCH", f"/me/events/{urllib.parse.quote(link['event_id'])}", event)
            if status != 404:
                db.execute("UPDATE outlook_links SET fingerprint = ?, synced_at = ? WHERE block_id = ?", (fingerprint, now, block_id))
                counts["updated"] += 1
                continue
        _, created = graph.call("POST", "/me/calendar/events", event)
        db.execute("INSERT OR REPLACE INTO outlook_links (block_id, event_id, fingerprint, synced_at) VALUES (?, ?, ?, ?)", (block_id, created["id"], fingerprint, now))
        counts["created"] += 1
    linked = {row[0] for row in db.execute("SELECT event_id FROM outlook_links")}  # includes ones created just now
    counts["read"], counts["deleted"] = pull_events(db, graph, today, skip=linked, counts=counts)
    _set(db, "outlook_last_sync", now)
    _set(db, "outlook_last_error", None)
    return counts


def _local(graph_time: dict[str, Any]) -> str:
    """Graph returns UTC wall time like '2026-11-23T15:00:00.0000000' → local 'YYYY-MM-DDTHH:MM'."""
    moment = datetime.strptime(graph_time["dateTime"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return moment.astimezone().replace(tzinfo=None).isoformat(timespec="minutes")


def pull_events(db: sqlite3.Connection, graph: Graph, today: date, skip: set[str] | None = None, counts: dict[str, int] | None = None) -> tuple[int, int]:
    """Cache your main Calendar (yesterday → READ_AHEAD days), minus Compass's own events, as busy time.
    A Compass-tagged event that isn't linked (a duplicate, or left over) is deleted.
    Returns (events read, total deleted including `counts["deleted"]`)."""
    deleted = (counts or {}).get("deleted", 0)
    start, end = today - timedelta(days=WINDOW_BACK), today + timedelta(days=READ_AHEAD)
    query = urllib.parse.urlencode({
        "startDateTime": f"{start.isoformat()}T00:00:00Z", "endDateTime": f"{end.isoformat()}T00:00:00Z",
        "$select": "id,subject,start,end,isAllDay,showAs,location,isCancelled,isOrganizer,type,categories", "$top": "250", "$orderby": "start/dateTime",
    })
    url: str | None = f"/me/calendarView?{query}"
    rows = []
    for _ in range(20):  # page limit
        if not url:
            break
        _, data = graph.call("GET", url)
        for item in (data or {}).get("value", []):
            if item.get("isCancelled") or item.get("id") in (skip or set()):
                continue
            if CATEGORY in (item.get("categories") or []):  # Compass made it but has no link to it
                graph.call("DELETE", f"/me/events/{urllib.parse.quote(item['id'])}")
                deleted += 1
                continue
            if item.get("isAllDay"):
                first, last = item["start"]["dateTime"][:10], item["end"]["dateTime"][:10]
                begin, finish = f"{first}T00:00", f"{last}T00:00"
            else:
                begin, finish = _local(item["start"]), _local(item["end"])
            rows.append((item["id"], (item.get("subject") or "Busy")[:200], begin, finish, int(bool(item.get("isAllDay"))),
                         int(item.get("showAs", "busy") not in ("free",)), ((item.get("location") or {}).get("displayName") or "")[:120],
                         int(item.get("isOrganizer", True) is not False), int(item.get("type") in ("occurrence", "exception"))))
        url = (data or {}).get("@odata.nextLink")
    db.execute("DELETE FROM outlook_events")
    db.executemany("INSERT OR REPLACE INTO outlook_events (id, title, start, end, all_day, busy, location, organizer, series) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows), deleted


def events_between(db: sqlite3.Connection, start: date, end: date) -> list[dict[str, Any]]:
    """Cached personal-Outlook events overlapping [start, end], shaped like calendar feed events."""
    rows = db.execute("SELECT * FROM outlook_events WHERE start < ? AND end > ? ORDER BY all_day DESC, start",
                      (f"{(end + timedelta(days=1)).isoformat()}T00:00", f"{start.isoformat()}T00:00")).fetchall()
    return [{**dict(row), "all_day": bool(row["all_day"]), "busy": bool(row["busy"]), "organizer": bool(row["organizer"]), "series": bool(row["series"]),
             "movable": movable(row), "feed_name": "Outlook", "feed_kind": "outlook", "fixed": True} for row in rows]


def movable(row: Any) -> bool:
    """Whether the planner may suggest a new time: a timed, busy event you organize, at most
    MOVABLE_MAX long, that hasn't started yet."""
    if row["all_day"] or not row["busy"] or not row["organizer"]:
        return False
    begin, finish = datetime.fromisoformat(row["start"]), datetime.fromisoformat(row["end"])
    return begin.date() == finish.date() and 0 < (finish - begin).total_seconds() / 60 <= MOVABLE_MAX and begin > datetime.now()


# ---------------------------------------------------------------- changing your own Outlook events

def _cached(db: sqlite3.Connection, event_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM outlook_events WHERE id = ?", (event_id,)).fetchone()
    if not row:
        raise OutlookError("Compass doesn't have that Outlook event any more. Press Sync now and try again.")
    return row


def edit_event(db: sqlite3.Connection, event_id: str, changes: dict[str, Any], transport: Transport | None = None) -> dict[str, Any]:
    """Change one of your personal Outlook events (title, location, busy, date, start/end minutes).
    For a repeating event this changes only that occurrence. Updates the local copy right away."""
    row = dict(_cached(db, event_id))
    begin, finish = datetime.fromisoformat(row["start"]), datetime.fromisoformat(row["end"])
    day = date.fromisoformat(changes["date"]) if changes.get("date") else begin.date()
    patch: dict[str, Any] = {}
    if changes.get("title") is not None:
        title = changes["title"].strip()[:200]
        if not title:
            raise OutlookError("Give the event a title.")
        patch["subject"], row["title"] = title, title
    if changes.get("location") is not None:
        place = changes["location"][:120]
        patch["location"], row["location"] = {"displayName": place}, place
    if changes.get("busy") is not None:
        patch["showAs"], row["busy"] = ("busy" if changes["busy"] else "free"), int(bool(changes["busy"]))
    if row["all_day"]:
        if day != begin.date():
            length = max(1, (finish.date() - begin.date()).days)
            last = day + timedelta(days=length)
            patch["start"], patch["end"] = {"dateTime": f"{day}T00:00:00", "timeZone": "UTC"}, {"dateTime": f"{last}T00:00:00", "timeZone": "UTC"}
            row["start"], row["end"] = f"{day}T00:00", f"{last}T00:00"
    elif changes.get("date") or changes.get("start") is not None or changes.get("end") is not None:
        old_start = begin.hour * 60 + begin.minute
        start = changes["start"] if changes.get("start") is not None else old_start
        end = changes["end"] if changes.get("end") is not None else start + int((finish - begin).total_seconds() // 60)
        if not (0 <= start < end <= 24 * 60):
            raise OutlookError("The end time has to be after the start time (same day).")
        patch["start"] = {"dateTime": _utc(day.isoformat(), start), "timeZone": "UTC"}
        patch["end"] = {"dateTime": _utc(day.isoformat(), end), "timeZone": "UTC"}
        moment = datetime.combine(day, datetime.min.time())
        row["start"] = (moment + timedelta(minutes=start)).isoformat(timespec="minutes")
        row["end"] = (moment + timedelta(minutes=end)).isoformat(timespec="minutes")
    if patch:
        status, _ = Graph(_access_token(db), transport).call("PATCH", f"/me/events/{urllib.parse.quote(event_id)}", patch)
        if status == 404:
            db.execute("DELETE FROM outlook_events WHERE id = ?", (event_id,))
            raise OutlookError("That event was already deleted in Outlook.")
        db.execute("UPDATE outlook_events SET title = ?, start = ?, end = ?, busy = ?, location = ? WHERE id = ?",
                   (row["title"], row["start"], row["end"], row["busy"], row["location"], event_id))
    return row


def delete_event(db: sqlite3.Connection, event_id: str, transport: Transport | None = None) -> None:
    """Delete one of your personal Outlook events (for a repeating event, just that occurrence)."""
    _cached(db, event_id)
    Graph(_access_token(db), transport).call("DELETE", f"/me/events/{urllib.parse.quote(event_id)}")
    db.execute("DELETE FROM outlook_events WHERE id = ?", (event_id,))


def status(db: sqlite3.Connection) -> dict[str, Any]:
    connected = bool(_setting(db, "outlook_client_id")) and _token_path().exists()
    return {"connected": connected, "account": _setting(db, "outlook_account"), "last_sync": _setting(db, "outlook_last_sync"),
            "last_error": _setting(db, "outlook_last_error"), "linked_events": db.execute("SELECT COUNT(*) FROM outlook_links").fetchone()[0],
            "sign_in": sign_in_status()}


def sync_quietly() -> dict[str, Any] | None:
    """Sync if connected; record (don't raise) errors. Used by the background loop and after plan changes."""
    with core.connection() as db:
        if not status(db)["connected"]:
            return None
        try:
            return sync(db)
        except OutlookError as error:
            _set(db, "outlook_last_error", str(error))
            return None


_soon_timer: threading.Timer | None = None


def sync_soon(delay: float = 5.0) -> None:
    """Debounced background sync after the plan changes (several quick edits -> one sync)."""
    global _soon_timer
    with core.connection() as db:
        if not status(db)["connected"]:
            return  # nothing to do, and no timer left behind
    if _soon_timer:
        _soon_timer.cancel()
    _soon_timer = threading.Timer(delay, sync_quietly)
    _soon_timer.daemon = True
    _soon_timer.start()
