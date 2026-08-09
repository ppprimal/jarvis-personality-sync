# Jarvis Personality Sync

Auto-sync via MCP — no Tailscale, no firewall.

Windows:
```powershell
irm https://raw.githubusercontent.com/ppprimal/jarvis-personality-sync/main/personality.md -OutFile $env:USERPROFILE\.config\opencode\personality.md
irm https://raw.githubusercontent.com/ppprimal/jarvis-personality-sync/main/install.ps1 | iex
```

Mac:
```bash
curl -s https://raw.githubusercontent.com/ppprimal/jarvis-personality-sync/main/personality.md -o ~/.config/opencode/personality.md
```

Sync: 2026-08-09 via Jarvis
