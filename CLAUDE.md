# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Health Hub (folder name `lifting_hub`) is a local-first training and recovery planner: FastAPI + SQLite backend, dependency-free HTML/CSS/JS PWA frontend, and an optional Garmin Connect Personal API sync.

## Commands

The virtualenv lives one level up, at `..\.venv` (the launchers check `.venv` first, then `..\.venv`).

```powershell
# Run the app (http://127.0.0.1:8765)
..\.venv\Scripts\python.exe -m uvicorn app:app --reload --port 8765

# Tests (all / single test)
..\.venv\Scripts\python.exe -m pytest
..\.venv\Scripts\python.exe -m pytest tests/test_app.py::test_garmin_csv_parser_keeps_runs_and_converts_kilometers

# Garmin sync (1–30 days); hits the real API using the key in .env
..\.venv\Scripts\python.exe garmin_sync.py --days 7

# Re-derive metric columns from stored raw responses (no API calls), e.g. after editing METRIC_FIELDS
..\.venv\Scripts\python.exe garmin_sync.py --rebuild

# Demo data (refuses to overwrite an existing DB) / DB backup into backups/
..\.venv\Scripts\python.exe scripts\load_demo_data.py
..\.venv\Scripts\python.exe scripts\backup_database.py
```

There is no linter or build step.

## Hands off

- `.env` holds the real `GARMIN_PERSONAL_API_KEY`. Do not read, edit, or print it.
- `Garmin API Key/` is private. Do not open or modify it.
- `data/lifting_hub.db` is the user's real health data. Tests must not touch it.

## Architecture

- **`app.py`** is the whole backend: Pydantic models, the plan generator (`build_draft`), the deterministic recovery/readiness rules engine (`deterministic_training_recommendation`, documented in README), Garmin CSV/TCX/GPX file import, and every `/api/*` route. It also serves `static/` and loads `.env` itself via `load_local_env()` at import time (no python-dotenv; real environment variables win).
- **Schema** is created in `initialize()` with `CREATE TABLE IF NOT EXISTS`; there is no migration framework, so new columns on existing tables need an explicit `ALTER TABLE ... ADD COLUMN` guarded by a `PRAGMA table_info` check. `connection()` returns a new `sqlite3` connection; `with connection() as db:` commits on exit but does not close.
- **`garmin_sync.py`** imports helpers from `app` (`connection`, `initialize`, `normalize_activity`). `run_sync(days)` exchanges the API key for a bearer token, pulls the activity list, then per day (newest first) calls four endpoints (`DAILY_ENDPOINTS`: summary, sleep, HRV, heart rate) and commits that day immediately. Each run writes a row to `garmin_sync_log` (`running` → `success` / `partial` / `error`), which the UI reads for status/history.
  - **Rate limiting:** all HTTP goes through `_request`, which paces calls (`GARMIN_REQUEST_INTERVAL`, default 1.5 s, doubled after a 429) and retries 429/5xx/network errors with exponential backoff honoring `Retry-After` (`GARMIN_MAX_RETRIES`, default 5).
  - **Errors:** `GarminFatalError` (bad key, persistent 429, no network) stops the sync; days already saved are kept and the run returns `status: "partial"`. `GarminEndpointError` (other 4xx/5xx) skips just that endpoint for that day. 404/204 mean "no data".
  - **Storage:** the full response of every endpoint is kept in `garmin_raw_responses (calendar_date, endpoint)`; raw activity JSON goes in `imported_activities.raw_json`. Useful fields are copied into `garmin_daily_metrics` columns defined by `METRIC_FIELDS` (column, type, endpoint, keys). Add a field there and `ensure_schema()` adds the column on the next sync. Dotted keys are exact paths; plain keys are searched recursively. Upserts use `COALESCE`, so a null never overwrites a stored value.
- **Triggers for sync:** `POST /api/garmin/sync` (first run backfills 14 days, then `GARMIN_SYNC_DAYS` capped at 7), `POST /api/garmin/backfill` (30 days), the CLI, and the daily Task Scheduler job from `scripts/register_garmin_sync_task.ps1`.
- **`GarminSyncError`** messages are shown to the user and must never include the key or token.
- **Consumers of Garmin data:** `/api/garmin/dashboard` and `/api/recommendations/training` read `garmin_daily_metrics` (the rules engine uses `sleep_seconds`, `hrv_ms`, `resting_hr`, `stress_avg` with a 28-day rolling baseline). Keep those column names stable.
- **Frontend** (`static/app.js`, `index.html`, `styles.css`) is plain JS calling the JSON API; no build tooling. It is a phone-first dark UI with four tabs (Today, Recovery, Train, Progress) plus Workout and Settings views; desktop shows the same column centered at 640px. Today renders entirely from `GET /api/today` (week strip, focus session with short-day set caps, readiness headline, latest Garmin night, up next). Recovery reads the latest night from that same payload and trends from `/api/garmin/dashboard`. Colors are CSS custom properties at the top of `styles.css`. Bump the `?v=` query on the CSS/JS links in `index.html` (and `CACHE` in `service-worker.js`) when shipping UI changes.
- **Short-day rule:** when readiness is 51–75 on a strength day, the engine keeps every exercise but caps each at 2 sets (`adjusted_sets`) and ~60% of the time (`adjusted_minutes`); the Today card offers "Do the full N min instead".
- Activities imported from Garmin are only linked to a planned session after an explicit user action (`/api/activities/{id}/link`); recommendations never modify the schedule.
