@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

REM Default: use CNN (deep) negative-only classifier on CPU (requires Python 3.10 venv + onnxruntime).
if not defined POST_CLASSIFIER_ENGINE set "POST_CLASSIFIER_ENGINE=deep"

REM Ensure Python 3.10 launcher exists (needed for numpy/onnxruntime wheels on Windows).
py -3.10 -V >nul 2>nul
if errorlevel 1 (
  echo [setup] ERROR: Python 3.10 launcher not found.
  echo Install Python 3.10 x64 and ensure `py -3.10` works, then rerun run.bat.
  pause
  exit /b 2
)

REM Create venv if missing OR if venv is not Python 3.10 (common issue: Python 3.13 venv breaks deep deps).
set "NEED_VENV=0"
if not exist "app\.venv\Scripts\python.exe" set "NEED_VENV=1"
if exist "app\.venv\Scripts\python.exe" (
  for /f "usebackq delims=" %%V in (`"app\.venv\Scripts\python.exe" -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"`) do set "VENV_PY=%%V"
  if /I not "!VENV_PY!"=="3.10" (
    echo [setup] Detected venv Python !VENV_PY! ^(need 3.10 for CNN deps^). Recreating venv...
    rmdir /s /q "app\.venv" 2>nul
    set "NEED_VENV=1"
  )
)

if "!NEED_VENV!"=="1" (
  echo [setup] Creating venv with Python 3.10...
  rmdir /s /q "app\.venv" 2>nul
  py -3.10 -m venv app\.venv
)

call "app\.venv\Scripts\activate.bat"

echo [setup] Upgrading pip...
python -m pip install --upgrade pip

echo [setup] Installing Python dependencies...
pip install -r app\requirements.txt --no-cache-dir --default-timeout 120

echo [setup] Installing DEEP (CNN) dependencies ^(numpy + onnxruntime^)...
pip install -r app\requirements-deep.txt --no-cache-dir --default-timeout 120

echo [setup] Installing Playwright Chromium (first run only)...
python -m playwright install chromium

echo [setup] Training post classifier from post_classifier_data (optional)...
python -m app.worker.post_classifier.train
if errorlevel 1 (
  echo [setup] Post classifier: train skipped or incomplete ^(need post_classifier_data\negative with enough images, or CNN deps failed^). Continuing.
)

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

