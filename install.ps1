# Jarvis install via GitHub raw (no Tailscale needed, bypasses macOS firewall)
$ErrorActionPreference="Stop"
$src="https://raw.githubusercontent.com/ppprimal/jarvis-personality-sync/main/personality.md"
$dst1="$env:USERPROFILE\.config\opencode\personality.md"
$dst2="$env:APPDATA\opencode\personality.md"
Write-Host "=== Jarvis via GitHub raw ===" -ForegroundColor Cyan
mkdir -Force (Split-Path $dst1) | Out-Null
irm $src -OutFile $dst1
mkdir -Force (Split-Path $dst2) | Out-Null
Copy-Item $dst1 $dst2 -Force -ErrorAction SilentlyContinue
Get-Content $dst1 | Select -First 5
Write-Host "Done" -ForegroundColor Green
# Self-update check
$inbox="$env:LOCALAPPDATA\Tailscale\Files"
if(Test-Path $inbox){dir $inbox | ft Name,Length}
