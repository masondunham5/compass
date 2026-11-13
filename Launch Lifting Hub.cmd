@echo off
cd /d "%~dp0"
start "Health Hub" /b ".venv\Scripts\pythonw.exe" -m uvicorn app:app --host 127.0.0.1 --port 8765 --log-level warning
timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:8765
