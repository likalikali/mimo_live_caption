@echo off
setlocal
reg delete "HKCU\Software\Microsoft\Multimedia\Audio" /v UserDuckingPreference /f
if errorlevel 1 (
  echo The value was already absent or could not be removed.
) else (
  echo The custom communications audio preference was removed.
)
pause
