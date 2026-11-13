@echo off
cd /d "%~dp0"
echo Starting Health Hub at http://127.0.0.1:8765
echo Keep this window open while you use the app.
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8765
pause
