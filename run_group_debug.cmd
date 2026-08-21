@echo off
setlocal
cd /d "%~dp0"
set "PYTHONW=D:\Program Files\Python\Python311\pythonw.exe"
set "OUT=artifacts\group-debug"
set "TERMS=%~1"
if not defined TERMS set "TERMS=a"
set "PAGES=%~2"
if not defined PAGES set "PAGES=2"
set "GROUP=%~3"
if not exist "%OUT%" mkdir "%OUT%"
del /q "%OUT%\result.json" 2>nul
echo Starting opened-group member backup, terms=%TERMS%, pages=%PAGES%...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$a=@('group_member_backup.py','-M','auto','-k','%TERMS%','-s','%PAGES%','-o','artifacts\group-debug'); if('%GROUP%'){$a+=@('-g','%GROUP%')}; Start-Process -FilePath '%PYTHONW%' -Verb RunAs -ArgumentList $a -WorkingDirectory '%CD%' -WindowStyle Hidden -Wait"
echo.
if exist "%OUT%\result.json" (
  echo Result:
  type "%OUT%\result.json"
) else (
  echo No result.json was created.
)
pause
