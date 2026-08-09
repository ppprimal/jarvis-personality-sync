# jarvis-personality-sync
Sync Jarvis personality Mac -> Windows via GitHub raw (bypassa firewall/Tailscale AUTH).
Mac: push personality.md -> git push
Windows: irm https://raw.githubusercontent.com/ppprimal/jarvis-personality-sync/main/install.ps1 | iex
Taildrop fallback: C:\\Users\\hp\\AppData\\Local\\Tailscale\\Files\\personality.md -> move to %USERPROFILE%\\.config\\opencode\\personality.md
