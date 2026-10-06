# Starts SmartSwap (if needed) and runs one bounded demo trial: CPU contention with the adaptive policy.
$ErrorActionPreference = 'Stop'
$base = 'http://127.0.0.1:8765'
$health = $null
try { $health = Invoke-RestMethod "$base/api/health" -TimeoutSec 2 } catch { }
if (-not $health) {
  Start-Process powershell -ArgumentList '-NoProfile', '-File', "$PSScriptRoot\start.ps1" -WindowStyle Minimized
  Start-Sleep -Seconds 4
}
$run = Invoke-RestMethod "$base/api/experiments" -Method Post -ContentType 'application/json' -Body '{"mode":"optimized","preset":"cpu","policy":"adaptive"}'
Write-Host "Trial $($run.run_id) started (about 10 s). Open $base/#experiment to see the controller timeline and action ledger."
Start-Process "$base/#experiment"
