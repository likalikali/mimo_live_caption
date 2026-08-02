@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Please run run.bat first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
pyinstaller --noconfirm --clean --onedir --windowed ^
  --name MiMoLiveCaption ^
  --collect-all keyring ^
  --collect-all pyaudiowpatch ^
  --collect-all sherpa_onnx ^
  app.py
echo.
echo Built: dist\MiMoLiveCaption\MiMoLiveCaption.exe
pause
endlocal
