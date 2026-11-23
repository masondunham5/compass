"""Send School To Do's files to Compass running on another computer (over Tailscale).

Run on the PC where you use School To Do. One-time setup (the URL and token come from
setup_server.ps1 on the server):
    ..\\.venv\\Scripts\\python.exe scripts\\push_school_todo.py --setup https://old-laptop.tailnet.ts.net TOKEN

Then a scheduled task runs it every few minutes (scripts\\register_school_push_task.ps1). It only
sends when something changed. Settings live in data\\school_push.json on this PC (ignored by Git).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "data" / "school_push.json"
APP = ROOT.parent / "school_todo" / "app.py"
DATA = Path(os.getenv("LOCALAPPDATA", ".")) / "School To Do"
JSON_FILES = ("completed.json", "manual_tasks.json", "task_overrides.json", "task_details.json")


def collect() -> dict[str, str]:
    if not APP.exists():
        raise SystemExit(f"Couldn't find School To Do at {APP}.")
    files = {"app.py": APP.read_text(encoding="utf-8")}
    for name in JSON_FILES:
        path = DATA / name
        if path.exists():
            files[name] = path.read_text(encoding="utf-8")
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description="Push School To Do files to Compass on another computer.")
    parser.add_argument("--setup", nargs=2, metavar=("URL", "TOKEN"), help="Save the server address and sync token.")
    parser.add_argument("--force", action="store_true", help="Send even if nothing changed.")
    args = parser.parse_args()
    if args.setup:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps({"url": args.setup[0].rstrip("/"), "token": args.setup[1].strip(), "last": ""}), encoding="utf-8")
        print(f"Saved. Compass server: {args.setup[0]}")
    if not CONFIG.exists():
        print("Run with --setup URL TOKEN first.")
        return 1
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    files = collect()
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    if digest == config.get("last") and not args.force:
        return 0  # nothing new
    body = json.dumps({"token": config["token"], "files": files}).encode()
    request = urllib.request.Request(f"{config['url']}/api/assistant/school/sync", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "Compass-school-push"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as error:
        print(f"Compass refused the files ({error.code}): {error.read().decode(errors='replace')[:200]}")
        return 1
    except (urllib.error.URLError, TimeoutError) as error:
        print(f"Couldn't reach Compass at {config['url']} ({error}). Is the server on and Tailscale connected?")
        return 1
    config["last"] = digest
    CONFIG.write_text(json.dumps(config), encoding="utf-8")
    print(f"Sent {', '.join(result['stored'])}. Compass now sees {result['tasks']} school tasks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
