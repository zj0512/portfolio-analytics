# start-portfolio.ps1 - start the portfolio app
# Usage: right-click "Run with PowerShell", or in terminal:  .\start-portfolio.ps1
$ErrorActionPreference = "Stop"
$Port = 5050
$Url = "http://127.0.0.1:$Port"
$Py  = "C:\Users\zc\AppData\Local\Programs\Python\Python312\python.exe"
$App = "D:\zj\portfolio_app\main.py"

# skip if already running
$listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Write-Host "[OK] already running (pid $($listening[0].OwningProcess)), skip start" -ForegroundColor Yellow
} else {
    # auto-install deps if missing
    & $Py -c "import flask, apscheduler" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "installing deps flask / apscheduler ..." -ForegroundColor Cyan
        & $Py -m pip install flask apscheduler --quiet --disable-pip-version-check
    }
    Write-Host "starting service ..." -ForegroundColor Cyan
    Start-Process $Py -ArgumentList "`"$App`"" -WorkingDirectory "D:\zj\portfolio_app" -WindowStyle Hidden
    # wait for port, up to 15s
    $ok = $false
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep 1
        if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { $ok = $true; break }
    }
    if (-not $ok) { Write-Host "[FAIL] port $Port not listening" -ForegroundColor Red; exit 1 }
    Write-Host "[OK] service started" -ForegroundColor Green
}

Start-Process $Url
Write-Host "[OK] browser opened: $Url" -ForegroundColor Green
