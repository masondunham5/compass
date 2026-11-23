"""Package your Compass data to move it to the always-on computer.

Run on this PC (where Compass has been running):
    ..\\.venv\\Scripts\\python.exe scripts\\export_for_server.py

Makes compass-move.zip on your Desktop with a consistent copy of data\\compass.db. Copy that zip
to the server (e.g. RustDesk file transfer) and pass it to setup_server.ps1 -ImportZip.

Not included, on purpose:
- The Outlook sign-in and the AI key: they're encrypted for this PC's Windows account and won't
  open anywhere else. Connect Outlook and paste the key again on the server (once).
- .env (your Garmin key): copy it to the server yourself if you want Garmin syncing there.
Stop Compass on this PC once the server is running, so there's only one copy changing.
"""

from __future__ import annotations

import sqlite3
import tempfile
from contextlib import closing
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "compass.db"


def main() -> None:
    if not SOURCE.exists():
        raise SystemExit("No data\\compass.db here — nothing to move.")
    desktop = Path.home() / "Desktop"
    if not desktop.exists():
        desktop = Path.home()
    target = desktop / "compass-move.zip"
    with tempfile.TemporaryDirectory() as folder:
        snapshot = Path(folder) / "compass.db"
        with closing(sqlite3.connect(SOURCE)) as src, closing(sqlite3.connect(snapshot)) as dst:
            src.backup(dst)  # consistent even while Compass is running
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot, "compass.db")
    size = target.stat().st_size / 1_000_000
    print(f"Created {target} ({size:.1f} MB). Copy it to the server and run setup_server.ps1 -ImportZip <path>.")


if __name__ == "__main__":
    main()
