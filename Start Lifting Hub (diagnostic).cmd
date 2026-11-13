@echo off
setlocal
cd /d "%~dp0"
echo Starting Health Hub at http://127.0.0.1:8765
echo Keep this window open while you use the app.
set "PYTHON=.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=..\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo.
    echo Python environment not found. From this folder, run:
    echo   python -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)
"%PYTHON%" -m uvicorn app:app --host 127.0.0.1 --port 8765
pause
