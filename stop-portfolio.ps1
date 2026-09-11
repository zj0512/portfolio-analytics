# stop-portfolio.ps1 - stop the portfolio app
# Usage: right-click "Run with PowerShell", or in terminal:  .\stop-portfolio.ps1
$Port = 5050

$listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $listening) {
    Write-Host "[OK] not running" -ForegroundColor Yellow
    exit 0
}
$listening | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
    try {
        $p = Get-Process -Id $_ -ErrorAction Stop
        Write-Host "stopping pid $($_) ($($p.ProcessName))" -ForegroundColor Cyan
        Stop-Process -Id $_ -Force
    } catch {
        Write-Host "pid $($_) already exited" -ForegroundColor DarkGray
    }
}
Start-Sleep 1
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "[FAIL] port $Port still occupied, check manually" -ForegroundColor Red; exit 1
}
Write-Host "[OK] stopped" -ForegroundColor Green
