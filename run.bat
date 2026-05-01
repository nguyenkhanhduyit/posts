@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

if not exist "app\.venv" (
  echo [setup] Creating venv...
  py -3 -m venv app\.venv
)

call "app\.venv\Scripts\activate.bat"

echo [setup] Upgrading pip...
python -m pip install --upgrade pip

echo [setup] Installing Python dependencies...
pip install -r app\requirements.txt --no-cache-dir --default-timeout 120

echo [setup] Installing Playwright Chromium (first run only)...
python -m playwright install chromium

if not exist "app\.env" (
  echo [setup] .env not found. Creating from .env.example...
  copy "app\.env.example" "app\.env" >nul
  echo [setup] Please edit .env and set FB_EMAIL + FB_PASSWORD.
)

echo.
echo [run] Starting backend + worker in ONE window...
echo [run] Tip: set WORKER_COUNT=2 (or 3) to run keywords in parallel.
python -m app.utils.supervisor
pause

