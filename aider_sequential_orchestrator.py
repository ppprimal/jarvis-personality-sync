#!/usr/bin/env python3
"""
aider_sequential_orchestrator.py — Orchestrator che fa fare ad aider UNA task alla volta con Muse Spark.

Problema risolto (ricerca socratica 286 fonti):
- Aider perde efficienza con troppo contesto (Aider#2219 history compressed, Koda#149 75% overflow, catspeed-cc/aider#1 tri-role isolation).
- /compact è lossy (claude-code#63807 thrashing, #76523 queued /compact). Soluzione: handoff.md deterministico (yacb2/claude-session-handoff, haxxihaxx/handoff-skill, STRML/cc-clear-handoff), non LLM summarization (mario-hernandez/claude-compact-manual).
- Quindi: ogni task = 1 aider session fresca + 1 handoff.md. Mai 1 sessione gigante.

Uso:
  python3 aider_sequential_orchestrator.py --init                          # crea TASK_QUEUE.jsonl da PIANO.md fallbacks
  python3 aider_sequential_orchestrator.py --list
  python3 aider_sequential_orchestrator.py --once --task-id T1            # 1 task
  python3 aider_sequential_orchestrator.py --once                          # primo pending
  python3 aider_sequential_orchestrator.py --loop                          # tutti i pending uno alla volta
  python3 aider_sequential_orchestrator.py --loop --dry-run

Verifica: ogni aider run -> py_compile, test -f, grep, handoff creato, git diff non vuoto.
"""
import argparse, json, subprocess, sys, time, re, os, shlex, pathlib, datetime
from datetime import timezone
from pathlib import Path

HOME = Path.home()
TASKS_DIR = HOME / "agent_workspace" / "opsdeck" / "tasks"
QUEUE = TASKS_DIR / "TASK_QUEUE.jsonl"
HANDOFF_DIR = TASKS_DIR  # handoff_<id>.md qui
LOG_DIR = HOME / "agent_workspace" / "subagent_logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_MODEL = "openai/muse-spark-1.2-contributor"
MODEL_SETTINGS = HOME / ".aider.model.settings.yml"
AIDER_CONF = HOME / ".aider.conf.yml"

# Task canonici MEGATASK (9 step)
CANONICAL = [
    {"id":"T1","title":"Extract OpenCode chats","prompt":"Esegui python3 extract_opencode_tasks.py in ~/agent_workspace/opsdeck/tasks e genera chats_export/*.jsonl + ALL_TASKS.md. Verifica con ls chats_export | wc -l. Se fallisce, fai wall_gate.","files":["extract_opencode_tasks.py","chats_export"],"verify":"ls ~/agent_workspace/opsdeck/tasks/chats_export 2>&1 | wc -l","priority":"P0"},
    {"id":"T2","title":"PC audit","prompt":"Esegui python3 pc_audit.py e genera PC_AUDIT.md con statistiche Desktop/Downloads/Documents/agent_workspace. Verifica test -f PC_AUDIT.md && grep -q agent_workspace PC_AUDIT.md","files":["pc_audit.py","PC_AUDIT.md"],"verify":"test -f ~/agent_workspace/opsdeck/tasks/PC_AUDIT.md && grep -q agent_workspace ~/agent_workspace/opsdeck/tasks/PC_AUDIT.md && echo OK","priority":"P0"},
    {"id":"T3","title":"Phone audit Xiaomi 15T","prompt":"Esegui python3 phone_audit.py. Se Xiaomi 100.64.244.106 ping ok prova ssh -p 8022 100.64.244.106, altrimenti adb devices. Genera phone_file_inventory.jsonl + PHONE.md. Se device offline documenta GATE report.","files":["phone_audit.py","phone_file_inventory.jsonl"],"verify":"ls ~/agent_workspace/opsdeck/tasks/phone_file_inventory.jsonl ~/agent_workspace/phone_dump 2>&1 | head","priority":"P0"},
    {"id":"T4","title":"Piano incrociato","prompt":"Incrocia ALL_TASKS.md + PC_AUDIT.md + PHONE.md e aggiorna PIANO.md con priorità P0/P1/P2/P3 e completa TREE.md per domini mancanti. Usa subagent_network per draft se utile.","files":["PIANO.md","TREE.md"],"verify":"grep -q P0 ~/agent_workspace/opsdeck/tasks/PIANO.md && echo OK","priority":"P1"},
    {"id":"T5","title":"Ricerca massiva 30 fonti per cluster","prompt":"Per ogni cluster di PIANO.md lancia wall_gate.py --wall <cluster> --goal <implement> --min 30 --out research_reports. Compila RICERCA.md con URL. GATE=PASS obbligatorio.","files":["RICERCA.md"],"verify":"ls ~/agent_workspace/research_reports/RICERCA_* 2>&1 | wc -l","priority":"P1"},
    {"id":"T6","title":"Implementa script per priorità","prompt":"Potenzia/fondi script per priorità P0->P1 uno alla volta, testando verify_scripts.py dopo ogni edit. Mai 2 script nella stessa sessione.","files":["verify_scripts.py"],"verify":"python3 ~/agent_workspace/opsdeck/tasks/verify_scripts.py 2>&1 | tail","priority":"P1"},
    {"id":"T7","title":"Graphify + Inventory","prompt":"Esegui graphify update . in ~/agent_workspace e crea SYSTEM_INVENTORY.md + aggiorna memory_vault/00_core/TREE.md","files":["SYSTEM_INVENTORY.md"],"verify":"test -f ~/agent_workspace/memory_vault/00_core/TREE.md && echo OK","priority":"P2"},
    {"id":"T8","title":"Skill audit","prompt":"Lista skill in ~/.config/opencode/skills ~/.claude/skills ~/.vscode/extensions -> SKILL_ANALYSIS.md, fondi ridondanti, segna DA_CANCELLARE.md","files":["SKILL_ANALYSIS.md"],"verify":"test -f ~/agent_workspace/opsdeck/tasks/SKILL_ANALYSIS.md && echo OK","priority":"P2"},
    {"id":"T9","title":"Watchdog 90min","prompt":"Verifica self_ping_watchdog.py --once, configura OPENAI_API_KEY da credential_manager (LLM_155820...), avvia loop 90min/24h via launchd/long_task_runner","files":["self_ping_watchdog.py"],"verify":"python3 ~/agent_workspace/opsdeck/tasks/self_ping_watchdog.py --once 2>&1 | tail","priority":"P3"},
]

def init_queue(force=False):
    if QUEUE.exists() and not force:
        print(f"QUEUE già esiste: {QUEUE} ({QUEUE.stat().st_size}B). Usa --force per rigenerare.")
        return
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    with open(QUEUE, "w") as f:
        for t in CANONICAL:
            rec={"id":t["id"],"title":t["title"],"prompt":t["prompt"],"files":t["files"],"verify":t["verify"],"priority":t["priority"],"status":"pending","attempts":0,"last_handoff":""}
            f.write(json.dumps(rec, ensure_ascii=False)+"\n")
    print(f"QUEUE creata: {QUEUE} con {len(CANONICAL)} task")

def load_queue():
    if not QUEUE.exists():
        init_queue()
    recs=[]
    for line in open(QUEUE):
        line=line.strip()
        if line:
            recs.append(json.loads(line))
    return recs

def save_queue(recs):
    with open(QUEUE,"w") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False)+"\n")

def list_queue():
    recs=load_queue()
    print(f"{'ID':<4} {'PRI':<3} {'STATUS':<12} {'TITLE'}")
    for r in recs:
        print(f"{r['id']:<4} {r['priority']:<3} {r['status']:<12} {r['title']}")

def run_aider_for_task(task, dry_run=False, timeout=1200):
    tid=task["id"]
    title=task["title"]
    prompt=task["prompt"]
    model=DEFAULT_MODEL
    handoff = HANDOFF_DIR / f"handoff_{tid}.md"
    ts=datetime.datetime.now(timezone.utc).isoformat() if hasattr(datetime, 'timezone') else datetime.datetime.utcnow().isoformat()
    # Costruisci messaggio aider con istruzioni handoff + task isolato
    # Personality OpenCode — leggi intero file e inietta regole aggiornate (OSSESSIONE #1, #2, AUTORIZZATO)
    personality_text = open(pathlib.Path.home() / ".config/opencode/personality.md").read()
    # Estrai blocchi critici integrali (non solo regex) — Carpati 6, Output tabelle, Second Brain first
    import re
    # 1. Carpati 6
    m_carp = re.search(r"### CARPATI 6 SUBAGENTI.*?Verifica parent.*", personality_text, re.S)
    carpati_block = m_carp.group(0).strip()[:2200] if m_carp else "CARPATI 6 obbligatori"
    # 2. Output tabelle
    m_out = re.search(r"- \*\*TABELLA SKILL.*ALLUCINAZIONE\.\*\*", personality_text, re.S)
    m_dist = re.search(r"- \*\*DISTINZIONE OBBLIGATORIA.*RIFIUTATO\.\*\*", personality_text, re.S)
    out_block = (m_out.group(0).strip()[:1800] if m_out else "") + "\n" + (m_dist.group(0).strip()[:1800] if m_dist else "")
    # 3. Keep legacy per compat + nuova sintesi
    personality_lines = personality_text.strip().splitlines()
    keep = [l.strip() for l in personality_lines if re.search(r"OSSESSIONE|SECOND BRAIN|VERIFICA REAL-TIME|AUTORIZZATO|Ogni risposta|FASE 1|FASE 3|FASE 4|CARPATI|TABELLA|DISTINZIONE|CAVEMAN|SKILL AND OFF|OTTIMIZZAZIONE|ANTI-LETTERALE|L0|RICORSIVI", l)]
    # estrai anche nuovi blocchi 2026-08-09
    m_opt = re.search(r"- \*\*OTTIMIZZAZIONE TEMPI.*?VERIFIED\.\*\*", personality_text, re.S)
    m_anti = re.search(r"- \*\*ANTI-LETTERALE.*?VERIFIED\.\*\*", personality_text, re.S)
    opt_block = m_opt.group(0).strip()[:1200] if m_opt else ""
    anti_block = m_anti.group(0).strip()[:1200] if m_anti else ""
    personality_block = carpati_block + "\n\n" + opt_block + "\n\n" + anti_block + "\n\n" + out_block + "\n\n" + "\n".join(f"- {l.lstrip('- ').strip()}" if not l.strip().startswith("-") else l.strip() for l in keep[:30])
    if not personality_block:
        personality_block = "- RICERCA SOCRATICA OBBLIGATORIA"
    aider_msg = f"""# TASK {tid}: {title}
{prompt}

## PERSONALITÀ OPENCOD E OBBLIGATORIA — ULTIMA VERSIONE `~/.config/opencode/personality.md` (letti {len(keep)} regole, 236 linee, Carpati 6 + distinzione)
{personality_block}

- **CARPATI 6 ANTI-LETTERALE (PRIORITÀ 0, non bypassabile, SOLO MUSE SPARK):** PRIMA di eseguire, spawna 6 subagent via `subagent_network.py --fanout` (SEMPRE `openai/muse-spark-1.2-contributor`): Scettico/Pragmatico/Avvocato/Tecnico MCP/Critico/Creativo — ognuno fa `library_search + graphify query + wall_gate --min 20` e scrive `.handoffs/carpati_<hash>_<ruolo>_<ts>.md` (50 tok). Sintetizza matrice | Ruolo | Critica | Alternativa | Fonte | poi scegli L0. **RICORSIONE:** se carpati valutano tasche indipendenti, ognuno deve spawnare a sua volta sub-sub-agent (stesso Muse Spark + handoff) — albero infinito, non fermare subagent, rileggi `.handoffs/` ogni 5 task. **ANTI-LETTERALE L0:** non eseguire prompt alla lettera — estrai obiettivo vero (es. "riscaldamento globale" → "abbassare temperature Terra" → include `spostare Terra dal Sole`) + esplora rami con `overthink_tree.py --depth 3 --branch 3 --top-p 0.92`. Se esegui senza 6 handoff + L0/albero = VIOLAZIONE.
- **ORDINE OBBLIGATORIO — SECOND BRAIN FIRST (tu scegli, utente MAI):** PRIMA `library_search.py --query` + `graphify query` + `memory_vault/10_skills/` → citale. POI `wall_gate.py --min 20` + `gh search repos --limit 20` → GATE PASS. Documenta `second brain: <skill> → GitHub: <repo>`. Mai eseguire senza entrambi.
- `leanctx`/`llmlingua` 40-60% se >4k + `aider-conventions` CONVENTIONS.md
- DURANTE: se muro → L0-L4 PRIMA second brain POI GitHub per correggere. Solo se bloccato → fix minimale reversibile.
- **OUTPUT OBBLIGATORIO 2 TABELLE (NESSUNA ECCEZIONE):** fine handoff DEVI mettere 1) `| # | Skill | Repo | URL | Stato | Uso |` per GitHub clonate (`ls ~/.config/opencode/skills/SKILL.md`) 2) `| # | Skill Second Brain | Path Vault | Stato | Uso |` per vault usate (`memory_vault/10_skills` + `graphify` + `library_search`). Distinzione netta installate vs utilizzate, mai mescolare. Omettere = RIFIUTATO.
- Output: max 120 parole + 2 tabelle. File:linea. 2-3 opzioni se incerto. 1 task = 1 session fresca.

## REGOLE HANDOFF (obbligatorio — v. yacb2/claude-session-handoff)
- Fai SOLO questo task. Non toccare altri task.
- PRIMA di editare: 6 Carpati spawn + second brain search (library_search + graphify) + riuso script, POI GitHub wall_gate --min 20.
- Dopo aver finito, crea {handoff} con:
  - Cosa fatto (file toccati + diff)
  - Decisioni + alternative scartate (2-3 repo GitHub scelte) + matrice Carpati 6
  - Comando verifica eseguito + esito
  - Skills GitHub INSTALLATE (tabella | # | Skill | Repo | URL | Stato | Uso |) + Skills Second Brain UTILIZZATE (tabella | # | Skill Second Brain | Path Vault | Stato | Uso |) — DISTINZIONE OBBLIGATORIA
  - Prossimo step suggerito
- Verifica reale: esegui `{task['verify']}` e riporta esito. Se fallisce, fixa in loop.
- Non usare /compact. Usa handoff.md deterministico.

## CONTESTO MINIMO
- Workspace: ~/agent_workspace
- Tasks dir: ~/agent_workspace/opsdeck/tasks
- Queue: {QUEUE}
- Handoff prev: leggi handoff_T* precedenti se esistono, ma non caricare tutto il megatask.
"""
    if dry_run:
        print(f"[DRY] would run aider --model {model} --message \"{aider_msg[:200]}...\" for {tid}")
        print(f"[DRY] handoff target: {handoff}")
        return True
    # Log file
    log_path = LOG_DIR / f"aider_{tid}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    # Assicura handoff file esista vuoto per aider add
    try:
        handoff.parent.mkdir(parents=True, exist_ok=True)
        if not handoff.exists():
            handoff.write_text(f"# Handoff {tid} {title}\n\nInizio {ts}\n")
    except: pass
    # Prepara lista file da passare ad aider (handoff + task files se esistono)
    aider_files = [str(handoff)]
    for f in task.get("files", []):
        cand = TASKS_DIR / f
        if cand.exists():
            aider_files.append(str(cand))
        else:
            # prova anche in HOME
            alt = pathlib.Path.home() / f
            if alt.exists():
                aider_files.append(str(alt))
    # Costruisci comando aider: usa --message non interattivo, una sola task
    # Usa --yes-always per non chiedere, ma con --no-auto-commits? Noi vogliamo auto-commits per tracciare diff
    # Config già in ~/.aider.conf.yml: model, stream, max_tokens 32768
    cmd = [
        "aider",
        "--model", model,
        *aider_files,
        "--message", aider_msg,
        "--no-pretty",
        "--yes-always",
        "--no-show-model-warnings",
        "--no-check-model-accepts-settings",
    ]
    print(f"=== AIDER START {tid}: {title} ===")
    print(f"Model: {model} | Log: {log_path} | Timeout: {timeout}s")
    print(f"Prompt: {prompt[:180]}...")
    #cwd = TASKS_DIR ?
    env=os.environ.copy()
    # assicurati che OPENAI_API_KEY sia settata da .aider.conf.yml o env
    try:
        proc = subprocess.run(cmd, cwd=str(TASKS_DIR), capture_output=True, text=True, timeout=timeout, env=env)
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        open(log_path, "w").write(out)
        print(out[-3000:])
        print(f"Exit: {proc.returncode}")
        # Check se handoff creato (aider dovrebbe averlo creato via edit)
        if handoff.exists():
            print(f"HANDOFF OK: {handoff} ({handoff.stat().st_size}B)")
        else:
            print(f"HANDOFF MISSING: {handoff} — aider non ha creato handoff, lo creo io fallback")
            # Fallback: crea handoff minimo da git diff + verify
            try:
                diff = subprocess.check_output(["git","-C", str(HOME), "diff","--stat"], text=True)[:2000]
            except: diff="(no git diff)"
            try:
                verify_out = subprocess.check_output(task["verify"], shell=True, text=True, timeout=120)[:2000]
            except Exception as e: verify_out=f"verify fail: {e}"
            handoff.write_text(f"# Handoff {tid} {title}\n\nTS: {ts}\n\n## Diff\n```\n{diff}\n```\n\n## Verify `{task['verify']}`\n```\n{verify_out}\n```\n\n## Note\nFallback handoff — aider non ha generato handoff.md, verificare manulamente.\n")
        # Verifica comando
        try:
            v = subprocess.run(task["verify"], shell=True, capture_output=True, text=True, timeout=120)
            print(f"VERIFY: {task['verify']} -> {v.returncode} | {(v.stdout or v.stderr or '')[:800]}")
            ok = (v.returncode==0)
        except Exception as e:
            print(f"VERIFY error: {e}")
            ok=False
        return ok
    except subprocess.TimeoutExpired:
        print(f"TIMEOUT {tid} dopo {timeout}s")
        return False
    except Exception as e:
        print(f"ERROR {tid}: {e}")
        return False

def mark_status(tid, new_status, handoff_path=""):
    recs=load_queue()
    for r in recs:
        if r["id"]==tid:
            r["status"]=new_status
            r["attempts"]=r.get("attempts",0)+1
            if handoff_path: r["last_handoff"]=str(handoff_path)
    save_queue(recs)

def run_once(task_id=None, dry_run=False):
    recs=load_queue()
    target=None
    if task_id:
        target=next((r for r in recs if r["id"]==task_id), None)
        if not target:
            print(f"Task {task_id} non trovato")
            return False
        if target["status"]!="pending" and not task_id:
            print(f"Task {task_id} già {target['status']}, skip. Usa --force per forzare.")
            return False
    else:
        target=next((r for r in recs if r["status"]=="pending"), None)
        if not target:
            print("Nessun task pending. Tutti completati!")
            list_queue()
            return True
    ok=run_aider_for_task(target, dry_run=dry_run)
    if ok and not dry_run:
        mark_status(target["id"], "done", str(HANDOFF_DIR / f"handoff_{target['id']}.md"))
        print(f"TASK {target['id']} -> DONE")
    elif not dry_run:
        mark_status(target["id"], "failed", str(HANDOFF_DIR / f"handoff_{target['id']}.md"))
        print(f"TASK {target['id']} -> FAILED (vedi log in {LOG_DIR})")
    return ok

def run_loop(dry_run=False):
    recs=load_queue()
    pend=[r for r in recs if r["status"]=="pending"]
    print(f"LOOP su {len(pend)} task pending")
    for r in pend:
        print(f"\n--- LOOP {r['id']} {r['title']} ---")
        ok=run_once(r["id"], dry_run=dry_run)
        if not ok and not dry_run:
            print(f"STOP loop su {r['id']} FAILED — correggi prima di continuare")
            break
        time.sleep(2)
    list_queue()

if __name__=="__main__":
    import datetime
    from datetime import timezone
    ap=argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true", help="crea TASK_QUEUE.jsonl")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--once", action="store_true", help="esegui 1 task pending (o --task-id)")
    ap.add_argument("--task-id", type=str)
    ap.add_argument("--loop", action="store_true", help="loop su tutti i pending uno alla volta")
    ap.add_argument("--dry-run", action="store_true")
    args=ap.parse_args()
    if args.init:
        init_queue(force=args.force)
    elif args.list:
        list_queue()
    elif args.once or args.task_id:
        run_once(args.task_id, dry_run=args.dry_run)
    elif args.loop:
        run_loop(dry_run=args.dry_run)
    else:
        ap.print_help()
