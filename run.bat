@echo off
setlocal EnableExtensions
cd /d "%~dp0"
echo [1/3] Preparing Python environment...

if not exist ".venv\Scripts\python.exe" (
  py -3.12 -m venv .venv
  if errorlevel 1 goto :fail
)

set "PY=.venv\Scripts\python.exe"

if not exist ".deps_060_ok" (
  echo [2/3] Checking and installing dependencies...
  where uv >nul 2>nul
  if not errorlevel 1 (
    uv pip install --python "%PY%" --link-mode copy -r requirements.txt
  ) else (
    "%PY%" -m pip --version >nul 2>nul
    if errorlevel 1 "%PY%" -m ensurepip --upgrade
    "%PY%" -m pip install --upgrade pip
    "%PY%" -m pip install -r requirements.txt
  )
  if errorlevel 1 goto :fail
  type nul > .deps_060_ok
) else (
  echo [2/3] Dependencies ready.
)

echo [3/3] Starting Adaptive Multi-model Live Caption...
"%PY%" app.py
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo Installation or startup failed.
echo Log: %%APPDATA%%\MiMoLiveCaption\app.log
pause
exit /b 1
