@echo off
setlocal
cd /d "%~dp0"
set UV_LINK_MODE=copy

if not exist .venv\Scripts\python.exe (
  echo [1/3] Creating virtual environment...
  where uv >nul 2>nul
  if %errorlevel%==0 (
    uv venv --python 3.12 .venv
  ) else (
    py -3.12 -m venv .venv
  )
)

call .venv\Scripts\activate.bat

echo [2/3] Checking and installing dependencies...
where uv >nul 2>nul
if %errorlevel%==0 (
  uv pip install -r requirements.txt --link-mode=copy
) else (
  python -m ensurepip --upgrade
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt
)
if errorlevel 1 goto :failed

echo [3/3] Starting Multi-model Live Caption...
python app.py
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo Installation or startup failed.
echo Please copy the messages above when reporting the problem.
pause
exit /b 1
