#!/usr/bin/env python3
"""
aider_parallel_orchestrator.py — 8 aider in parallelo, coordinati, non si pestano i piedi.
- N workers (default 8) -> 2100 task in ~12h (vs 105h sequenziale)
- Coda condivisa TASK_QUEUE.jsonl con flock (fcntl) atomico -> mai 2 worker sullo stesso task
- Ogni worker marca task "running" con worker_id/pid/ts, poi "done/failed"
- Conflict avoidance: legge running tasks degli altri, salta task che toccano stessi file
- Legge handoffs recenti per non duplicare lavoro (intelligenza)
- Rate limit: jitter 5-15s tra claim, backoff 90s su 429
- Se worker crasha, task "running" >30min viene resettato a pending
- Log per worker: long_tasks/aider_parallel/worker_<id>.log + status condiviso
"""
import json, time, sys, os, fcntl, traceback, random, subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta
from multiprocessing import Process

HOME = Path.home()
QUEUE = HOME / "agent_workspace/opsdeck/tasks/TASK_QUEUE.jsonl"
STATUS_DIR = HOME / "agent_workspace/long_tasks/aider_parallel"
STATUS_FILE = STATUS_DIR / "status.json"
LOCK_FILE = HOME / "agent_workspace/opsdeck/tasks/.queue.lock"
LOG_DIR = HOME / "agent_workspace/subagent_logs"

sys.path.insert(0, str(HOME / "agent_workspace/memory_vault/60_tools/python_library"))
import aider_sequential_orchestrator as orch

def now(): return datetime.now(timezone.utc).isoformat()
def log(worker_id, msg):
    line=f"[{now()}][W{worker_id}] {msg}"
    print(line, flush=True)
    try:
        STATUS_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATUS_DIR / f"worker_{worker_id}.log","a") as f: f.write(line+"\n")
        with open(STATUS_DIR / "combined.log","a") as f: f.write(line+"\n")
    except: pass

def load_queue_locked():
    recs=[]
    for line in open(QUEUE):
        line=line.strip()
        if line:
            try: recs.append(json.loads(line))
            except: pass
    return recs

def save_queue_locked(recs):
    tmp=QUEUE.with_suffix(".tmp")
    with open(tmp,"w") as f:
        for r in recs: f.write(json.dumps(r, ensure_ascii=False)+"\n")
    tmp.replace(QUEUE)

def claim_task(worker_id):
    # flock esclusivo
    LOCK_FILE.touch(exist_ok=True)
    with open(LOCK_FILE,"r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            recs=load_queue_locked()
            # reset stuck running >30min
            now_dt=datetime.now(timezone.utc)
            for r in recs:
                if r.get("status")=="running":
                    ts=r.get("claimed_utc")
                    try:
                        claimed=datetime.fromisoformat(ts.replace("Z","+00:00")) if ts else now_dt
                    except: claimed=now_dt
                    if now_dt - claimed > timedelta(minutes=30):
                        log(worker_id, f"reset stuck {r['id']} running >30min -> pending")
                        r["status"]="pending"
            # raccogli file occupati da running
            occupied=set()
            for r in recs:
                if r.get("status")=="running":
                    for f in r.get("files",[]): occupied.add(f)
                    occupied.add(f"handoff_{r['id']}.md")
            # cerca primo pending non in conflitto
            target=None
            for r in recs:
                if r.get("status")=="pending" or (r.get("status")=="failed" and r.get("attempts",0)<3):
                    # conflitto file?
                    my_files=set(r.get("files",[])) | {f"handoff_{r['id']}.md"}
                    if my_files & occupied:
                        continue  # salta, file occupati da altro worker
                    # anche check handoffs recenti: se altro worker ha appena fatto task con stesso prefisso titolo, evita
                    target=r
                    break
            if not target:
                # se tutti in conflitto, prendi primo pending comunque (meglio che idle)
                for r in recs:
                    if r.get("status")=="pending":
                        target=r
                        break
            if target:
                target["status"]="running"
                target["claimed_utc"]=now()
                target["worker_id"]=worker_id
                target["claimed_pid"]=os.getpid()
                save_queue_locked(recs)
                fcntl.flock(lock, fcntl.LOCK_UN)
                return target
            fcntl.flock(lock, fcntl.LOCK_UN)
            return None
        except Exception as e:
            try: fcntl.flock(lock, fcntl.LOCK_UN)
            except: pass
            log(worker_id, f"claim error {e} {traceback.format_exc()}")
            return None

def mark_done(worker_id, tid, ok, handoff=""):
    LOCK_FILE.touch(exist_ok=True)
    with open(LOCK_FILE,"r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            recs=load_queue_locked()
            for r in recs:
                if r["id"]==tid:
                    if ok:
                        r["status"]="done"
                    else:
                        # se 429, rimetti pending per retry, altrimenti failed
                        r["status"]="failed" if r.get("attempts",0)>=2 else "failed"
                    r["attempts"]=r.get("attempts",0)+1
                    if handoff: r["last_handoff"]=handoff
                    r["updated_utc"]=now()
                    r.pop("claimed_utc",None)
                    r.pop("worker_id",None)
                    r.pop("claimed_pid",None)
                    break
            save_queue_locked(recs)
            fcntl.flock(lock, fcntl.LOCK_UN)
        except Exception as e:
            try: fcntl.flock(lock, fcntl.LOCK_UN)
            except: pass
            log(worker_id, f"mark error {e}")

def worker_loop(worker_id):
    log(worker_id, f"START worker {worker_id} PID {os.getpid()} model {orch.DEFAULT_MODEL}")
    consecutive_fails=0
    # jitter iniziale per non partire tutti insieme
    time.sleep(random.uniform(2, 10) + worker_id*2)
    while True:
        try:
            rec=claim_task(worker_id)
            if not rec:
                # nessun pending, controlla ogni 30s
                pending=sum(1 for r in load_queue_locked() if r.get("status")=="pending")
                done=sum(1 for r in load_queue_locked() if r.get("status")=="done")
                log(worker_id, f"idle no claim pending:{pending} done:{done} sleep 20")
                # aggiorna status condiviso
                try:
                    STATUS_DIR.mkdir(parents=True, exist_ok=True)
                    status={"worker_id":worker_id,"state":"idle","pending":pending,"done":done,"ts":now(),"pid":os.getpid()}
                    with open(STATUS_DIR / f"worker_{worker_id}.status.json","w") as f: json.dump(status,f)
                except: pass
                time.sleep(20)
                # se coda finita per tutti, esci? No, aspetta 60 e ricontrolla (mai fermarsi)
                if pending==0:
                    time.sleep(40)
                continue
            tid=rec["id"]; title=rec["title"][:70]
            log(worker_id, f">>> CLAIM {tid} :: {title}")
            # status condiviso running
            try:
                with open(STATUS_DIR / f"worker_{worker_id}.status.json","w") as f:
                    json.dump({"worker_id":worker_id,"state":"running","current":tid,"title":title,"ts":now(),"pid":os.getpid()},f)
                # aggregato
                agg={}
                for i in range(8):
                    p=STATUS_DIR / f"worker_{i}.status.json"
                    if p.exists():
                        try: agg[f"W{i}"]=json.loads(p.read_text())
                        except: pass
                with open(STATUS_FILE,"w") as f: json.dump({"workers":agg,"pending":sum(1 for r in load_queue_locked() if r.get("status")=="pending"),"done":sum(1 for r in load_queue_locked() if r.get("status")=="done"),"ts":now()},f, indent=2)
            except: pass
            ok=False
            handoff=str(HOME / f"agent_workspace/opsdeck/tasks/handoff_{tid}.md")
            try:
                ok=orch.run_aider_for_task(rec, dry_run=False, timeout=900)
            except Exception as e:
                log(worker_id, f"EXCEPTION aider {tid}: {e}\n{traceback.format_exc()}")
                ok=False
            # check 429 nel log
            tail=""
            try:
                logs=sorted(LOG_DIR.glob(f"aider_{tid}_*.log"), key=lambda p: p.stat().st_mtime)[-1:]
                if logs: tail=open(logs[0]).read()[-3000:]
            except: pass
            is_rate="429" in tail or "RESOURCE_EXHAUSTED" in tail or "rate limit" in tail.lower()
            if is_rate:
                log(worker_id, f"⏳ {tid} RATE LIMIT -> rimetto pending sleep 90")
                # rimetti pending
                with open(LOCK_FILE,"r+") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    recs=load_queue_locked()
                    for r in recs:
                        if r["id"]==tid:
                            r["status"]="pending"
                            r.pop("claimed_utc",None)
                            break
                    save_queue_locked(recs)
                    fcntl.flock(lock, fcntl.LOCK_UN)
                time.sleep(90)
                consecutive_fails+=1
            else:
                if ok:
                    log(worker_id, f"✅ {tid} DONE")
                    mark_done(worker_id, tid, True, handoff)
                    consecutive_fails=0
                else:
                    log(worker_id, f"❌ {tid} FAILED -> continuo")
                    mark_done(worker_id, tid, False, handoff)
                    consecutive_fails+=1
                    if consecutive_fails>=3:
                        log(worker_id, f"3 fail di fila -> sleep 20 per non hammerare")
                        time.sleep(20)
                        consecutive_fails=0
            # jitter tra task
            time.sleep(random.uniform(3,8))
        except KeyboardInterrupt:
            log(worker_id, "KeyboardInterrupt exit")
            break
        except Exception as e:
            log(worker_id, f"LOOP EXCEPTION {e}\n{traceback.format_exc()}")
            time.sleep(10)

def main():
    import argparse
    ap=argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8, help="num parallel aider")
    ap.add_argument("--dry-run", action="store_true")
    args=ap.parse_args()
    if args.dry_run:
        print(f"DRY {args.workers} workers would claim")
        for i in range(min(3, args.workers)):
            r=claim_task(f"dry{i}")
            print(f"dry{i} claim {r['id'] if r else None}")
            if r:
                # rollback
                with open(LOCK_FILE,"r+") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    recs=load_queue_locked()
                    for rec in recs:
                        if rec["id"]==r["id"]:
                            rec["status"]="pending"
                            rec.pop("claimed_utc",None)
                            break
                    save_queue_locked(recs)
                    fcntl.flock(lock, fcntl.LOCK_UN)
        return
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    # pulisci vecchi status
    for p in STATUS_DIR.glob("worker_*.status.json"):
        try: p.unlink()
        except: pass
    procs=[]
    for i in range(args.workers):
        p=Process(target=worker_loop, args=(i,))
        p.start()
        procs.append(p)
        log("COORD", f"spawned W{i} PID {p.pid}")
        time.sleep(1)
    log("COORD", f"ALL {args.workers} workers running. CTRL-C to stop. Monitoring combined.log")
    try:
        while True:
            time.sleep(30)
            # heartbeat
            alive=sum(1 for p in procs if p.is_alive())
            pending=sum(1 for r in load_queue_locked() if r.get("status")=="pending")
            done=sum(1 for r in load_queue_locked() if r.get("status")=="done")
            running=sum(1 for r in load_queue_locked() if r.get("status")=="running")
            log("COORD", f"heartbeat alive:{alive}/{args.workers} pending:{pending} running:{running} done:{done}")
            # restart dead workers
            for idx, p in enumerate(procs):
                if not p.is_alive():
                    log("COORD", f"W{idx} DEAD -> respawn")
                    np=Process(target=worker_loop, args=(idx,))
                    np.start()
                    procs[idx]=np
            if pending==0 and running==0:
                log("COORD", "QUEUE EMPTY — all done. Sleep 60 and re-check (non si ferma mai)")
                time.sleep(60)
    except KeyboardInterrupt:
        log("COORD", "stopping all workers")
        for p in procs: p.terminate()

if __name__=="__main__":
    main()
