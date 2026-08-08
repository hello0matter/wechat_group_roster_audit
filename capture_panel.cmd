@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=D:\Program Files\Python\Python311\python.exe"
if exist "%PYTHON%" goto run

where py >nul 2>nul
if errorlevel 1 (
  echo Python 3 was not found.
  pause
  exit /b 1
)
set "PYTHON="

:run
if defined PYTHON (
  "%PYTHON%" quick_capture.py
) else (
  py -3 quick_capture.py
)
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" (
  echo Capture completed. The PNG is in the artifacts folder.
) else (
  echo Capture failed with exit code %EXIT_CODE%.
)
pause
exit /b %EXIT_CODE%
