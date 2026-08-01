@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Please run run.bat once before audio diagnosis.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
(
  echo MiMo Live Caption audio diagnosis
  echo Generated: %date% %time%
  echo.
  python -m pyaudiowpatch
) > audio_devices.txt 2>&1

echo Audio device information was saved to:
echo %CD%\audio_devices.txt
notepad audio_devices.txt
endlocal
