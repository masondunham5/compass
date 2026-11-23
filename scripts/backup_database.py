"""Create a timestamped Compass database backup.

Run from the project root:
    .\\.venv\\Scripts\\python.exe scripts\\backup_database.py
    .\\.venv\\Scripts\\python.exe scripts\\backup_database.py --to "G:\\My Drive\\Compass backups" --keep 30

Without --to, backups go to backups/ (ignored by Git) and are never pruned. With --to, the copy
goes to that folder (e.g. a Google Drive folder that syncs to the cloud) and only the newest
--keep copies made by this script are kept there. This never modifies the source database.
"""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "compass.db"
BACKUPS = ROOT / "backups"
PREFIX = "compass_"


def backup(destination_dir: Path, keep: int | None = None, source: Path = SOURCE) -> Path | None:
    if not source.exists():
        print("Stopped: no Compass database exists yet. Nothing was changed.")
        return None
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{PREFIX}{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.db"
    temp = destination.with_suffix(".db.partial")  # a cloud folder never syncs half a file
    # SQLite's backup API gives a consistent snapshot even if the server is running.
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(temp)) as target:
        src.backup(target)  # closed before the rename (Windows won't rename an open file)
    temp.replace(destination)
    if keep:
        copies = sorted(destination_dir.glob(f"{PREFIX}*.db"), key=lambda path: path.name)
        for old in copies[:-keep]:
            old.unlink()
    print(f"Created backup: {destination}")
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="Back up the Compass database.")
    parser.add_argument("--to", type=Path, help="Folder for the copy (e.g. in Google Drive). Default: backups/")
    parser.add_argument("--keep", type=int, default=30, help="With --to: how many copies to keep there (default 30).")
    args = parser.parse_args()
    if args.to:
        backup(args.to, keep=max(1, args.keep))
    else:
        backup(BACKUPS)


if __name__ == "__main__":
    main()
