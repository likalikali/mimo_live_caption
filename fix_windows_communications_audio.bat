@echo off
setlocal
reg add "HKCU\Software\Microsoft\Multimedia\Audio" /v UserDuckingPreference /t REG_DWORD /d 3 /f
if errorlevel 1 (
  echo Failed to change Windows communications audio setting.
) else (
  echo Windows communications audio is now set to: Do nothing.
  echo Restart MiMo Live Caption before testing again.
)
pause
