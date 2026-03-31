"""
app.py — Redis Operator backend
Flask + APScheduler + SQLite + Redis
"""

import os
import sys
import json
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

def _task_runner(worker_id: int, task_path: str, output_dir: str):
    """Execute a task file. Supports .py and .bat/.sh scripts."""
    add_log("FIRE", f"Worker #{worker_id} fired — {os.path.basename(task_path)}")
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
        add_log("OK", f"Worker #{worker_id} completed — {os.path.basename(task_path)}")
    except Exception:
        tb = traceback.format_exc()
        add_log("ERROR", f"Worker #{worker_id} FAILED — {os.path.basename(task_path)}\n{tb}")

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

def _remaining_today(worker_id: int) -> int:
    now = datetime.now().astimezone()
    end_of_day = datetime.now().replace(hour=23, minute=59, second=59).astimezone()
    count = 0
    for job in scheduler.get_jobs():
        if not job.id.startswith(f"w{worker_id}_"):
            continue
        t = job.next_run_time
        if t and now <= t <= end_of_day:
            count += 1
            # For interval triggers, walk forward
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
            "next_trigger": next_trigger,
            "remaining_today": remaining,
            "schedule_display": _schedule_display(r["sched_type"], r["sched_value"]),
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

def create_app():
    global scheduler
    init_db()
    redis_result = start_redis()
    if not redis_result["ok"]:
        add_log("ERROR", redis_result["message"])
    jobstores = {"default": SQLAlchemyJobStore(url=DB_URL)}
    scheduler = BackgroundScheduler(jobstores=jobstores, timezone=_get_local_tz())
    scheduler.start()
    restore_workers()
    atexit.register(lambda: scheduler.shutdown(wait=False))
    atexit.register(stop_redis)
    return app

application = create_app()

if __name__ == "__main__":
    application.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
