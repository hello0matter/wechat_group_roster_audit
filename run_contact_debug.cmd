@echo off
setlocal
cd /d "%~dp0"
set "PYTHONW=D:\Program Files\Python\Python311\pythonw.exe"
set "OUT=artifacts\debug"
if not exist "%OUT%" mkdir "%OUT%"
del /q "%OUT%\result.json" 2>nul
echo Starting administrator contact backup...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$a=@('wx.py','-m','chat','-f','-s','1','-o','artifacts\debug'); Start-Process -FilePath '%PYTHONW%' -Verb RunAs -ArgumentList $a -WorkingDirectory '%CD%' -WindowStyle Hidden -Wait"
echo.
if exist "%OUT%\result.json" (
  echo Result:
  type "%OUT%\result.json"
) else (
  echo No result.json was created. Check artifacts\debug\ for diagnostic files.
)
pause
