# Jarvis personality install for Windows via Tailscale — 2026-08-09
$ErrorActionPreference = "Stop"
$src = "http://100.119.3.18:8766/personality.md"
$dst1 = "$env:USERPROFILE\.config\opencode\personality.md"
$dst2 = "$env:APPDATA\opencode\personality.md"
Write-Host "=== Jarvis personality install ===" -ForegroundColor Cyan
mkdir -Force (Split-Path $dst1) | Out-Null
Write-Host "[1] Fetch $src -> $dst1"
irm $src -OutFile $dst1
Write-Host "[2] Also copy to $dst2 if needed"
mkdir -Force (Split-Path $dst2) | Out-Null
Copy-Item $dst1 $dst2 -Force -ErrorAction SilentlyContinue
Write-Host "[3] Verify"
Get-Content $dst1 | Select-Object -First 5
Write-Host "Done — restart opencode to load Jarvis" -ForegroundColor Green
# Also check Taildrop inbox
$inbox = "$env:LOCALAPPDATA\Tailscale\Files"
if (Test-Path $inbox) { Write-Host "Taildrop inbox: $inbox"; dir $inbox | ft Name,Length }
