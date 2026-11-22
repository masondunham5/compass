"""Create a timestamped, local-only Compass database backup.

Run from the project root:
    .\\.venv\\Scripts\\python.exe scripts\\backup_database.py

Backups are intentionally ignored by Git. This command never modifies the
source database and does not upload anything.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "compass.db"
BACKUPS = ROOT / "backups"


def main() -> None:
    if not SOURCE.exists():
        print("Stopped: no Compass database exists yet. Nothing was changed.")
        return
    BACKUPS.mkdir(exist_ok=True)
    destination = BACKUPS / f"compass_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.db"
    # SQLite's backup API gives a consistent snapshot even if the server is running.
    with sqlite3.connect(SOURCE) as source, sqlite3.connect(destination) as target:
        source.backup(target)
    print(f"Created local backup: {destination}")


if __name__ == "__main__":
    main()
