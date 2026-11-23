"""Running Compass on another computer: School To Do sync, backups to a folder, the move export."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import app  # noqa: E402
import core  # noqa: E402
from assistant import school  # noqa: E402

SCHOOL_APP = '''
from datetime import date
ITEMS = [
    ("2030-01-15", "CSE 231", "assignment", "Project 1"),
    ("2030-01-20", "ITM 210", "quiz", "Quiz 3"),
]
ESTIMATE_MINUTES = {"assignment": 120, "quiz": 45}
'''


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    monkeypatch.setattr(core, "DATABASE", tmp_path / "test.db")
    monkeypatch.setattr(core, "LEGACY_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(core, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.delenv("SCHOOL_TODO_APP", raising=False)
    monkeypatch.delenv("SCHOOL_TODO_DATA", raising=False)
    monkeypatch.setattr(school, "DEFAULT_APP", tmp_path / "not_here" / "app.py")  # no School To Do on the server
    core.initialize()
    (tmp_path / "school_sync_token.txt").write_text("a-long-secret-sync-token\n", encoding="utf-8")
    return tmp_path


def test_school_files_sync_to_the_server(server):
    files = {"app.py": SCHOOL_APP, "completed.json": json.dumps(["2030-01-20|ITM 210|quiz|Quiz 3"])}
    with TestClient(app.app) as client:
        assert client.post("/api/assistant/school/sync", json={"token": "wrong-token-1234567", "files": files}).status_code == 403
        assert client.post("/api/assistant/school/sync", json={"token": "a-long-secret-sync-token", "files": {"evil.py": "x"}}).status_code == 400
        assert client.post("/api/assistant/school/sync", json={"token": "a-long-secret-sync-token", "files": {**files, "completed.json": "{nope"}}).status_code == 400
        result = client.post("/api/assistant/school/sync", json={"token": "a-long-secret-sync-token", "files": files}).json()
    assert result["stored"] == ["app.py", "completed.json"] and result["tasks"] == 2
    tasks = {t.title: t for t in school.load_tasks()}
    assert tasks["Quiz 3"].done and not tasks["Project 1"].done
    assert school._paths() == (server / "school_sync" / "app.py", server / "school_sync")


def test_no_token_file_means_sync_is_off(server):
    (server / "school_sync_token.txt").unlink()
    with TestClient(app.app) as client:
        assert client.post("/api/assistant/school/sync", json={"token": "a-long-secret-sync-token", "files": {"app.py": SCHOOL_APP}}).status_code == 403


def test_backup_to_a_folder_keeps_the_newest(tmp_path, monkeypatch):
    import backup_database
    source = tmp_path / "compass.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE t (x)")
        db.execute("INSERT INTO t VALUES (1)")
    drive = tmp_path / "My Drive" / "Compass backups"
    for name in ("compass_2026-01-01_00-00-00.db", "compass_2026-01-02_00-00-00.db", "notes.txt"):
        drive.mkdir(parents=True, exist_ok=True)
        (drive / name).write_text("old")
    made = backup_database.backup(drive, keep=2, source=source)
    left = sorted(p.name for p in drive.iterdir())
    assert made.name in left and "notes.txt" in left and "compass_2026-01-01_00-00-00.db" not in left and len(left) == 3
    copy = sqlite3.connect(made)
    assert copy.execute("SELECT x FROM t").fetchone() == (1,)
    copy.close()


def test_export_for_server_makes_a_zip(tmp_path, monkeypatch):
    import zipfile

    import export_for_server
    source = tmp_path / "compass.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE t (x)")
    monkeypatch.setattr(export_for_server, "SOURCE", source)
    monkeypatch.setattr(export_for_server.Path, "home", staticmethod(lambda: tmp_path))
    export_for_server.main()
    with zipfile.ZipFile(tmp_path / "compass-move.zip") as archive:
        assert archive.namelist() == ["compass.db"]
