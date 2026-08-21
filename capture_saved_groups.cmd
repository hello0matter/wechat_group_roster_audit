@echo off
setlocal
cd /d "%~dp0"

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%I"
set "OUTPUT=artifacts\saved-groups-%STAMP%"

set "PYTHON=D:\Program Files\Python\Python311\python.exe"
if exist "%PYTHON%" (
  "%PYTHON%" wx.py -m saved -n -o "%OUTPUT%"
) else (
  where py >nul 2>nul
  if errorlevel 1 (
    echo Python 3 was not found.
    pause
    exit /b 1
  )
  py -3 wx.py -m saved -n -o "%OUTPUT%"
)
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Output: %OUTPUT%
if "%EXIT_CODE%"=="0" (
  echo Saved Groups capture completed. See result.json for the stop reason.
) else (
  echo Capture failed with exit code %EXIT_CODE%.
)
pause
exit /b %EXIT_CODE%
