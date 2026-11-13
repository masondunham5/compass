@echo off
setlocal
cd /d "%~dp0"
set "PYTHON=.venv\Scripts\pythonw.exe"
if not exist "%PYTHON%" set "PYTHON=..\.venv\Scripts\pythonw.exe"
if not exist "%PYTHON%" (
    echo Health Hub could not find Python. Run the diagnostic launcher for setup instructions.
    pause
    exit /b 1
)
start "Health Hub" /b "%PYTHON%" -m uvicorn app:app --host 127.0.0.1 --port 8765 --log-level warning
timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:8765
