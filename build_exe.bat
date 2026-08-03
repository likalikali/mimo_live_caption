@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Please run run.bat once first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
pyinstaller --noconfirm --clean --onefile --windowed ^
  --name MultiModelLiveCaption ^
  --collect-all keyring ^
  --collect-all pyaudiowpatch ^
  --collect-all sherpa_onnx ^
  --collect-all google.cloud.speech_v2 ^
  --collect-all google.auth ^
  --collect-all grpc ^
  --hidden-import websocket ^
  app.py
pause
