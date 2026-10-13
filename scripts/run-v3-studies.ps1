# Runs every v3 study design on this PC, analyses each one, and zips the results
# so they can be pooled with other machines (python -m research.pool <dirs...>).
#
#   .\scripts\run-v3-studies.ps1            # full protocol, about 5-6 hours
#   .\scripts\run-v3-studies.ps1 -Quick     # half the blocks, a pilot run
#   .\scripts\run-v3-studies.ps1 -Designs blame,ecoqos
#
# Before starting: plug in AC power, close other apps, pause OneDrive/backup
# sync, and leave the PC alone. No administrator rights are needed. The study
# opens and closes Notepad itself (realapp design); do not type into it.
param(
  [switch]$Quick,
  [string[]]$Designs = @('blame', 'attribution', 'ecoqos', 'realapp', 'real', 'hybrid', 'main')
)
$ErrorActionPreference = 'Stop'
Set-Location "$PSScriptRoot\.."
$blocks = @{ main = 10; hybrid = 8; ecoqos = 10; real = 8; realapp = 8; blame = 10; attribution = 8 }
$out = Join-Path $env:LOCALAPPDATA 'SmartSwap\results'
$tag = "$($env:COMPUTERNAME.ToLower())-$(Get-Date -Format yyyyMMdd)"
$done = @()
foreach ($design in $Designs) {
  $n = $blocks[$design]; if ($Quick) { $n = [math]::Max(3, [math]::Floor($n / 2)) }
  $id = "$tag-$design"
  Write-Host "=== $design : $n blocks -> $out\$id ==="
  python -m research.run_study --design $design --blocks $n --study-id $id --out $out
  if ($LASTEXITCODE -ne 0) { throw "study $design failed" }
  python -m research.analyze (Join-Path $out $id)
  $done += (Join-Path $out $id)
}
$zip = Join-Path $out "$tag-smartswap-v3.zip"
Compress-Archive -Path $done -DestinationPath $zip -Force
Write-Host "Done. Send this file to be pooled with other machines: $zip"
