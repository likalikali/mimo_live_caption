@echo off
setlocal
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
  echo Please run run.bat first to create the Python environment.
  pause
  exit /b 1
)

rem Usage:
rem   install_streaming_model.bat [proxy] [accurate^|fast]
rem Examples:
rem   install_streaming_model.bat http://127.0.0.1:20800 accurate
rem   install_streaming_model.bat socks5h://127.0.0.1:20800 fast
set "PROXY_URL=%~1"
set "MODEL_KIND=%~2"
if "%PROXY_URL%"=="" set "PROXY_URL=%MIMO_DOWNLOAD_PROXY%"
if "%PROXY_URL%"=="" set "PROXY_URL=http://127.0.0.1:20800"
if "%MODEL_KIND%"=="" set "MODEL_KIND=accurate"

echo Project folder: %~dp0
echo Model folder:   %~dp0models
echo Model kind:     %MODEL_KIND%
echo Proxy:          %PROXY_URL%
echo.

.venv\Scripts\python.exe download_streaming_model.py --model "%MODEL_KIND%" --proxy "%PROXY_URL%"
if errorlevel 1 (
  echo.
  echo Installation failed. Check whether the proxy protocol and port are correct.
  echo HTTP example:   http://127.0.0.1:20800
  echo SOCKS5 example: socks5h://127.0.0.1:20800
) else (
  echo.
  echo The local streaming model is ready in the project models folder.
  echo Restart the application. It will prefer the accurate model automatically.
)
pause
endlocal
