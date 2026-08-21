@echo off
setlocal
cd /d "%~dp0"
set "PYTHON=D:\Program Files\Python\Python311\python.exe"
set "OUT=artifacts\debug"
if not exist "%OUT%" mkdir "%OUT%"
echo Starting administrator contact backup...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$a=@('wx.py','-m','chat','-f','-s','1','-o','artifacts\debug'); Start-Process -FilePath '%PYTHON%' -Verb RunAs -ArgumentList $a -WorkingDirectory '%CD%' -Wait"
echo.
if exist "%OUT%\result.json" (
  echo Result:
  type "%OUT%\result.json"
) else (
  echo No result.json was created. Check artifacts\debug\ for diagnostic files.
)
pause
