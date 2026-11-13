# Health Hub

Health Hub is a local-first, responsive training and recovery planner. It pairs
a structured lifting/running plan with daily readiness context, workout logging,
and reviewable Garmin activity imports—without requiring a cloud account.

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
git clone https://github.com/YOUR-USERNAME/health-hub.git
cd health-hub
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn app:app --reload --port 8765
```

Then open `http://127.0.0.1:8765`.

On the original Windows setup, you can also double-click:

`Launch Lifting Hub.cmd`

For a visible troubleshooting window, use:

`Start Lifting Hub (diagnostic).cmd`

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

## Project structure

```text
health-hub/
├── app.py                 # FastAPI routes, planning logic, SQLite persistence
├── static/                # Responsive PWA interface
├── scripts/load_demo_data.py
├── tests/                 # Focused automated checks
├── data/                  # Local-only SQLite database (ignored by Git)
└── requirements.txt
```

## GitHub portfolio safety

This folder is safe to publish only after checking it does **not** include:

- `data/lifting_hub.db` or another database file
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
