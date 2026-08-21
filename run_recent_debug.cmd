@echo off
setlocal
cd /d "%~dp0"
set "PYTHONW=D:\Program Files\Python\Python311\pythonw.exe"
set "OUT=artifacts\recent-debug"
set "LIMIT=%~1"
if not defined LIMIT set "LIMIT=10"
if not exist "%OUT%" mkdir "%OUT%"
del /q "%OUT%\result.json" 2>nul
del /q "%OUT%\recent-contact-*.png" 2>nul
echo Starting recent direct-chat contact backup, limit=%LIMIT%...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$a=@('wx.py','-r','-s','%LIMIT%','-o','artifacts\recent-debug'); Start-Process -FilePath '%PYTHONW%' -Verb RunAs -ArgumentList $a -WorkingDirectory '%CD%' -WindowStyle Hidden -Wait"
echo.
if exist "%OUT%\result.json" (
  echo Result:
  type "%OUT%\result.json"
) else (
  echo No result.json was created.
)
pause
