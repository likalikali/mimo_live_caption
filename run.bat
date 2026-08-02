@echo off
setlocal
cd /d "%~dp0"

echo [1/3] Preparing Python environment...
if not exist .venv\Scripts\python.exe (
  py -3.12 -m venv .venv
  if errorlevel 1 goto :fail
)

if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat

echo [2/3] Checking and installing dependencies...
where uv >nul 2>nul
if not errorlevel 1 (
  uv pip install --python .venv\Scripts\python.exe -r requirements.txt
) else (
  .venv\Scripts\python.exe -m ensurepip --upgrade >nul 2>nul
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)
if errorlevel 1 goto :fail

echo [3/3] Starting MiMo Live Caption...
.venv\Scripts\python.exe app.py
if errorlevel 1 goto :fail
endlocal
exit /b 0

:fail
echo.
echo Installation or startup failed.
echo Please copy the messages above when reporting the problem.
pause
endlocal
exit /b 1
