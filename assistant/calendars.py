"""Read-only calendar feeds (ICS links or uploaded .ics files) stored as local events.

Feeds are never written back to. A feed's URL is a secret (anyone with it can read the
calendar), so it is stored only in the local database, never returned by the API in full,
and never included in error messages or logs.
"""

from __future__ import annotations

import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import uuid4

WINDOW_PAST_DAYS = 14
WINDOW_FUTURE_DAYS = 180
MAX_BYTES = 10 * 1024 * 1024
STALE_HOURS = 3
FEED_KINDS = {"school", "personal", "other"}


class CalendarError(ValueError):
    """User-facing calendar problem; messages never contain the feed URL."""


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS calendar_feeds (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'school',
            source TEXT NOT NULL DEFAULT 'url',
            url TEXT,
            fixed INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_synced TEXT,
            last_error TEXT,
            event_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS calendar_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feed_id TEXT NOT NULL,
            uid TEXT NOT NULL,
            title TEXT NOT NULL,
            start TEXT NOT NULL,
            end TEXT NOT NULL,
            all_day INTEGER NOT NULL DEFAULT 0,
            location TEXT NOT NULL DEFAULT '',
            busy INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS calendar_events_start ON calendar_events (start);
        """
    )


# ---------------------------------------------------------------- fetching and parsing

def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CalendarError("That doesn't look like a calendar link. It should start with https:// or webcal://.")
    return url


def url_hint(url: str | None) -> str | None:
    """Safe-to-show description of a feed URL: host and the last path segment only."""
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    return f"{parsed.netloc}/…/{tail}" if tail else parsed.netloc


def fetch(url: str) -> bytes:
    request = urllib.request.Request(normalize_url(url), headers={"User-Agent": "HealthHub-Calendar/1.0", "Accept": "text/calendar, */*"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        if error.code in {401, 403, 404}:
            raise CalendarError(f"The calendar server refused the link ({error.code}). It may have been unpublished or reset.") from error
        raise CalendarError(f"The calendar server returned an error ({error.code}). Try again later.") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise CalendarError("Couldn't reach the calendar server. Check this computer's internet connection.") from error
    if len(body) > MAX_BYTES:
        raise CalendarError("That calendar is larger than 10 MB, which is more than Compass will import.")
    return body


def _local_naive(value: Any) -> tuple[datetime, bool]:
    """Convert an ICS date/datetime to local wall-clock time. Returns (datetime, all_day)."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone().replace(tzinfo=None)
        return value, False
    if isinstance(value, date):
        return datetime.combine(value, time()), True
    raise CalendarError("The calendar contains an event with an unreadable date.")


def parse_events(content: bytes, today: date | None = None) -> list[dict[str, Any]]:
    """Expand an .ics file into individual occurrences within the import window."""
    import icalendar
    import recurring_ical_events

    today = today or date.today()
    try:
        calendar = icalendar.Calendar.from_ical(content)
    except ValueError as error:
        raise CalendarError("That file isn't a valid calendar (.ics).") from error
    start = datetime.combine(today - timedelta(days=WINDOW_PAST_DAYS), time())
    end = datetime.combine(today + timedelta(days=WINDOW_FUTURE_DAYS), time())
    events = []
    for component in recurring_ical_events.of(calendar).between(start, end):
        if str(component.get("STATUS", "")).upper() == "CANCELLED":
            continue
        begin, all_day = _local_naive(component.decoded("DTSTART"))
        if component.get("DTEND") is not None:
            finish, _ = _local_naive(component.decoded("DTEND"))
        elif component.get("DURATION") is not None:
            finish = begin + component.decoded("DURATION")
        else:
            finish = begin + (timedelta(days=1) if all_day else timedelta(hours=1))
        transparent = str(component.get("TRANSP", "")).upper() == "TRANSPARENT"
        free = str(component.get("X-MICROSOFT-CDO-BUSYSTATUS", "")).upper() == "FREE"
        events.append({
            "uid": str(component.get("UID", "")) or uuid4().hex,
            "title": re.sub(r"\s+", " ", str(component.get("SUMMARY", "Busy"))).strip() or "Busy",
            "start": begin.isoformat(timespec="minutes"), "end": finish.isoformat(timespec="minutes"),
            "all_day": all_day, "location": str(component.get("LOCATION", "") or "").strip()[:200],
            "busy": not (transparent or free),
        })
    events.sort(key=lambda event: event["start"])
    return events


# ---------------------------------------------------------------- feeds

def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def public_feed(row: sqlite3.Row) -> dict[str, Any]:
    feed = dict(row)
    feed["url_hint"] = url_hint(feed.pop("url"))
    feed["fixed"] = bool(feed["fixed"])
    return feed


def list_feeds(db: sqlite3.Connection) -> list[dict[str, Any]]:
    return [public_feed(row) for row in db.execute("SELECT * FROM calendar_feeds ORDER BY created_at")]


def store_events(db: sqlite3.Connection, feed_id: str, events: list[dict[str, Any]]) -> int:
    db.execute("DELETE FROM calendar_events WHERE feed_id = ?", (feed_id,))
    db.executemany(
        "INSERT INTO calendar_events (feed_id, uid, title, start, end, all_day, location, busy) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(feed_id, e["uid"], e["title"], e["start"], e["end"], int(e["all_day"]), e["location"], int(e["busy"])) for e in events],
    )
    db.execute("UPDATE calendar_feeds SET last_synced = ?, last_error = NULL, event_count = ? WHERE id = ?", (_now(), len(events), feed_id))
    return len(events)


def add_feed(db: sqlite3.Connection, name: str, kind: str, url: str | None = None, content: bytes | None = None, fixed: bool = True) -> dict[str, Any]:
    """Create a feed from a link (fetched now) or an uploaded file. Nothing is saved if it can't be read."""
    if kind not in FEED_KINDS:
        raise CalendarError("Pick school, personal, or other.")
    if url:
        url = normalize_url(url)
        if db.execute("SELECT 1 FROM calendar_feeds WHERE url = ?", (url,)).fetchone():
            raise CalendarError("That calendar link is already added.")
        content = fetch(url)
    if content is None:
        raise CalendarError("Paste a calendar link or choose an .ics file.")
    events = parse_events(content)
    feed_id = f"cal-{uuid4().hex[:10]}"
    db.execute("INSERT INTO calendar_feeds (id, name, kind, source, url, fixed, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
               (feed_id, name.strip() or "Calendar", kind, "url" if url else "file", url, int(fixed), _now()))
    store_events(db, feed_id, events)
    return public_feed(db.execute("SELECT * FROM calendar_feeds WHERE id = ?", (feed_id,)).fetchone())


def sync_feed(db: sqlite3.Connection, feed_id: str) -> dict[str, Any]:
    row = db.execute("SELECT * FROM calendar_feeds WHERE id = ?", (feed_id,)).fetchone()
    if not row:
        raise CalendarError("Calendar not found.")
    if row["source"] != "url":
        raise CalendarError("Uploaded calendars don't refresh — upload a new file to update it.")
    try:
        store_events(db, feed_id, parse_events(fetch(row["url"])))
    except CalendarError as error:
        db.execute("UPDATE calendar_feeds SET last_error = ? WHERE id = ?", (str(error), feed_id))
        raise
    return public_feed(db.execute("SELECT * FROM calendar_feeds WHERE id = ?", (feed_id,)).fetchone())


def sync_stale(db: sqlite3.Connection, max_age_hours: float = STALE_HOURS) -> int:
    """Refresh link feeds not synced recently. Errors are recorded on the feed, never raised."""
    cutoff = (datetime.now().astimezone() - timedelta(hours=max_age_hours)).isoformat(timespec="seconds")
    refreshed = 0
    for row in db.execute("SELECT id FROM calendar_feeds WHERE source = 'url' AND (last_synced IS NULL OR last_synced < ?)", (cutoff,)).fetchall():
        try:
            sync_feed(db, row["id"])
            refreshed += 1
        except CalendarError:
            pass
    return refreshed


def remove_feed(db: sqlite3.Connection, feed_id: str) -> bool:
    db.execute("DELETE FROM calendar_events WHERE feed_id = ?", (feed_id,))
    return bool(db.execute("DELETE FROM calendar_feeds WHERE id = ?", (feed_id,)).rowcount)


def events_between(db: sqlite3.Connection, start: date, end: date) -> list[dict[str, Any]]:
    """Events overlapping [start, end] (inclusive dates), with their feed's name/kind/fixed flag."""
    rows = db.execute(
        """SELECT e.*, f.name AS feed_name, f.kind AS feed_kind, f.fixed AS fixed FROM calendar_events e
           JOIN calendar_feeds f ON f.id = e.feed_id
           WHERE e.start < ? AND e.end > ? ORDER BY e.all_day DESC, e.start""",
        # Compare full timestamps: an event ending at 00:00 on a day does not overlap that day.
        (f"{(end + timedelta(days=1)).isoformat()}T00:00", f"{start.isoformat()}T00:00"),
    ).fetchall()
    return [{**dict(row), "all_day": bool(row["all_day"]), "busy": bool(row["busy"]), "fixed": bool(row["fixed"])} for row in rows]
