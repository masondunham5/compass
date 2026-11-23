"""Read-only adapter for the separate School To Do app (Documents/ai-assistant/school_todo).

School To Do keeps its built-in task list in `app.py` (a RAW "date|course|kind|title" block
plus a loop that adds weekly prep budgets) and your changes in %LOCALAPPDATA%/School To Do:
  completed.json      — keys of completed tasks ("date|course|kind|title" as displayed)
  manual_tasks.json   — tasks you added or imported
  task_overrides.json — {"edits": {original_key: [d, c, k, t]}, "hidden": [original_key, ...]}
  task_details.json   — {original_key: {"minutes": int, "time": "2:40 PM", "notes": str}}

This module never writes to those files and never runs School To Do's app: it parses app.py
with `ast` and executes only the statements that build ITEMS, in a namespace with no file,
network, or import access. Paths can be overridden with SCHOOL_TODO_APP / SCHOOL_TODO_DATA.
"""

from __future__ import annotations

import ast
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_APP = ROOT.parent / "school_todo" / "app.py"
DEFAULT_DATA = Path(os.getenv("LOCALAPPDATA", ".")) / "School To Do"
DEFAULT_ESTIMATES = {"assignment": 120, "quiz": 45, "exam": 0, "lab": 120, "exercise": 60, "prep": 60}


@dataclass
class SchoolTask:
    key: str            # School To Do's canonical key (original "date|course|kind|title")
    due_date: str
    course: str
    kind: str
    title: str
    minutes: int
    due_time: str       # "HH:MM", 24-hour
    done: bool
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def due(self) -> datetime:
        return datetime.combine(date.fromisoformat(self.due_date), time.fromisoformat(self.due_time))


class SchoolSourceError(RuntimeError):
    pass


SYNC_FILES = ("app.py", "completed.json", "manual_tasks.json", "task_overrides.json", "task_details.json")


def sync_dir() -> Path:
    """Where a server copy of Compass keeps School To Do files pushed from the PC that runs it."""
    import core
    return core.DATA_DIR / "school_sync"


def _paths() -> tuple[Path, Path]:
    """Explicit overrides first, then a synced copy (Compass running on another computer), then
    the School To Do app next to Compass on this computer."""
    if os.getenv("SCHOOL_TODO_APP") or os.getenv("SCHOOL_TODO_DATA"):
        return Path(os.getenv("SCHOOL_TODO_APP", DEFAULT_APP)), Path(os.getenv("SCHOOL_TODO_DATA", DEFAULT_DATA))
    synced = sync_dir()
    if (synced / "app.py").exists():
        return synced / "app.py", synced
    return DEFAULT_APP, DEFAULT_DATA


def receive_sync(files: dict[str, str]) -> list[str]:
    """Store School To Do files pushed from another computer (only the known file names, JSON
    files must parse). Written to a temp name then swapped in, so a reader never sees half a file."""
    unknown = sorted(set(files) - set(SYNC_FILES))
    if unknown:
        raise SchoolSourceError(f"Unexpected file(s): {', '.join(unknown)}")
    if "app.py" not in files:
        raise SchoolSourceError("app.py is required.")
    for name, text in files.items():
        if len(text) > 4_000_000:
            raise SchoolSourceError(f"{name} is too large.")
        if name.endswith(".json"):
            try:
                json.loads(text)
            except ValueError as error:
                raise SchoolSourceError(f"{name} isn't valid JSON.") from error
    folder = sync_dir()
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        temp = folder / f".{name}.tmp"
        temp.write_text(text, encoding="utf-8")
        temp.replace(folder / name)
    for name in set(SYNC_FILES) - set(files) - {"app.py"}:  # deleted on the PC -> deleted here
        (folder / name).unlink(missing_ok=True)
    (folder / "received_at.txt").write_text(datetime.now().astimezone().isoformat(timespec="seconds"), encoding="utf-8")
    return sorted(files)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def built_in_items(app_path: Path) -> tuple[list[tuple[str, str, str, str]], dict[str, int]]:
    """Reproduce School To Do's ITEMS and ESTIMATE_MINUTES from its source without running the app."""
    try:
        tree = ast.parse(app_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as error:
        raise SchoolSourceError(f"Couldn't read School To Do at {app_path.name}.") from error
    namespace: dict[str, Any] = {"__builtins__": {"range": range, "tuple": tuple, "len": len, "list": list, "str": str},
                                 "date": date, "timedelta": timedelta}
    wanted = {"RAW", "ITEMS", "ESTIMATE_MINUTES"}
    for node in tree.body:
        targets = [t.id for t in getattr(node, "targets", []) if isinstance(t, ast.Name)]
        is_items_loop = isinstance(node, ast.For) and "ITEMS.append" in ast.unparse(node)
        if (isinstance(node, ast.Assign) and set(targets) & wanted) or is_items_loop:
            try:
                exec(compile(ast.Module(body=[node], type_ignores=[]), str(app_path), "exec"), namespace)  # noqa: S102 — sandboxed, see docstring
            except Exception as error:  # noqa: BLE001
                raise SchoolSourceError("School To Do's task list has a format Compass doesn't understand yet.") from error
    items = [tuple(item) for item in namespace.get("ITEMS", []) if len(item) == 4]
    estimates = {**DEFAULT_ESTIMATES, **namespace.get("ESTIMATE_MINUTES", {})}
    return items, estimates


def _clock(text: str | None) -> str | None:
    """'2:40 PM' / '14:40' -> '14:40'."""
    if not text:
        return None
    text = str(text).strip().upper()
    match = re.match(r"^(\d{1,2}):(\d{2})\s*(AM|PM)?$", text)
    if not match:
        return None
    hour, minute, half = int(match.group(1)), int(match.group(2)), match.group(3)
    if half == "PM" and hour != 12:
        hour += 12
    if half == "AM" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}" if hour < 24 and minute < 60 else None


def load_tasks() -> list[SchoolTask]:
    """All visible School To Do tasks with School To Do's own rules for edits, estimates and due times."""
    app_path, data_dir = _paths()
    items, estimates = built_in_items(app_path)
    manual = [tuple(item) for item in _read_json(data_dir / "manual_tasks.json", []) if isinstance(item, list) and len(item) == 4]
    overrides = _read_json(data_dir / "task_overrides.json", {})
    edits = {key: tuple(value) for key, value in (overrides.get("edits") or {}).items() if isinstance(value, list) and len(value) == 4}
    hidden = set(overrides.get("hidden") or [])
    details = _read_json(data_dir / "task_details.json", {})
    completed = set(_read_json(data_dir / "completed.json", []))
    manual_set = set(manual)
    tasks = []
    for original in list(items) + manual:
        key = "|".join(original)
        if key in hidden:
            continue
        shown = edits.get(key, original)
        due_date, course, kind, title = shown
        try:
            date.fromisoformat(due_date)
        except ValueError:
            continue
        detail = details.get(key, {}) if isinstance(details.get(key), dict) else {}
        minutes = int(detail.get("minutes", estimates.get(kind, 60)) or 0)
        default_time = "23:59" if original in manual_set else ("14:40" if kind in ("lab", "quiz") and course == "CSE 231" else "23:59")
        tasks.append(SchoolTask(
            key=key, due_date=due_date, course=course, kind=kind, title=title, minutes=minutes,
            due_time=_clock(detail.get("time")) or default_time, done="|".join(shown) in completed or key in completed,
            notes=str(detail.get("notes") or ""),
        ))
    tasks.sort(key=lambda task: (task.due, task.course, task.title))
    return tasks


def status() -> dict[str, Any]:
    app_path, data_dir = _paths()
    try:
        tasks = load_tasks()
    except SchoolSourceError as error:
        return {"connected": False, "message": str(error), "app_found": app_path.exists(), "data_found": data_dir.exists()}
    today = date.today().isoformat()
    open_tasks = [task for task in tasks if not task.done and task.kind != "exam"]
    return {
        "connected": True, "app_found": True, "data_found": data_dir.exists(), "total": len(tasks),
        "open": len(open_tasks), "overdue": sum(task.due_date < today for task in open_tasks),
        "open_minutes": sum(task.minutes for task in open_tasks if task.due_date >= today),
    }
