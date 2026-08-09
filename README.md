# jarvis-personality-sync
Sync Jarvis personality Mac -> Windows via GitHub raw.
Mac: personality.md 236 lines — Carpati 6 + tabella obbligatoria INSTALLATE vs SECOND BRAIN UTILIZZATE
Windows: irm https://raw.githubusercontent.com/ppprimal/jarvis-personality-sync/main/install.ps1 | iex
Verifica: Get-Content $env:USERPROFILE\\.config\\opencode\\personality.md | Select-Object -First 5 ; (Get-Content $env:USERPROFILE\\.config\\opencode\\personality.md | Measure-Object -Line).Lines
Taildrop: C:\\Users\\hp\\AppData\\Local\\Tailscale\\Files\\personality.md
