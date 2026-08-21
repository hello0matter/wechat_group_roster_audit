$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pywechat = if ($env:PYWECHAT2_ROOT) { $env:PYWECHAT2_ROOT } else { "D:\tmp\anjian\pj\st\tmp\pywechat2" }
$python = Join-Path $pywechat ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "pywechat2 venv not found: $python" }
& $python -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin --name WechatRosterGUI (Join-Path $root "wechat_gui.py")
& $python -m PyInstaller --noconfirm --clean --onefile --console --uac-admin --name wechat_backup_runner `
    --collect-all pyautogui --collect-all pywinauto --collect-all pycaw `
    --collect-all sounddevice --collect-all soundfile --collect-all emoji `
    --collect-all markdownify --collect-all bs4 --collect-all packaging `
    --collect-all psutil --collect-all PIL --hidden-import wx --hidden-import open_group `
    --hidden-import quick_capture --hidden-import wechat_group_roster_audit (Join-Path $root "backup_runner.py")
$dist = Join-Path $root "portable"
New-Item -ItemType Directory -Force $dist | Out-Null
Copy-Item (Join-Path $root "dist\WechatRosterGUI.exe") $dist -Force
Copy-Item (Join-Path $root "dist\wechat_backup_runner.exe") $dist -Force
$source = Join-Path $dist "pywechat2"
New-Item -ItemType Directory -Force $source | Out-Null
robocopy $pywechat $source /E /XD .venv __pycache__ build dist artifacts | Out-Null
Write-Host "Portable bundle: $dist"
Remove-Item (Join-Path $root "build") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $root "dist") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $root "WechatRosterGUI.spec") -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $dist "uia_backup_runner.exe") -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $dist "wx_ocr_runner.exe") -Force -ErrorAction SilentlyContinue
