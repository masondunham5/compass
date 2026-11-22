# Compass

Compass is a local-first life planner. It brings your calendars (school and
personal, read-only), your day, and your week into one place, with health and
training as a built-in section: a structured lifting/running plan, daily
readiness from Garmin, workout logging, and recovery insights—without
requiring a cloud account.

> Portfolio demo project. The repository intentionally excludes personal health
> data, local databases, exports, and credentials.

## What it does today

- Builds training blocks around goals, available time, equipment, preferred
  lifting split, and running volume.
- Shows upcoming training sessions and weekly balance.
- Uses a daily readiness check-in to give a realistic recommendation.
- Adds a daily health check-in for sleep, energy, soreness, stress, protein consistency, and optional weight.
- Provides a recovery-first recommendation without changing training automatically.
- Imports Garmin CSV, TCX, and GPX activity exports locally; a run is linked to a planned session only after explicit approval.
- Is ready for an optional Garmin Connect Personal API sync: activities plus daily sleep, HRV, resting heart rate, steps, calories, stress, and Body Battery are stored only in the local database.
- Lets you lock a session so later scheduling changes cannot move it.
- Provides an active-workout screen with exercise targets, per-set weight/reps,
  quick `+5` weight changes, completion states, and a rest timer.
- Stores all training data in a local SQLite database.
- Can be installed from a browser as a PWA when served over HTTPS or localhost.

## Tech stack

| Layer | Choice |
| --- | --- |
| Backend | Python + FastAPI |
| Data | Local SQLite |
| Frontend | Dependency-free HTML, CSS, and JavaScript |
| Delivery | Progressive Web App + Uvicorn |
| Privacy model | Local-first; no Garmin password or cloud account is stored |

## Run locally

**Prerequisites:** Python 3.11+.

```powershell
git clone https://github.com/YOUR-USERNAME/compass.git
cd compass
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn app:app --reload --port 8765
```

Then open `http://127.0.0.1:8765`.

On the original Windows setup, you can also double-click:

`Launch Compass.cmd`

For a visible troubleshooting window, use:

`Start Compass (diagnostic).cmd`

### Optional demo data

The app starts empty by design. To create an anonymous sample plan and recovery
check-in, run this before starting the server:

```powershell
python scripts/load_demo_data.py
```

The script stops rather than overwrite an existing local database.

## Tests

```powershell
python -m pytest
```

The included tests cover prescription parsing, recovery guidance, and Garmin CSV
normalization. They deliberately do not touch your production database.

## Phone access later

Run this app on the always-on Windows computer and put the computer and phone on
the same private Tailscale network. That provides private phone access without
exposing the app directly to the public internet. Use HTTPS before treating the
phone deployment as installable outside of your home computer.

## Optional Garmin Personal API sync

This is an optional **personal-use** connection. Compass never stores a
Garmin password. It stores only the Personal API key you create in Garmin
Connect, and only in a local `.env` file that Git ignores.

On the always-on Windows computer:

1. Create a Garmin Personal API key in Garmin Connect. Do not share it or add
   it to GitHub.
2. Copy `.env.example` to `.env` in the Compass project folder.
3. Set the following values in `.env`:

   ```text
   GARMIN_PERSONAL_API_KEY=your-key-here
   GARMIN_KEY_EXPIRES_ON=2027-11-14
   GARMIN_SYNC_DAYS=2
   ```

4. With the server stopped or running, test a rolling seven-day pull:

   ```powershell
   .\.venv\Scripts\python.exe garmin_sync.py --days 7
   ```

5. Open Compass and review **Recovery**. The Garmin Daily Sync section
   reports connection state, last sync, and the first dashboard metrics.

For automatic daily updates, create a second Task Scheduler task (separate
from the server task):

```text
Program/script: <Compass folder>\.venv\Scripts\python.exe
Arguments: garmin_sync.py --days 2
Start in: <Compass folder>
Trigger: Daily, after Garmin Connect has had time to sync your watch
```

Or, after a successful manual sync, create the same task with the included
script (default: 11:00 PM daily):

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\register_garmin_sync_task.ps1
```

The sync task may be run repeatedly; it upserts daily metrics and ignores
already-imported activities. If Garmin rejects a key, Compass records a
safe error message and leaves existing data unchanged.

## Deterministic training recommendations

Compass's daily training recommendation uses no AI model and never changes,
moves, or cancels a session. It is a transparent rules engine:

1. Start each day at a recovery score of **100**.
2. Deduct points for the following signals: sleep under 7 hours (-12), sleep
   under 6 hours (-30), HRV at least 15% below the user's rolling Garmin
   baseline (-20), resting HR at least 8% above baseline (-15), Garmin stress
   at least 65 (-10), soreness at least 6/10 (-8) or 8/10 (-30), and energy
   at most 5/10 (-8) or 3/10 (-25).
3. Choose today's planned session; if none exists, choose the next planned
   session. A locked planned session is surfaced first but is never moved.
4. Score 76–100: recommend the planned session. Score 51–75: recommend a
   shortened version (about 60% of duration; no running intensity). Score
   0–50: recommend recovery walking/mobility or rest and explicitly reject
   catch-up volume.
5. Display every triggered rule in the UI so the recommendation is auditable.

The rolling HRV and resting-HR baselines use the available prior Garmin days
(up to 28). With no Garmin data, the plan and manual check-in remain usable;
the dashboard clearly indicates which recovery signals are unavailable.

### Local backup

Before a major update—or whenever you want a copy of your history—create a
consistent local snapshot while Compass is running:

```powershell
.\.venv\Scripts\python.exe scripts\backup_database.py
```

The result is placed in the ignored `backups/` folder. It is never uploaded or
included in GitHub.

## Project structure

```text
compass/
├── app.py                 # FastAPI routes, planning logic, SQLite persistence
├── garmin_sync.py         # Optional private Garmin Personal API sync command
├── static/                # Responsive PWA interface
├── scripts/load_demo_data.py
├── tests/                 # Focused automated checks
├── data/                  # Local-only SQLite database (ignored by Git)
└── requirements.txt
```

## GitHub portfolio safety

This folder is safe to publish only after checking it does **not** include:

- `data/compass.db` or another database file
- `.env` files, tokens, or connection URLs
- personal workout logs, screenshots, or exported health data

The repository-level `.gitignore` excludes the local database, common secrets,
caches, exports, and private screenshot folders. Add only intentionally
sanitized screenshots or a demo video before publishing.

## Roadmap

- Authenticated private sync from a phone companion to a home server
- More robust Garmin activity-format support
- Training-load and trend visualizations
- Exportable plan and workout summaries

## License

Add a license before making a public repository available. The MIT License is a
simple default for a portfolio project; choose a different license if you want
to restrict reuse.
