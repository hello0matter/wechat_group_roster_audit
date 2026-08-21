$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pywechat = if ($env:PYWECHAT2_ROOT) { $env:PYWECHAT2_ROOT } else { "D:\tmp\anjian\pj\st\tmp\pywechat2" }
$python = Join-Path $pywechat ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "D:\Program Files\Python\Python311\python.exe"
}
& $python -m pip install pyinstaller
& $python -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin --name WechatRosterGUI (Join-Path $root "wechat_gui.py")
Write-Host "Built: $root\dist\WechatRosterGUI.exe"
Remove-Item (Join-Path $root "build") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $root "dist") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $root "WechatRosterGUI.spec") -Force -ErrorAction SilentlyContinue
