"""
app.py — Redis Operator backend
Flask + APScheduler + SQLite + Redis
"""

import os
import sys
import json
import time
import subprocess
import threading
import importlib.util
import traceback
import sqlite3
import socket
import signal
import atexit
from collections import deque
from datetime import datetime, date, timedelta
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
import redis

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent.resolve()
DB_PATH = BASE_DIR / "redis_operator.db"
STATIC_DIR = BASE_DIR / "static"
DB_URL = f"sqlite:///{DB_PATH}"
ENV_PATH = BASE_DIR / ".env"
TEMPLATES_DIR = BASE_DIR / "templates" / "generated"

# ---------------------------------------------------------------------------
# Template script generators
# ---------------------------------------------------------------------------
def _gen_folder_backup(cfg: dict) -> str:
    source = cfg.get("source", "")
    dest   = cfg.get("dest", "")
    keep   = int(cfg.get("keep", 3))
    return f'''import shutil, os, datetime

SOURCE = r"{source}"
DEST   = r"{dest}"
KEEP   = {keep}

def run():
    ts  = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(DEST, f"backup_{{ts}}")
    os.makedirs(DEST, exist_ok=True)
    shutil.copytree(SOURCE, dst)
    backups = sorted(
        os.path.join(DEST, d) for d in os.listdir(DEST)
        if d.startswith("backup_") and os.path.isdir(os.path.join(DEST, d))
    )
    while len(backups) > KEEP:
        shutil.rmtree(backups.pop(0))
    print(f"Backup complete: {{dst}} ({{len(backups)}}/{{KEEP}} copies)")
'''

def _gen_file_cleanup(cfg: dict) -> str:
    folder  = cfg.get("folder", "")
    pattern = cfg.get("pattern", "*.tmp")
    days    = int(cfg.get("days", 7))
    return f'''import glob, os, time

FOLDER  = r"{folder}"
PATTERN = "{pattern}"
DAYS    = {days}

def run():
    cutoff = time.time() - (DAYS * 86400)
    removed = 0
    for f in glob.glob(os.path.join(FOLDER, PATTERN)):
        if os.path.isfile(f) and os.path.getmtime(f) < cutoff:
            os.remove(f)
            removed += 1
    print(f"Cleanup: {{removed}} file(s) removed matching {{PATTERN}} older than {{DAYS}} days")
'''

def _gen_folder_watcher(cfg: dict) -> str:
    watch = cfg.get("watch", "")
    rules = cfg.get("rules", [])
    rules_dict = {r["ext"].lower(): r["dest"] for r in rules if r.get("ext") and r.get("dest")}
    return f'''import os, shutil

WATCH = r"{watch}"
RULES = {repr(rules_dict)}  # {{".ext": r"destination_folder"}}

def run():
    moved = 0
    for fname in os.listdir(WATCH):
        fpath = os.path.join(WATCH, fname)
        if not os.path.isfile(fpath):
            continue
        ext = os.path.splitext(fname)[1].lower()
        if ext in RULES:
            dest = RULES[ext]
            os.makedirs(dest, exist_ok=True)
            shutil.move(fpath, os.path.join(dest, fname))
            moved += 1
    print(f"Folder watcher: {{moved}} file(s) moved")
'''

def _gen_uptime_check(cfg: dict) -> str:
    url      = cfg.get("url", "")
    log_file = cfg.get("log_file", "")
    return f'''import urllib.request, datetime, os

URL      = "{url}"
LOG_FILE = r"{log_file}"

def run():
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        urllib.request.urlopen(URL, timeout=10)
        status = "UP"
    except Exception as e:
        status = f"DOWN: {{e}}"
    os.makedirs(os.path.dirname(os.path.abspath(LOG_FILE)), exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"[{{ts}}] {{URL}} — {{status}}\\n")
    print(f"[{{ts}}] {{URL}} — {{status}}")
    if status.startswith("DOWN"):
        raise RuntimeError(f"Site down: {{URL}} — {{status}}")
'''

def _gen_open_url(cfg: dict) -> str:
    url = cfg.get("url", "")
    return f'''import webbrowser

URL = "{url}"

def run():
    webbrowser.open(URL)
    print(f"Opened: {{URL}}")
'''

TEMPLATE_GENERATORS = {
    "folder_backup":  _gen_folder_backup,
    "file_cleanup":   _gen_file_cleanup,
    "folder_watcher": _gen_folder_watcher,
    "uptime_check":   _gen_uptime_check,
    "open_url":       _gen_open_url,
}

def _load_dotenv():
    """Load KEY=VALUE pairs from .env into os.environ (only for unset keys)."""
    if not ENV_PATH.exists():
        return
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val

# ---------------------------------------------------------------------------
# In-memory log buffer
# ---------------------------------------------------------------------------
LOG_BUFFER: deque = deque(maxlen=500)
LOG_LOCK = threading.Lock()

def add_log(level: str, message: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = {"ts": ts, "level": level, "msg": message}
    with LOG_LOCK:
        LOG_BUFFER.append(entry)

# ---------------------------------------------------------------------------
# Redis management
# ---------------------------------------------------------------------------
_redis_proc = None
_redis_client = None

def _redis_running() -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", 6379), timeout=1)
        s.close()
        return True
    except OSError:
        return False

def start_redis() -> dict:
    """
    Try to connect to a running Redis. If not found, attempt to start redis-server.
    Returns {"ok": bool, "message": str}.
    """
    global _redis_proc, _redis_client

    if _redis_running():
        add_log("INFO", "Redis already running on port 6379.")
        _redis_client = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
        return {"ok": True, "message": "Redis already running."}

    # Determine binary name
    binary = "redis-server.exe" if sys.platform == "win32" else "redis-server"

    # Check if binary is on PATH
    import shutil
    if not shutil.which(binary):
        msg = (
            f"redis-server not found on PATH.\n\n"
            f"Install Redis:\n"
            f"  Windows : https://github.com/tporadowski/redis/releases\n"
            f"            or: winget install Redis.Redis\n"
            f"  macOS   : brew install redis\n"
            f"  Ubuntu  : sudo apt install redis-server\n\n"
            f"After installing, ensure '{binary}' is on your system PATH, then restart Redis Operator."
        )
        add_log("ERROR", msg)
        return {"ok": False, "message": msg}

    try:
        _redis_proc = subprocess.Popen(
            [binary],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Wait up to 3 seconds for Redis to come up
        import time
        for _ in range(30):
            if _redis_running():
                break
            time.sleep(0.1)
        else:
            return {"ok": False, "message": "redis-server started but not responding on port 6379."}

        _redis_client = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
        add_log("INFO", f"redis-server started (pid={_redis_proc.pid}).")
        return {"ok": True, "message": f"Redis started (pid={_redis_proc.pid})."}
    except Exception as e:
        return {"ok": False, "message": f"Failed to start Redis: {e}"}

def stop_redis():
    global _redis_proc
    if _redis_proc and _redis_proc.poll() is None:
        try:
            _redis_proc.terminate()
            _redis_proc.wait(timeout=5)
            add_log("INFO", "redis-server stopped.")
        except Exception:
            _redis_proc.kill()

# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        # ── Existing tables ────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workers (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                task_path   TEXT NOT NULL,
                sched_type  TEXT NOT NULL,
                sched_value TEXT NOT NULL,
                output_dir  TEXT DEFAULT '',
                paused      INTEGER DEFAULT 0,
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL UNIQUE,
                config_json TEXT NOT NULL,
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)

        # ── V2: worker groups ──────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL UNIQUE,
                order_index INTEGER DEFAULT 0,
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)

        # Migrate workers table: add group_id if missing
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(workers)")}
        if "group_id" not in existing_cols:
            conn.execute("ALTER TABLE workers ADD COLUMN group_id INTEGER DEFAULT NULL")

        # ── V2: task chains ────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chains (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL,
                sched_type      TEXT NOT NULL,
                sched_value     TEXT NOT NULL,
                stop_on_failure INTEGER DEFAULT 1,
                paused          INTEGER DEFAULT 0,
                group_id        INTEGER DEFAULT NULL,
                created_at      TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chain_steps (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chain_id    INTEGER NOT NULL,
                order_index INTEGER NOT NULL,
                task_path   TEXT NOT NULL,
                FOREIGN KEY (chain_id) REFERENCES chains(id) ON DELETE CASCADE
            )
        """)

        # ── V2: run history (workers + chains share this table) ────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS run_history (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                worker_id    INTEGER DEFAULT NULL,
                chain_id     INTEGER DEFAULT NULL,
                triggered_at TEXT DEFAULT (datetime('now')),
                trigger_type TEXT NOT NULL,
                success      INTEGER NOT NULL,
                duration_ms  INTEGER NOT NULL,
                error_msg    TEXT DEFAULT ''
            )
        """)

        conn.commit()

# ---------------------------------------------------------------------------
# APScheduler
# ---------------------------------------------------------------------------
# Scheduler initialized lazily in create_app()
scheduler: BackgroundScheduler = None  # type: ignore

def _get_local_tz():
    """Get local timezone, falling back to UTC if unavailable."""
    try:
        from tzlocal import get_localzone
        return get_localzone()
    except Exception:
        pass
    import datetime
    return datetime.timezone.utc

def _make_triggers(sched_type: str, sched_value: str):
    """Return list of (trigger, job_id_suffix) tuples for a worker."""
    triggers = []
    if sched_type == "fixed":
        times = [t.strip() for t in sched_value.split(",") if t.strip()]
        for i, t in enumerate(times):
            h, m = t.split(":")
            triggers.append((CronTrigger(hour=int(h), minute=int(m)), f"t{i}"))
    else:  # interval
        # sched_value format: "Xh Ym" or "Xh" or "Ym"
        hours, minutes = 0, 0
        parts = sched_value.lower().replace(" ", "")
        if "h" in parts:
            idx = parts.index("h")
            hours = int(parts[:idx])
            parts = parts[idx + 1:]
        if "m" in parts:
            idx = parts.index("m")
            minutes = int(parts[:idx])
        if hours == 0 and minutes == 0:
            minutes = 5  # fallback
        triggers.append((IntervalTrigger(hours=hours, minutes=minutes), "i0"))
    return triggers

def _record_run(worker_id=None, chain_id=None, trigger_type="scheduled",
                success=True, duration_ms=0, error_msg=""):
    """Write one row to run_history."""
    with get_db() as conn:
        conn.execute(
            """INSERT INTO run_history
               (worker_id, chain_id, trigger_type, success, duration_ms, error_msg)
               VALUES (?,?,?,?,?,?)""",
            (worker_id, chain_id, trigger_type, int(success), duration_ms, error_msg),
        )
        conn.commit()

def _last_run_status(worker_id=None, chain_id=None):
    """Return 'ok', 'error', or None for the most recent run of a worker/chain."""
    with get_db() as conn:
        if worker_id is not None:
            row = conn.execute(
                "SELECT success FROM run_history WHERE worker_id=? ORDER BY id DESC LIMIT 1",
                (worker_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT success FROM run_history WHERE chain_id=? ORDER BY id DESC LIMIT 1",
                (chain_id,),
            ).fetchone()
    if row is None:
        return None
    return "ok" if row["success"] else "error"

def _task_runner(worker_id: int, task_path: str, output_dir: str,
                 trigger_type: str = "scheduled"):
    """Execute a task file. Supports .py and .bat/.sh scripts."""
    log_level = "MANUAL" if trigger_type == "manual" else "FIRE"
    add_log(log_level, f"Worker #{worker_id} fired — {os.path.basename(task_path)}")
    t0 = time.time()
    error_msg = ""
    success = False
    try:
        ext = Path(task_path).suffix.lower()
        if ext == ".py":
            spec = importlib.util.spec_from_file_location("_task", task_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if hasattr(mod, "run"):
                mod.run()
        elif ext in (".bat", ".sh", ".cmd"):
            result = subprocess.run(
                [task_path],
                capture_output=True,
                text=True,
                cwd=output_dir or os.path.dirname(task_path),
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or f"exit code {result.returncode}")
        else:
            raise ValueError(f"Unsupported file type: {ext}")
        success = True
        add_log("OK", f"Worker #{worker_id} completed — {os.path.basename(task_path)}")
    except Exception:
        tb = traceback.format_exc()
        error_msg = tb
        add_log("ERROR", f"Worker #{worker_id} FAILED — {os.path.basename(task_path)}\n{tb}")
    finally:
        duration_ms = int((time.time() - t0) * 1000)
        _record_run(worker_id=worker_id, trigger_type=trigger_type,
                    success=success, duration_ms=duration_ms, error_msg=error_msg)

def register_worker_jobs(worker_id: int, task_path: str, sched_type: str,
                         sched_value: str, output_dir: str, paused: bool = False):
    """Add APScheduler jobs for a worker. Remove existing ones first."""
    # Remove any existing jobs for this worker
    for job in scheduler.get_jobs():
        if job.id.startswith(f"w{worker_id}_"):
            scheduler.remove_job(job.id)

    if paused:
        return

    triggers = _make_triggers(sched_type, sched_value)
    for trigger, suffix in triggers:
        job_id = f"w{worker_id}_{suffix}"
        scheduler.add_job(
            _task_runner,
            trigger=trigger,
            id=job_id,
            args=[worker_id, task_path, output_dir],
            replace_existing=True,
            misfire_grace_time=60,
        )

def remove_worker_jobs(worker_id: int):
    for job in scheduler.get_jobs():
        if job.id.startswith(f"w{worker_id}_"):
            scheduler.remove_job(job.id)

def _next_fire_times(worker_id: int):
    """Return list of next fire datetimes for all jobs of this worker."""
    times = []
    for job in scheduler.get_jobs():
        if job.id.startswith(f"w{worker_id}_") and job.next_run_time:
            times.append(job.next_run_time)
    return sorted(times)

def _remaining_today_for_prefix(prefix: str) -> int:
    now = datetime.now().astimezone()
    end_of_day = datetime.now().replace(hour=23, minute=59, second=59).astimezone()
    count = 0
    for job in scheduler.get_jobs():
        if not job.id.startswith(prefix):
            continue
        t = job.next_run_time
        if t and now <= t <= end_of_day:
            count += 1
            trigger = job.trigger
            if isinstance(trigger, IntervalTrigger):
                cur = t
                while True:
                    nxt = trigger.get_next_fire_time(cur, cur)
                    if nxt and nxt <= end_of_day:
                        count += 1
                        cur = nxt
                    else:
                        break
    return count

def _remaining_today(worker_id: int) -> int:
    return _remaining_today_for_prefix(f"w{worker_id}_")

# ---------------------------------------------------------------------------
# Chain runner and scheduler
# ---------------------------------------------------------------------------
def _chain_runner(chain_id: int, trigger_type: str = "scheduled"):
    """Run chain steps sequentially via subprocess."""
    with get_db() as conn:
        chain = conn.execute("SELECT * FROM chains WHERE id=?", (chain_id,)).fetchone()
        steps = conn.execute(
            "SELECT * FROM chain_steps WHERE chain_id=? ORDER BY order_index",
            (chain_id,),
        ).fetchall()
    if not chain or not steps:
        return

    name         = chain["name"]
    stop_on_fail = bool(chain["stop_on_failure"])
    total        = len(steps)
    t0           = time.time()
    log_level    = "MANUAL" if trigger_type == "manual" else "FIRE"
    add_log(log_level, f"Chain '{name}' started ({total} step(s))")

    overall_success = True
    last_error      = ""

    for i, step in enumerate(steps, start=1):
        task_path = step["task_path"]
        basename  = os.path.basename(task_path)
        step_t0   = time.time()
        add_log("INFO", f"Chain '{name}' — step {i}/{total}: {basename}")
        try:
            ext = Path(task_path).suffix.lower()
            if ext == ".py":
                result = subprocess.run(
                    [sys.executable, task_path],
                    capture_output=True, text=True,
                )
            elif ext in (".bat", ".sh", ".cmd"):
                result = subprocess.run(
                    [task_path], capture_output=True, text=True,
                )
            else:
                raise ValueError(f"Unsupported file type: {ext}")
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or f"exit code {result.returncode}")
            add_log("OK", f"Chain '{name}' — step {i}/{total} completed in {time.time()-step_t0:.1f}s")
        except Exception:
            tb = traceback.format_exc()
            last_error      = tb
            overall_success = False
            add_log("ERROR", f"Chain '{name}' — step {i}/{total} FAILED\n{tb}")
            if stop_on_fail:
                add_log("INFO", f"Chain '{name}' stopped at step {i} (stop-on-failure)")
                break

    duration_ms = int((time.time() - t0) * 1000)
    if overall_success:
        add_log("OK", f"Chain '{name}' completed in {duration_ms/1000:.1f}s")
    else:
        add_log("ERROR", f"Chain '{name}' finished with errors in {duration_ms/1000:.1f}s")
    _record_run(chain_id=chain_id, trigger_type=trigger_type,
                success=overall_success, duration_ms=duration_ms, error_msg=last_error)

def register_chain_jobs(chain_id: int, sched_type: str, sched_value: str,
                        paused: bool = False):
    for job in scheduler.get_jobs():
        if job.id.startswith(f"c{chain_id}_"):
            scheduler.remove_job(job.id)
    if paused:
        return
    triggers = _make_triggers(sched_type, sched_value)
    for trigger, suffix in triggers:
        scheduler.add_job(
            _chain_runner,
            trigger=trigger,
            id=f"c{chain_id}_{suffix}",
            args=[chain_id],
            kwargs={"trigger_type": "scheduled"},
            replace_existing=True,
            misfire_grace_time=60,
        )

def remove_chain_jobs(chain_id: int):
    for job in scheduler.get_jobs():
        if job.id.startswith(f"c{chain_id}_"):
            scheduler.remove_job(job.id)

def _next_fire_times_chain(chain_id: int):
    times = []
    for job in scheduler.get_jobs():
        if job.id.startswith(f"c{chain_id}_") and job.next_run_time:
            times.append(job.next_run_time)
    return sorted(times)

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder=str(STATIC_DIR))

@app.route("/")
def index():
    return send_from_directory(str(STATIC_DIR), "index.html")

# --- File browser ---
@app.route("/api/browse", methods=["GET"])
def browse_file():
    """Open a native OS file picker and return the chosen absolute path."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", True)
        mode = request.args.get("mode", "file")
        if mode == "dir":
            path = filedialog.askdirectory(parent=root, title="Select Output Directory")
        else:
            path = filedialog.askopenfilename(
                parent=root,
                title="Select Task File",
                filetypes=[("Task files", "*.py *.bat *.sh *.cmd"), ("All files", "*.*")],
            )
        root.destroy()
        if path:
            return jsonify({"path": os.path.abspath(path)})
        return jsonify({"path": ""})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- Redis status ---
@app.route("/api/redis-status", methods=["GET"])
def redis_status():
    ok = _redis_running()
    return jsonify({"running": ok})

# --- Workers ---
@app.route("/api/workers", methods=["GET"])
def list_workers():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM workers ORDER BY id").fetchall()
    result = []
    for r in rows:
        fires = _next_fire_times(r["id"])
        next_trigger = fires[0].strftime("%Y-%m-%d %H:%M:%S") if fires else "—"
        remaining = _remaining_today(r["id"]) if not r["paused"] else 0
        result.append({
            "id": r["id"],
            "name": r["name"],
            "task_path": r["task_path"],
            "sched_type": r["sched_type"],
            "sched_value": r["sched_value"],
            "output_dir": r["output_dir"],
            "paused": bool(r["paused"]),
            "group_id": r["group_id"],
            "next_trigger": next_trigger,
            "remaining_today": remaining,
            "schedule_display": _schedule_display(r["sched_type"], r["sched_value"]),
            "last_run_status": _last_run_status(worker_id=r["id"]),
            "entity_type": "worker",
        })
    return jsonify(result)

def _schedule_display(sched_type, sched_value):
    if sched_type == "fixed":
        return f"Fixed: {sched_value}"
    return f"Every {sched_value}"

@app.route("/api/workers", methods=["POST"])
def add_workers():
    data = request.get_json()
    workers = data if isinstance(data, list) else [data]
    added = []
    errors = []
    for w in workers:
        name = w.get("name", "").strip()
        task_path = w.get("task_path", "").strip()
        sched_type = w.get("sched_type", "interval")
        sched_value = w.get("sched_value", "1h").strip()
        output_dir = w.get("output_dir", "").strip()

        if not name or not task_path:
            errors.append(f"Worker missing name or task path: {w}")
            continue
        if not os.path.isabs(task_path):
            task_path = os.path.abspath(task_path)
        if output_dir and not os.path.isabs(output_dir):
            output_dir = os.path.abspath(output_dir)

        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO workers (name, task_path, sched_type, sched_value, output_dir) VALUES (?,?,?,?,?)",
                (name, task_path, sched_type, sched_value, output_dir),
            )
            worker_id = cur.lastrowid
            conn.commit()

        register_worker_jobs(worker_id, task_path, sched_type, sched_value, output_dir)
        add_log("INFO", f"Worker '{name}' registered (id={worker_id}).")
        added.append(worker_id)

    return jsonify({"added": added, "errors": errors}), (201 if added else 400)

@app.route("/api/workers/<int:worker_id>", methods=["PUT"])
def update_worker(worker_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM workers WHERE id=?", (worker_id,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404

    data = request.get_json()
    name = data.get("name", "").strip()
    task_path = data.get("task_path", "").strip()
    sched_type = data.get("sched_type", row["sched_type"])
    sched_value = data.get("sched_value", row["sched_value"]).strip()
    output_dir = data.get("output_dir", "").strip()

    if not name or not task_path:
        return jsonify({"error": "Missing name or task path"}), 400
    if not os.path.isabs(task_path):
        task_path = os.path.abspath(task_path)
    if output_dir and not os.path.isabs(output_dir):
        output_dir = os.path.abspath(output_dir)

    group_id = data.get("group_id", row["group_id"])  # preserve existing if not provided

    with get_db() as conn:
        conn.execute(
            "UPDATE workers SET name=?, task_path=?, sched_type=?, sched_value=?, output_dir=?, group_id=? WHERE id=?",
            (name, task_path, sched_type, sched_value, output_dir, group_id, worker_id),
        )
        conn.commit()

    register_worker_jobs(worker_id, task_path, sched_type, sched_value, output_dir,
                         paused=bool(row["paused"]))
    add_log("INFO", f"Worker '{name}' updated (id={worker_id}).")
    return jsonify({"ok": True})

@app.route("/api/workers/<int:worker_id>/pause", methods=["POST"])
def toggle_pause(worker_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM workers WHERE id=?", (worker_id,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        new_paused = 0 if row["paused"] else 1
        conn.execute("UPDATE workers SET paused=? WHERE id=?", (new_paused, worker_id))
        conn.commit()

    if new_paused:
        remove_worker_jobs(worker_id)
        add_log("PAUSE", f"Worker '{row['name']}' paused.")
    else:
        register_worker_jobs(worker_id, row["task_path"], row["sched_type"],
                             row["sched_value"], row["output_dir"], paused=False)
        add_log("INFO", f"Worker '{row['name']}' resumed.")

    return jsonify({"paused": bool(new_paused)})

@app.route("/api/workers/<int:worker_id>/history", methods=["GET"])
def get_worker_history(worker_id):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT triggered_at, trigger_type, success, duration_ms, error_msg
               FROM run_history WHERE worker_id=? ORDER BY id DESC LIMIT 10""",
            (worker_id,),
        ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/workers/<int:worker_id>/run-now", methods=["POST"])
def run_worker_now(worker_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM workers WHERE id=?", (worker_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    t = threading.Thread(
        target=_task_runner,
        args=(worker_id, row["task_path"], row["output_dir"]),
        kwargs={"trigger_type": "manual"},
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True})

@app.route("/api/workers/<int:worker_id>", methods=["DELETE"])
def delete_worker(worker_id):
    with get_db() as conn:
        row = conn.execute("SELECT name FROM workers WHERE id=?", (worker_id,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        conn.execute("DELETE FROM workers WHERE id=?", (worker_id,))
        conn.commit()
    remove_worker_jobs(worker_id)
    add_log("DELETE", f"Worker '{row['name']}' deleted.")
    return jsonify({"ok": True})

@app.route("/api/workers/pause-all", methods=["POST"])
def pause_all_workers():
    with get_db() as conn:
        rows = conn.execute("SELECT id FROM workers WHERE paused=0").fetchall()
        conn.execute("UPDATE workers SET paused=1")
        conn.commit()
    for row in rows:
        remove_worker_jobs(row["id"])
    add_log("PAUSE", "All workers paused.")
    return jsonify({"ok": True})

@app.route("/api/workers/all", methods=["DELETE"])
def delete_all_workers():
    with get_db() as conn:
        rows = conn.execute("SELECT id FROM workers").fetchall()
        conn.execute("DELETE FROM workers")
        conn.commit()
    for row in rows:
        remove_worker_jobs(row["id"])
    add_log("DELETE", "All workers deleted.")
    return jsonify({"ok": True})

# --- Profiles ---
@app.route("/api/profiles", methods=["GET"])
def list_profiles():
    with get_db() as conn:
        rows = conn.execute("SELECT id, name, created_at FROM profiles ORDER BY name").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/profiles", methods=["POST"])
def save_profile():
    data = request.get_json()
    name = data.get("name", "").strip()
    config = data.get("config")
    if not name or not config:
        return jsonify({"error": "Missing name or config"}), 400
    with get_db() as conn:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO profiles (name, config_json) VALUES (?,?)",
                (name, json.dumps(config)),
            )
            conn.commit()
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    add_log("INFO", f"Profile '{name}' saved.")
    return jsonify({"ok": True}), 201

@app.route("/api/profiles/<int:profile_id>", methods=["GET"])
def load_profile(profile_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"name": row["name"], "config": json.loads(row["config_json"])})

@app.route("/api/profiles/<int:profile_id>", methods=["DELETE"])
def delete_profile(profile_id):
    with get_db() as conn:
        row = conn.execute("SELECT name FROM profiles WHERE id=?", (profile_id,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        conn.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
        conn.commit()
    add_log("INFO", f"Profile '{row['name']}' deleted.")
    return jsonify({"ok": True})

# --- Templates ---
@app.route("/api/templates", methods=["POST"])
def create_from_template():
    data          = request.get_json()
    template_type = data.get("template_type", "")
    config        = data.get("config", {})
    worker_name   = data.get("worker_name", "").strip()
    sched_type    = data.get("sched_type", "fixed")
    sched_value   = data.get("sched_value", "09:00").strip()

    if not worker_name:
        return jsonify({"error": "Worker name required"}), 400
    if template_type not in TEMPLATE_GENERATORS:
        return jsonify({"error": f"Unknown template type: {template_type}"}), 400

    script_content = TEMPLATE_GENERATORS[template_type](config)
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in worker_name.lower())
    script_path = TEMPLATES_DIR / f"{safe_name}_{template_type}.py"
    script_path.write_text(script_content, encoding="utf-8")

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO workers (name, task_path, sched_type, sched_value) VALUES (?,?,?,?)",
            (worker_name, str(script_path), sched_type, sched_value),
        )
        worker_id = cur.lastrowid
        conn.commit()

    register_worker_jobs(worker_id, str(script_path), sched_type, sched_value, "")
    add_log("INFO", f"Worker '{worker_name}' created from template '{template_type}' (id={worker_id}).")
    return jsonify({"ok": True, "worker_id": worker_id}), 201

# --- Groups ---
@app.route("/api/groups", methods=["GET"])
def list_groups():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM groups ORDER BY order_index, id").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/groups", methods=["POST"])
def create_group():
    data = request.get_json()
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Group name required"}), 400
    with get_db() as conn:
        try:
            cur = conn.execute("INSERT INTO groups (name) VALUES (?)", (name,))
            group_id = cur.lastrowid
            conn.commit()
        except sqlite3.IntegrityError:
            return jsonify({"error": f"Group '{name}' already exists"}), 409
    add_log("INFO", f"Group '{name}' created (id={group_id}).")
    return jsonify({"ok": True, "group_id": group_id}), 201

@app.route("/api/groups/<int:group_id>", methods=["PUT"])
def update_group(group_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
    data = request.get_json()
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Group name required"}), 400
    with get_db() as conn:
        try:
            conn.execute("UPDATE groups SET name=? WHERE id=?", (name, group_id))
            conn.commit()
        except sqlite3.IntegrityError:
            return jsonify({"error": f"Group '{name}' already exists"}), 409
    return jsonify({"ok": True})

@app.route("/api/groups/<int:group_id>", methods=["DELETE"])
def delete_group(group_id):
    with get_db() as conn:
        row = conn.execute("SELECT name FROM groups WHERE id=?", (group_id,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        # Unassign workers and chains from this group
        conn.execute("UPDATE workers SET group_id=NULL WHERE group_id=?", (group_id,))
        conn.execute("UPDATE chains SET group_id=NULL WHERE group_id=?", (group_id,))
        conn.execute("DELETE FROM groups WHERE id=?", (group_id,))
        conn.commit()
    add_log("DELETE", f"Group '{row['name']}' deleted.")
    return jsonify({"ok": True})

@app.route("/api/groups/<int:group_id>/assign", methods=["POST"])
def assign_to_group(group_id):
    """Assign a worker or chain to a group. Pass group_id=null to unassign."""
    data = request.get_json()
    entity_type = data.get("entity_type", "worker")
    entity_id   = data.get("entity_id")
    if entity_id is None:
        return jsonify({"error": "entity_id required"}), 400
    with get_db() as conn:
        if entity_type == "chain":
            conn.execute("UPDATE chains SET group_id=? WHERE id=?", (group_id, entity_id))
        else:
            conn.execute("UPDATE workers SET group_id=? WHERE id=?", (group_id, entity_id))
        conn.commit()
    return jsonify({"ok": True})

@app.route("/api/workers/<int:worker_id>/assign-group", methods=["POST"])
def assign_worker_group(worker_id):
    data = request.get_json()
    group_id = data.get("group_id")  # may be None to unassign
    with get_db() as conn:
        row = conn.execute("SELECT name FROM workers WHERE id=?", (worker_id,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        conn.execute("UPDATE workers SET group_id=? WHERE id=?", (group_id, worker_id))
        conn.commit()
    return jsonify({"ok": True})

@app.route("/api/chains/<int:chain_id>/assign-group", methods=["POST"])
def assign_chain_group(chain_id):
    data = request.get_json()
    group_id = data.get("group_id")  # may be None to unassign
    with get_db() as conn:
        row = conn.execute("SELECT name FROM chains WHERE id=?", (chain_id,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        conn.execute("UPDATE chains SET group_id=? WHERE id=?", (group_id, chain_id))
        conn.commit()
    return jsonify({"ok": True})

# --- Chains ---
@app.route("/api/chains", methods=["GET"])
def list_chains():
    with get_db() as conn:
        chains = conn.execute("SELECT * FROM chains ORDER BY id").fetchall()
        all_steps = conn.execute("SELECT * FROM chain_steps ORDER BY chain_id, order_index").fetchall()
    steps_by_chain = {}
    for s in all_steps:
        steps_by_chain.setdefault(s["chain_id"], []).append(dict(s))
    result = []
    for c in chains:
        cid = c["id"]
        fires = _next_fire_times_chain(cid)
        next_trigger = fires[0].strftime("%Y-%m-%d %H:%M:%S") if fires else "—"
        remaining = _remaining_today_for_prefix(f"c{cid}_") if not c["paused"] else 0
        result.append({
            "id": cid,
            "name": c["name"],
            "sched_type": c["sched_type"],
            "sched_value": c["sched_value"],
            "stop_on_failure": bool(c["stop_on_failure"]),
            "paused": bool(c["paused"]),
            "group_id": c["group_id"],
            "steps": steps_by_chain.get(cid, []),
            "next_trigger": next_trigger,
            "remaining_today": remaining,
            "schedule_display": _schedule_display(c["sched_type"], c["sched_value"]),
            "last_run_status": _last_run_status(chain_id=cid),
            "entity_type": "chain",
        })
    return jsonify(result)

@app.route("/api/chains", methods=["POST"])
def add_chain():
    data = request.get_json()
    name = data.get("name", "").strip()
    sched_type = data.get("sched_type", "interval")
    sched_value = data.get("sched_value", "1h").strip()
    stop_on_failure = int(data.get("stop_on_failure", True))
    steps = data.get("steps", [])

    if not name:
        return jsonify({"error": "Chain name required"}), 400
    if not steps:
        return jsonify({"error": "At least one step required"}), 400

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO chains (name, sched_type, sched_value, stop_on_failure) VALUES (?,?,?,?)",
            (name, sched_type, sched_value, stop_on_failure),
        )
        chain_id = cur.lastrowid
        for i, step in enumerate(steps):
            task_path = step.get("task_path", "").strip()
            if not task_path:
                continue
            if not os.path.isabs(task_path):
                task_path = os.path.abspath(task_path)
            conn.execute(
                "INSERT INTO chain_steps (chain_id, order_index, task_path) VALUES (?,?,?)",
                (chain_id, i, task_path),
            )
        conn.commit()

    register_chain_jobs(chain_id, sched_type, sched_value)
    add_log("INFO", f"Chain '{name}' registered (id={chain_id}, {len(steps)} step(s)).")
    return jsonify({"ok": True, "chain_id": chain_id}), 201

@app.route("/api/chains/<int:chain_id>", methods=["PUT"])
def update_chain(chain_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM chains WHERE id=?", (chain_id,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404

    data = request.get_json()
    name = data.get("name", "").strip()
    sched_type = data.get("sched_type", row["sched_type"])
    sched_value = data.get("sched_value", row["sched_value"]).strip()
    stop_on_failure = int(data.get("stop_on_failure", row["stop_on_failure"]))
    steps = data.get("steps", [])

    if not name:
        return jsonify({"error": "Chain name required"}), 400

    group_id = data.get("group_id", row["group_id"])  # preserve existing if not provided

    with get_db() as conn:
        conn.execute(
            "UPDATE chains SET name=?, sched_type=?, sched_value=?, stop_on_failure=?, group_id=? WHERE id=?",
            (name, sched_type, sched_value, stop_on_failure, group_id, chain_id),
        )
        conn.execute("DELETE FROM chain_steps WHERE chain_id=?", (chain_id,))
        for i, step in enumerate(steps):
            task_path = step.get("task_path", "").strip()
            if not task_path:
                continue
            if not os.path.isabs(task_path):
                task_path = os.path.abspath(task_path)
            conn.execute(
                "INSERT INTO chain_steps (chain_id, order_index, task_path) VALUES (?,?,?)",
                (chain_id, i, task_path),
            )
        conn.commit()

    register_chain_jobs(chain_id, sched_type, sched_value, paused=bool(row["paused"]))
    add_log("INFO", f"Chain '{name}' updated (id={chain_id}).")
    return jsonify({"ok": True})

@app.route("/api/chains/<int:chain_id>", methods=["DELETE"])
def delete_chain(chain_id):
    with get_db() as conn:
        row = conn.execute("SELECT name FROM chains WHERE id=?", (chain_id,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        conn.execute("DELETE FROM chain_steps WHERE chain_id=?", (chain_id,))
        conn.execute("DELETE FROM chains WHERE id=?", (chain_id,))
        conn.commit()
    remove_chain_jobs(chain_id)
    add_log("DELETE", f"Chain '{row['name']}' deleted.")
    return jsonify({"ok": True})

@app.route("/api/chains/<int:chain_id>/pause", methods=["POST"])
def toggle_pause_chain(chain_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM chains WHERE id=?", (chain_id,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        new_paused = 0 if row["paused"] else 1
        conn.execute("UPDATE chains SET paused=? WHERE id=?", (new_paused, chain_id))
        conn.commit()

    if new_paused:
        remove_chain_jobs(chain_id)
        add_log("PAUSE", f"Chain '{row['name']}' paused.")
    else:
        register_chain_jobs(chain_id, row["sched_type"], row["sched_value"], paused=False)
        add_log("INFO", f"Chain '{row['name']}' resumed.")

    return jsonify({"paused": bool(new_paused)})

@app.route("/api/chains/<int:chain_id>/run-now", methods=["POST"])
def run_chain_now(chain_id):
    with get_db() as conn:
        row = conn.execute("SELECT name FROM chains WHERE id=?", (chain_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    t = threading.Thread(
        target=_chain_runner,
        args=(chain_id,),
        kwargs={"trigger_type": "manual"},
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True})

@app.route("/api/chains/<int:chain_id>/history", methods=["GET"])
def get_chain_history(chain_id):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT triggered_at, trigger_type, success, duration_ms, error_msg
               FROM run_history WHERE chain_id=? ORDER BY id DESC LIMIT 10""",
            (chain_id,),
        ).fetchall()
    return jsonify([dict(r) for r in rows])

# --- AI Analysis ---
@app.route("/api/api-key-status", methods=["GET"])
def api_key_status():
    return jsonify({"has_key": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())})

@app.route("/api/save-api-key", methods=["POST"])
def save_api_key():
    data = request.get_json()
    key = (data.get("key") or "").strip()
    if not key:
        return jsonify({"error": "No key provided"}), 400

    lines = []
    replaced = False
    if ENV_PATH.exists():
        with open(ENV_PATH) as f:
            for line in f:
                if line.strip().startswith("ANTHROPIC_API_KEY"):
                    lines.append(f"ANTHROPIC_API_KEY={key}\n")
                    replaced = True
                else:
                    lines.append(line)
    if not replaced:
        lines.append(f"ANTHROPIC_API_KEY={key}\n")

    with open(ENV_PATH, "w") as f:
        f.writelines(lines)

    os.environ["ANTHROPIC_API_KEY"] = key
    return jsonify({"ok": True})

@app.route("/api/analyze-logs", methods=["POST"])
def analyze_logs():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not configured"}), 400

    data = request.get_json()
    entries = data.get("entries", [])
    if not entries:
        return jsonify({"error": "No error entries provided"}), 400

    formatted = "\n".join(
        f"[{e.get('ts','')}] {e.get('level','')}: {e.get('msg','')}"
        for e in entries
    )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=(
                "You are a helpful assistant that diagnoses errors in scheduled task runners. "
                "The user is running Redis Operator, a local dashboard that schedules Python and "
                "batch workers via APScheduler. Analyze the error log entries and provide a clear, "
                "plain-English explanation of what went wrong and exactly what the user should do "
                "to fix it. Be concise and practical."
            ),
            messages=[{
                "role": "user",
                "content": (
                    f"Here are the error log entries from my task scheduler:\n\n{formatted}\n\n"
                    "What went wrong and how do I fix it?"
                ),
            }],
        ) as stream:
            response = stream.get_final_message()
        text = next((b.text for b in response.content if b.type == "text"), "")
        return jsonify({"analysis": text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- Logs ---
@app.route("/api/logs", methods=["GET"])
def get_logs():
    since = request.args.get("since", 0, type=int)
    with LOG_LOCK:
        entries = list(LOG_BUFFER)[since:]
    return jsonify({"entries": entries, "total": len(LOG_BUFFER)})

# --- Shutdown ---
@app.route("/api/shutdown", methods=["POST"])
def shutdown():
    func = request.environ.get("werkzeug.server.shutdown")
    if func:
        func()
    else:
        os.kill(os.getpid(), signal.SIGTERM)
    return jsonify({"ok": True})

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
def restore_workers():
    """Re-register all non-paused workers from DB into APScheduler on startup."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM workers WHERE paused=0").fetchall()
    for r in rows:
        try:
            register_worker_jobs(r["id"], r["task_path"], r["sched_type"],
                                 r["sched_value"], r["output_dir"])
            add_log("INFO", f"Worker '{r['name']}' restored from DB.")
        except Exception as e:
            add_log("ERROR", f"Failed to restore worker '{r['name']}': {e}")

def restore_chains():
    """Re-register all non-paused chains from DB into APScheduler on startup."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM chains WHERE paused=0").fetchall()
    for r in rows:
        try:
            register_chain_jobs(r["id"], r["sched_type"], r["sched_value"])
            add_log("INFO", f"Chain '{r['name']}' restored from DB.")
        except Exception as e:
            add_log("ERROR", f"Failed to restore chain '{r['name']}': {e}")

def create_app():
    global scheduler
    _load_dotenv()
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    redis_result = start_redis()
    if not redis_result["ok"]:
        add_log("ERROR", redis_result["message"])
    jobstores = {"default": SQLAlchemyJobStore(url=DB_URL)}
    scheduler = BackgroundScheduler(jobstores=jobstores, timezone=_get_local_tz())
    scheduler.start()
    restore_workers()
    restore_chains()
    atexit.register(lambda: scheduler.shutdown(wait=False))
    atexit.register(stop_redis)
    return app

application = create_app()

if __name__ == "__main__":
    application.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
