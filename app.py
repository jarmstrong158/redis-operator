"""
app.py — Redis Operator backend
Flask + APScheduler + SQLite + Redis
"""

import os
import sys
import ast
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
import sysconfig
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import groupby
from datetime import datetime, date, timedelta
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
import redis

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------
# sys.frozen is True when running as a PyInstaller bundle.
# PyInstaller 6+ puts bundled assets in _internal/ (sys._MEIPASS).
# User data (DB, .env, generated templates) lives next to the exe.
if getattr(sys, "frozen", False):
    BASE_DIR    = Path(sys.executable).parent.resolve()          # writable: db, .env, templates
    BUNDLE_DIR  = Path(getattr(sys, "_MEIPASS", BASE_DIR))       # read-only: static, tasks, redis
else:
    BASE_DIR    = Path(__file__).parent.resolve()
    BUNDLE_DIR  = BASE_DIR

DB_PATH       = BASE_DIR   / "redis_operator.db"
STATIC_DIR    = BUNDLE_DIR / "static"
ENV_PATH      = BASE_DIR   / ".env"
TEMPLATES_DIR = BASE_DIR   / "templates" / "generated"

VERSION = "3.0.2"
GITHUB_REPO = "jarmstrong158/redis-operator"

# ---------------------------------------------------------------------------
# Dependency management
# ---------------------------------------------------------------------------
def _stdlib_modules() -> set:
    """Return the set of stdlib module names for the running Python."""
    try:
        return sys.stdlib_module_names  # Python 3.10+
    except AttributeError:
        import sysconfig as _sc
        stdlib_path = _sc.get_paths()["stdlib"]
        names = set()
        for p in [stdlib_path, sysconfig.get_paths().get("platstdlib", "")]:
            try:
                for f in os.listdir(p):
                    names.add(f.split(".")[0])
            except OSError:
                pass
        return names

_STDLIB = None

def _is_stdlib(module_name: str) -> bool:
    global _STDLIB
    if _STDLIB is None:
        _STDLIB = _stdlib_modules()
    return module_name.split(".")[0] in _STDLIB

def _get_missing_modules(script_path: str) -> list:
    """Parse a .py file for imports and return any that aren't installed."""
    missing = []
    try:
        with open(script_path, encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename=script_path)
    except Exception:
        return missing

    top_level = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                top_level.add(node.module.split(".")[0])

    for mod in top_level:
        if _is_stdlib(mod):
            continue
        try:
            importlib.util.find_spec(mod)
        except (ModuleNotFoundError, ValueError):
            missing.append(mod)
        else:
            if importlib.util.find_spec(mod) is None:
                missing.append(mod)

    return missing

def _pip_install(packages: list, context: str = "") -> bool:
    """Install packages via pip. Logs result. Returns True on success."""
    if not packages:
        return True
    pkg_list = ", ".join(packages)
    add_log("INFO", f"Auto-installing: {pkg_list}{' for ' + context if context else ''}")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet"] + packages,
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            add_log("OK", f"Installed: {pkg_list}")
            return True
        else:
            add_log("ERROR", f"pip install failed for {pkg_list}:\n{result.stderr.strip()}")
            return False
    except Exception as e:
        add_log("ERROR", f"pip install exception: {e}")
        return False

def _extract_missing_module(tb: str) -> str:
    """Pull the module name out of a ModuleNotFoundError traceback."""
    import re
    m = re.search(r"No module named '([^']+)'", tb)
    return m.group(1).split(".")[0] if m else ""

# ---------------------------------------------------------------------------
# Template script generators
# ---------------------------------------------------------------------------
def _gen_folder_backup(cfg: dict) -> str:
    source = cfg.get("source", "")
    dest   = cfg.get("dest", "")
    keep   = int(cfg.get("keep", 3))
    summary_email = cfg.get("summary_email", "").strip()
    email_block = ""
    if summary_email:
        email_block = f'''
    _send_email(
        "{summary_email}",
        f"[Backup] {{os.path.basename(SOURCE)}} — {{len(backups)}}/{{KEEP}} copies",
        f"Backup complete: {{dst}}\\n{{len(backups)}}/{{KEEP}} copies retained.",
    )'''
    return (_EMAIL_HELPER_CODE if summary_email else '') + f'''
import shutil, os, datetime

SOURCE = r"{source}"
DEST   = r"{dest}"
KEEP   = {keep}

if __name__ == "__main__":
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
{email_block}
'''

def _gen_file_cleanup(cfg: dict) -> str:
    folder  = cfg.get("folder", "")
    pattern = cfg.get("pattern", "*.tmp")
    days    = int(cfg.get("days", 7))
    summary_email = cfg.get("summary_email", "").strip()
    email_block = ""
    if summary_email:
        email_block = f'''
    if removed > 0:
        _send_email(
            "{summary_email}",
            f"[Cleanup] {{removed}} file(s) removed from {{os.path.basename(FOLDER)}}",
            f"Cleanup: {{removed}} file(s) matching {{PATTERN}} older than {{DAYS}} days removed from {{FOLDER}}",
        )'''
    return (_EMAIL_HELPER_CODE if summary_email else '') + f'''
import glob, os, time

FOLDER  = r"{folder}"
PATTERN = "{pattern}"
DAYS    = {days}

if __name__ == "__main__":
    cutoff = time.time() - (DAYS * 86400)
    removed = 0
    for f in glob.glob(os.path.join(FOLDER, PATTERN)):
        if os.path.isfile(f) and os.path.getmtime(f) < cutoff:
            os.remove(f)
            removed += 1
    print(f"Cleanup: {{removed}} file(s) removed matching {{PATTERN}} older than {{DAYS}} days")
{email_block}
'''

def _gen_folder_watcher(cfg: dict) -> str:
    watch = cfg.get("watch", "")
    rules = cfg.get("rules", [])
    # Build rules: {".ext": {"dest": path_or_empty, "email_to": addr_or_empty}}
    rules_dict = {}
    has_email = False
    for r in rules:
        ext = (r.get("ext") or "").lower()
        dest = r.get("dest", "")
        email_to = r.get("email_to", "")
        if ext and (dest or email_to):
            rules_dict[ext] = {"dest": dest, "email_to": email_to}
            if email_to:
                has_email = True
    return (_EMAIL_HELPER_CODE if has_email else '') + f'''
import os, shutil

WATCH = r"{watch}"
RULES = {repr(rules_dict)}

if __name__ == "__main__":
    moved = 0
    emailed = 0
    for fname in os.listdir(WATCH):
        fpath = os.path.join(WATCH, fname)
        if not os.path.isfile(fpath):
            continue
        ext = os.path.splitext(fname)[1].lower()
        if ext in RULES:
            rule = RULES[ext]
            dest = rule.get("dest", "")
            email_to = rule.get("email_to", "")
            if email_to:
                _send_email(email_to, f"File: {{fname}}", f"Attached file from {{WATCH}}", [fpath])
                emailed += 1
            if dest:
                os.makedirs(dest, exist_ok=True)
                shutil.move(fpath, os.path.join(dest, fname))
                moved += 1
            elif email_to and not dest:
                pass  # emailed only, no move
    print(f"Folder watcher: {{moved}} moved, {{emailed}} emailed")
'''

def _gen_uptime_check(cfg: dict) -> str:
    url         = cfg.get("url", "")
    log_file    = cfg.get("log_file", "")
    alert_email = cfg.get("alert_email", "").strip()
    email_block = ""
    if alert_email:
        email_block = f'''
    if status.startswith("DOWN"):
        _send_email(
            "{alert_email}",
            f"[Alert] {{URL}} is DOWN",
            f"{{URL}} is not responding.\\n\\n[{{ts}}] {{status}}",
        )'''
    return (_EMAIL_HELPER_CODE if alert_email else '') + f'''
import urllib.request, datetime, os

URL      = "{url}"
LOG_FILE = r"{log_file}"

if __name__ == "__main__":
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
{email_block}
    if status.startswith("DOWN"):
        raise RuntimeError(f"Site down: {{URL}} — {{status}}")
'''

def _gen_open_url(cfg: dict) -> str:
    url = cfg.get("url", "")
    return f'''import webbrowser

URL = "{url}"

if __name__ == "__main__":
    webbrowser.open(URL)
    print(f"Opened: {{URL}}")
'''


def _gen_run_and_email(cfg: dict) -> str:
    script_path = cfg.get("script_path", "")
    output_file = cfg.get("output_file", "")
    email_to    = cfg.get("email_to", "")
    return _EMAIL_HELPER_CODE + f'''
import subprocess, sys, os, datetime

SCRIPT_PATH = r"{script_path}"
OUTPUT_FILE = r"{output_file}"
EMAIL_TO    = "{email_to}"

if __name__ == "__main__":
    print(f"Running {{os.path.basename(SCRIPT_PATH)}} ...")
    result = subprocess.run(
        [sys.executable, SCRIPT_PATH],
        capture_output=True, text=True,
        cwd=os.path.dirname(SCRIPT_PATH) or ".",
    )
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0:
        raise RuntimeError(
            f"Script failed (exit {{result.returncode}}):\\n{{result.stderr.strip()}}"
        )
    print(f"Script completed. Looking for output: {{OUTPUT_FILE}}")
    if not os.path.isfile(OUTPUT_FILE):
        raise FileNotFoundError(f"Output file not found: {{OUTPUT_FILE}}")
    today = datetime.datetime.now().strftime("%m/%d/%Y")
    _send_email(
        EMAIL_TO,
        f"Report — {{os.path.basename(OUTPUT_FILE)}} ({{today}})",
        f"Automated report generated on {{today}}.\\nAttached: {{os.path.basename(OUTPUT_FILE)}}",
        [OUTPUT_FILE],
    )
    print("Done.")
'''

TEMPLATE_GENERATORS = {
    "folder_backup":  _gen_folder_backup,
    "file_cleanup":   _gen_file_cleanup,
    "folder_watcher": _gen_folder_watcher,
    "uptime_check":   _gen_uptime_check,
    "open_url":       _gen_open_url,
    "run_and_email":  _gen_run_and_email,
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
# Email helper
# ---------------------------------------------------------------------------
def _save_env_key(key: str, value: str):
    """Write or update a single KEY=VALUE in the .env file and set os.environ."""
    lines = []
    replaced = False
    if ENV_PATH.exists():
        with open(ENV_PATH) as f:
            for line in f:
                if line.strip().startswith(key):
                    lines.append(f"{key}={value}\n")
                    replaced = True
                else:
                    lines.append(line)
    if not replaced:
        lines.append(f"{key}={value}\n")
    with open(ENV_PATH, "w") as f:
        f.writelines(lines)
    os.environ[key] = value


def _send_email(to: str, subject: str, body_text: str,
                attachments: list = None) -> bool:
    """Send an email via Gmail SMTP. Returns True on success, False on failure.
    Never raises — logs errors and continues."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders

    gmail_user = os.environ.get("GMAIL_USER", "").strip()
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not gmail_user or not gmail_pass:
        add_log("ERROR", "Email send failed — Gmail credentials not configured")
        return False
    if not to or not to.strip():
        return False

    try:
        msg = MIMEMultipart()
        msg["From"] = gmail_user
        msg["To"] = to.strip()
        msg["Subject"] = subject
        msg.attach(MIMEText(body_text, "plain"))

        for filepath in (attachments or []):
            if not os.path.isfile(filepath):
                continue
            filename = os.path.basename(filepath)
            with open(filepath, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={filename}")
            msg.attach(part)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, to.strip(), msg.as_string())
        add_log("OK", f"Email sent to {to.strip()} — {subject}")
        return True
    except Exception as e:
        add_log("ERROR", f"Email send failed — {e}")
        return False


# Inline email helper for generated template scripts (stdlib only)
_EMAIL_HELPER_CODE = '''
import os, smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

def _send_email(to, subject, body, attachments=None):
    user = os.environ.get("GMAIL_USER", "")
    pw = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not user or not pw or not to:
        print(f"Email skipped — credentials not configured")
        return False
    try:
        msg = MIMEMultipart()
        msg["From"] = user
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        for fp in (attachments or []):
            if not os.path.isfile(fp):
                continue
            with open(fp, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={os.path.basename(fp)}")
            msg.attach(part)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(user, pw)
            s.sendmail(user, to, msg.as_string())
        print(f"Email sent to {to}")
        return True
    except Exception as e:
        print(f"Email failed: {e}")
        return False
'''

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

    # Determine binary — prefer bundled copy, fall back to PATH
    binary_name = "redis-server.exe" if sys.platform == "win32" else "redis-server"
    bundled = BUNDLE_DIR / "redis_bundled" / binary_name
    if bundled.exists():
        binary = str(bundled)
        add_log("INFO", "Using bundled redis-server.")
    else:
        import shutil
        binary = shutil.which(binary_name)
        if not binary:
            msg = (
                f"redis-server not found.\n\n"
                f"Install Redis:\n"
                f"  Windows : https://github.com/tporadowski/redis/releases\n"
                f"            or: winget install Redis.Redis\n"
                f"  macOS   : brew install redis\n"
                f"  Ubuntu  : sudo apt install redis-server\n\n"
                f"After installing, ensure '{binary_name}' is on your system PATH, then restart."
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

        # Migrate workers table: add missing columns
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(workers)")}
        if "group_id" not in existing_cols:
            conn.execute("ALTER TABLE workers ADD COLUMN group_id INTEGER DEFAULT NULL")
        if "requirements" not in existing_cols:
            conn.execute("ALTER TABLE workers ADD COLUMN requirements TEXT DEFAULT ''")
        if "new_console" not in existing_cols:
            conn.execute("ALTER TABLE workers ADD COLUMN new_console INTEGER DEFAULT 0")
        if "timeout_minutes" not in existing_cols:
            conn.execute("ALTER TABLE workers ADD COLUMN timeout_minutes INTEGER DEFAULT 0")
        if "env_vars" not in existing_cols:
            conn.execute("ALTER TABLE workers ADD COLUMN env_vars TEXT DEFAULT ''")
        if "notify_email" not in existing_cols:
            conn.execute("ALTER TABLE workers ADD COLUMN notify_email TEXT DEFAULT ''")
        if "notify_on" not in existing_cols:
            conn.execute("ALTER TABLE workers ADD COLUMN notify_on TEXT DEFAULT 'always'")

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
                stage       INTEGER DEFAULT 0,
                FOREIGN KEY (chain_id) REFERENCES chains(id) ON DELETE CASCADE
            )
        """)
        # Migrate chain_steps: add stage column to existing DBs
        existing_step_cols = {row[1] for row in conn.execute("PRAGMA table_info(chain_steps)")}
        if "stage" not in existing_step_cols:
            conn.execute("ALTER TABLE chain_steps ADD COLUMN stage INTEGER DEFAULT 0")
            # Each existing step gets its own stage so old chains stay sequential
            conn.execute("UPDATE chain_steps SET stage = order_index")

        # Migrate chains table: add notification columns
        existing_chain_cols = {row[1] for row in conn.execute("PRAGMA table_info(chains)")}
        if "notify_email" not in existing_chain_cols:
            conn.execute("ALTER TABLE chains ADD COLUMN notify_email TEXT DEFAULT ''")
        if "notify_on" not in existing_chain_cols:
            conn.execute("ALTER TABLE chains ADD COLUMN notify_on TEXT DEFAULT 'always'")

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
    elif sched_type == "cron":
        triggers.append((CronTrigger.from_crontab(sched_value), "c0"))
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

def _parse_env_vars(env_vars: str) -> dict:
    """Parse KEY=VALUE lines into a dict. Skips blank lines and comments."""
    result = {}
    for line in env_vars.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        result[key.strip()] = val.strip()
    return result

def _run_script(task_path: str, output_dir: str = "",
                new_console: bool = False,
                timeout_minutes: int = 0,
                env_vars: str = "") -> subprocess.CompletedProcess:
    """Run a script file as a subprocess and return the result."""
    ext = Path(task_path).suffix.lower()
    if ext == ".py":
        cmd = [sys.executable, task_path]
    elif ext in (".bat", ".sh", ".cmd"):
        cmd = [task_path]
    else:
        raise ValueError(f"Unsupported file type: {ext}")
    cwd = output_dir or os.path.dirname(task_path) or str(BASE_DIR)
    timeout_secs = timeout_minutes * 60 if timeout_minutes and timeout_minutes > 0 else None
    env = {**os.environ, **_parse_env_vars(env_vars)} if env_vars.strip() else None
    if new_console and sys.platform == "win32":
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        proc.wait(timeout=timeout_secs)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout="", stderr="")
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=timeout_secs,
    )

def _task_runner(worker_id: int, task_path: str, output_dir: str,
                 trigger_type: str = "scheduled", new_console: bool = False,
                 timeout_minutes: int = 0, env_vars: str = ""):
    """Execute a task file as a subprocess.
    On ModuleNotFoundError, auto-installs the missing package and retries once."""
    log_level = "MANUAL" if trigger_type == "manual" else "FIRE"
    add_log(log_level, f"Worker #{worker_id} fired — {os.path.basename(task_path)}")
    t0 = time.time()
    error_msg = ""
    success = False
    basename = os.path.basename(task_path)
    try:
        result = _run_script(task_path, output_dir, new_console=new_console,
                             timeout_minutes=timeout_minutes, env_vars=env_vars)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "ModuleNotFoundError" in stderr or "No module named" in stderr:
                missing = _extract_missing_module(stderr)
                if missing and _pip_install([missing], context=f"Worker #{worker_id}"):
                    add_log("INFO", f"Worker #{worker_id} — retrying after install...")
                    result = _run_script(task_path, output_dir, new_console=new_console,
                                        timeout_minutes=timeout_minutes, env_vars=env_vars)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or f"exit code {result.returncode}")
        if result.stdout.strip():
            add_log("INFO", f"Worker #{worker_id} output:\n{result.stdout.strip()}")
        success = True
        add_log("OK", f"Worker #{worker_id} completed — {basename}")
    except subprocess.TimeoutExpired:
        error_msg = f"Timed out after {timeout_minutes} minute(s)."
        add_log("ERROR", f"Worker #{worker_id} timed out after {timeout_minutes} min — {basename}")
    except Exception:
        tb = traceback.format_exc()
        error_msg = tb
        add_log("ERROR", f"Worker #{worker_id} FAILED — {basename}\n{tb}")
    finally:
        duration_ms = int((time.time() - t0) * 1000)
        _record_run(worker_id=worker_id, trigger_type=trigger_type,
                    success=success, duration_ms=duration_ms, error_msg=error_msg)
        # --- Email notification ---
        try:
            with get_db() as conn:
                w = conn.execute("SELECT name, notify_email, notify_on FROM workers WHERE id=?",
                                 (worker_id,)).fetchone()
            if w and w["notify_email"]:
                should_send = (w["notify_on"] == "always"
                               or (w["notify_on"] == "failure" and not success)
                               or (w["notify_on"] == "success" and success))
                if should_send:
                    status_icon = "\u2713" if success else "\u2717"
                    status_word = "completed" if success else "failed"
                    dur_str = f"{duration_ms/1000:.1f}s" if duration_ms >= 1000 else f"{duration_ms}ms"
                    body = f"Worker: {w['name']}\nStatus: {status_word.title()}\nDuration: {dur_str}\nTriggered: {trigger_type}"
                    if error_msg:
                        body += f"\n\nError:\n{error_msg[:2000]}"
                    _send_email(w["notify_email"],
                                f"[Redis Operator] {status_icon} \"{w['name']}\" {status_word}",
                                body)
        except Exception:
            pass  # never crash on notification failure

def register_worker_jobs(worker_id: int, task_path: str, sched_type: str,
                         sched_value: str, output_dir: str, paused: bool = False,
                         new_console: bool = False, timeout_minutes: int = 0,
                         env_vars: str = ""):
    """Add APScheduler jobs for a worker. Remove existing ones first."""
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
            kwargs={"new_console": new_console, "timeout_minutes": timeout_minutes,
                    "env_vars": env_vars},
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
def _run_one_chain_step(chain_name: str, step_label: str, task_path: str):
    """Execute a single chain step subprocess. Returns (success, error_str, duration_s)."""
    t0 = time.time()
    ext = Path(task_path).suffix.lower()
    if ext == ".py":
        result = subprocess.run([sys.executable, task_path], capture_output=True, text=True)
        if result.returncode != 0 and "ModuleNotFoundError" in result.stderr:
            missing = _extract_missing_module(result.stderr)
            if missing and _pip_install([missing], context=f"Chain '{chain_name}' {step_label}"):
                add_log("INFO", f"Chain '{chain_name}' — {step_label} retrying after install...")
                result = subprocess.run([sys.executable, task_path], capture_output=True, text=True)
    elif ext in (".bat", ".sh", ".cmd"):
        result = subprocess.run([task_path], capture_output=True, text=True)
    else:
        raise ValueError(f"Unsupported file type: {ext}")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"exit code {result.returncode}")
    return time.time() - t0


def _chain_runner(chain_id: int, trigger_type: str = "scheduled"):
    """Run chain steps. Steps with the same stage number run in parallel; stages run sequentially."""
    with get_db() as conn:
        chain = conn.execute("SELECT * FROM chains WHERE id=?", (chain_id,)).fetchone()
        steps = conn.execute(
            "SELECT * FROM chain_steps WHERE chain_id=? ORDER BY stage, order_index",
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
    step_num        = 0  # running counter for display

    # Group steps by stage (already sorted by stage, order_index above)
    stage_groups = [(stg, list(grp)) for stg, grp in groupby(steps, key=lambda s: s["stage"])]

    for stage_num, stage_steps in stage_groups:
        if not overall_success and stop_on_fail:
            break
        parallel = len(stage_steps) > 1
        if parallel:
            add_log("INFO", f"Chain '{name}' — stage {stage_num}: {len(stage_steps)} steps in parallel")

        def _run_step(step, sn=step_num):
            """Thread target — runs one step, logs, returns (success, err)."""
            task_path = step["task_path"]
            basename  = os.path.basename(task_path)
            label     = f"step {sn + 1}/{total}"
            add_log("INFO", f"Chain '{name}' — {label}: {basename}")
            try:
                elapsed = _run_one_chain_step(name, label, task_path)
                add_log("OK", f"Chain '{name}' — {label} completed in {elapsed:.1f}s")
                return True, ""
            except Exception:
                tb = traceback.format_exc()
                add_log("ERROR", f"Chain '{name}' — {label} FAILED\n{tb}")
                return False, tb

        step_num += len(stage_steps)

        if parallel:
            with ThreadPoolExecutor(max_workers=len(stage_steps)) as ex:
                futures = {ex.submit(_run_step, step): step for step in stage_steps}
                for fut in as_completed(futures):
                    ok, err = fut.result()
                    if not ok:
                        overall_success = False
                        last_error = err
        else:
            ok, err = _run_step(stage_steps[0])
            if not ok:
                overall_success = False
                last_error = err
                if stop_on_fail:
                    add_log("INFO", f"Chain '{name}' stopped (stop-on-failure)")

    duration_ms = int((time.time() - t0) * 1000)
    if overall_success:
        add_log("OK", f"Chain '{name}' completed in {duration_ms/1000:.1f}s")
    else:
        add_log("ERROR", f"Chain '{name}' finished with errors in {duration_ms/1000:.1f}s")
    _record_run(chain_id=chain_id, trigger_type=trigger_type,
                success=overall_success, duration_ms=duration_ms, error_msg=last_error)
    # --- Email notification ---
    try:
        with get_db() as conn:
            c = conn.execute("SELECT notify_email, notify_on FROM chains WHERE id=?",
                             (chain_id,)).fetchone()
        if c and c["notify_email"]:
            should_send = (c["notify_on"] == "always"
                           or (c["notify_on"] == "failure" and not overall_success)
                           or (c["notify_on"] == "success" and overall_success))
            if should_send:
                status_icon = "\u2713" if overall_success else "\u2717"
                status_word = "completed" if overall_success else "failed"
                dur_str = f"{duration_ms/1000:.1f}s" if duration_ms >= 1000 else f"{duration_ms}ms"
                body = f"Chain: {name}\nStatus: {status_word.title()}\nSteps: {total}\nDuration: {dur_str}\nTriggered: {trigger_type}"
                if last_error:
                    body += f"\n\nError:\n{last_error[:2000]}"
                _send_email(c["notify_email"],
                            f"[Redis Operator] {status_icon} \"{name}\" {status_word}",
                            body)
    except Exception:
        pass

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
            "requirements": r["requirements"] or "",
            "new_console": bool(r["new_console"]),
            "timeout_minutes": int(r["timeout_minutes"] or 0),
            "env_vars": r["env_vars"] or "",
            "notify_email": r["notify_email"] or "",
            "notify_on": r["notify_on"] or "always",
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
    if sched_type == "cron":
        return f"Cron: {sched_value}"
    return f"Every {sched_value}"

def _install_for_worker(name: str, task_path: str, requirements: str):
    """Install declared requirements + auto-detected missing imports."""
    to_install = []
    # Declared requirements from the user
    declared = [p.strip() for p in requirements.split(",") if p.strip()]
    to_install.extend(declared)
    # Auto-detected missing imports (only for .py files that exist)
    if task_path.endswith(".py") and os.path.isfile(task_path):
        missing = _get_missing_modules(task_path)
        for m in missing:
            if m not in to_install:
                to_install.append(m)
    if to_install:
        _pip_install(to_install, context=f"worker '{name}'")

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
        requirements = w.get("requirements", "").strip()
        new_console = bool(w.get("new_console", False))
        timeout_minutes = int(w.get("timeout_minutes", 0) or 0)
        env_vars = w.get("env_vars", "").strip()
        notify_email = w.get("notify_email", "").strip()
        notify_on = w.get("notify_on", "always").strip()

        if not name or not task_path:
            errors.append(f"Worker missing name or task path: {w}")
            continue
        if not os.path.isabs(task_path):
            task_path = os.path.abspath(task_path)
        if output_dir and not os.path.isabs(output_dir):
            output_dir = os.path.abspath(output_dir)

        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO workers (name, task_path, sched_type, sched_value, output_dir, requirements, new_console, timeout_minutes, env_vars, notify_email, notify_on) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (name, task_path, sched_type, sched_value, output_dir, requirements, int(new_console), timeout_minutes, env_vars, notify_email, notify_on),
            )
            worker_id = cur.lastrowid
            conn.commit()

        _install_for_worker(name, task_path, requirements)
        register_worker_jobs(worker_id, task_path, sched_type, sched_value, output_dir,
                             new_console=new_console, timeout_minutes=timeout_minutes,
                             env_vars=env_vars)
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
    requirements = data.get("requirements", row["requirements"] or "").strip()
    new_console = bool(data.get("new_console", bool(row["new_console"])))
    timeout_minutes = int(data.get("timeout_minutes", row["timeout_minutes"]) or 0)
    env_vars = data.get("env_vars", row["env_vars"] or "").strip()
    notify_email = data.get("notify_email", row["notify_email"] or "").strip()
    notify_on = data.get("notify_on", row["notify_on"] or "always").strip()

    with get_db() as conn:
        conn.execute(
            "UPDATE workers SET name=?, task_path=?, sched_type=?, sched_value=?, output_dir=?, group_id=?, requirements=?, new_console=?, timeout_minutes=?, env_vars=?, notify_email=?, notify_on=? WHERE id=?",
            (name, task_path, sched_type, sched_value, output_dir, group_id, requirements, int(new_console), timeout_minutes, env_vars, notify_email, notify_on, worker_id),
        )
        conn.commit()

    _install_for_worker(name, task_path, requirements)
    register_worker_jobs(worker_id, task_path, sched_type, sched_value, output_dir,
                         paused=bool(row["paused"]), new_console=new_console,
                         timeout_minutes=timeout_minutes, env_vars=env_vars)
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
    limit  = request.args.get("limit",  100, type=int)
    offset = request.args.get("offset", 0,   type=int)
    with get_db() as conn:
        rows = conn.execute(
            """SELECT triggered_at, trigger_type, success, duration_ms, error_msg
               FROM run_history WHERE worker_id=? ORDER BY id DESC LIMIT ? OFFSET ?""",
            (worker_id, limit, offset),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM run_history WHERE worker_id=?", (worker_id,)
        ).fetchone()[0]
    return jsonify({"rows": [dict(r) for r in rows], "total": total, "offset": offset, "limit": limit})

@app.route("/api/workers/<int:worker_id>/run-now", methods=["POST"])
def run_worker_now(worker_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM workers WHERE id=?", (worker_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    t = threading.Thread(
        target=_task_runner,
        args=(worker_id, row["task_path"], row["output_dir"]),
        kwargs={"trigger_type": "manual", "new_console": bool(row["new_console"]),
                "timeout_minutes": int(row["timeout_minutes"] or 0),
                "env_vars": row["env_vars"] or ""},
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
            "notify_email": c["notify_email"] or "",
            "notify_on": c["notify_on"] or "always",
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
    notify_email = data.get("notify_email", "").strip()
    notify_on = data.get("notify_on", "always").strip()
    steps = data.get("steps", [])

    if not name:
        return jsonify({"error": "Chain name required"}), 400
    if not steps:
        return jsonify({"error": "At least one step required"}), 400

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO chains (name, sched_type, sched_value, stop_on_failure, notify_email, notify_on) VALUES (?,?,?,?,?,?)",
            (name, sched_type, sched_value, stop_on_failure, notify_email, notify_on),
        )
        chain_id = cur.lastrowid
        for i, step in enumerate(steps):
            task_path = step.get("task_path", "").strip()
            if not task_path:
                continue
            if not os.path.isabs(task_path):
                task_path = os.path.abspath(task_path)
            stage = int(step.get("stage", i))
            conn.execute(
                "INSERT INTO chain_steps (chain_id, order_index, task_path, stage) VALUES (?,?,?,?)",
                (chain_id, i, task_path, stage),
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
    notify_email = data.get("notify_email", row["notify_email"] or "").strip()
    notify_on = data.get("notify_on", row["notify_on"] or "always").strip()
    steps = data.get("steps", [])

    if not name:
        return jsonify({"error": "Chain name required"}), 400

    group_id = data.get("group_id", row["group_id"])  # preserve existing if not provided

    with get_db() as conn:
        conn.execute(
            "UPDATE chains SET name=?, sched_type=?, sched_value=?, stop_on_failure=?, group_id=?, notify_email=?, notify_on=? WHERE id=?",
            (name, sched_type, sched_value, stop_on_failure, group_id, notify_email, notify_on, chain_id),
        )
        conn.execute("DELETE FROM chain_steps WHERE chain_id=?", (chain_id,))
        for i, step in enumerate(steps):
            task_path = step.get("task_path", "").strip()
            if not task_path:
                continue
            if not os.path.isabs(task_path):
                task_path = os.path.abspath(task_path)
            stage = int(step.get("stage", i))
            conn.execute(
                "INSERT INTO chain_steps (chain_id, order_index, task_path, stage) VALUES (?,?,?,?)",
                (chain_id, i, task_path, stage),
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
    limit  = request.args.get("limit",  100, type=int)
    offset = request.args.get("offset", 0,   type=int)
    with get_db() as conn:
        rows = conn.execute(
            """SELECT triggered_at, trigger_type, success, duration_ms, error_msg
               FROM run_history WHERE chain_id=? ORDER BY id DESC LIMIT ? OFFSET ?""",
            (chain_id, limit, offset),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM run_history WHERE chain_id=?", (chain_id,)
        ).fetchone()[0]
    return jsonify({"rows": [dict(r) for r in rows], "total": total, "offset": offset, "limit": limit})

# --- Email Settings ---
@app.route("/api/email-settings", methods=["GET"])
def get_email_settings():
    gmail_user = os.environ.get("GMAIL_USER", "").strip()
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    return jsonify({
        "has_credentials": bool(gmail_user and gmail_pass),
        "email": gmail_user,
    })

@app.route("/api/email-settings", methods=["POST"])
def save_email_settings():
    data = request.get_json()
    email = (data.get("email") or "").strip()
    password = (data.get("password") or "").strip()
    if not email or not password:
        return jsonify({"error": "Email and app password required"}), 400
    _save_env_key("GMAIL_USER", email)
    _save_env_key("GMAIL_APP_PASSWORD", password)
    add_log("INFO", f"Email settings saved ({email}).")
    return jsonify({"ok": True})

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

# --- Import / Export ---
@app.route("/api/export", methods=["GET"])
def export_data():
    with get_db() as conn:
        workers  = conn.execute("SELECT * FROM workers ORDER BY id").fetchall()
        groups   = conn.execute("SELECT * FROM groups ORDER BY id").fetchall()
        chains   = conn.execute("SELECT * FROM chains ORDER BY id").fetchall()
        steps    = conn.execute(
            "SELECT * FROM chain_steps ORDER BY chain_id, order_index"
        ).fetchall()

    group_map = {g["id"]: g["name"] for g in groups}
    steps_by_chain = {}
    for s in steps:
        steps_by_chain.setdefault(s["chain_id"], []).append(
            {"task_path": s["task_path"], "order_index": s["order_index"], "stage": s["stage"]}
        )

    payload = {
        "version": 1,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "groups": [{"name": g["name"]} for g in groups],
        "workers": [
            {
                "name":            w["name"],
                "task_path":       w["task_path"],
                "sched_type":      w["sched_type"],
                "sched_value":     w["sched_value"],
                "output_dir":      w["output_dir"] or "",
                "requirements":    w["requirements"] or "",
                "new_console":     bool(w["new_console"]),
                "timeout_minutes": int(w["timeout_minutes"] or 0),
                "env_vars":        w["env_vars"] or "",
                "notify_email":    w["notify_email"] or "",
                "notify_on":       w["notify_on"] or "always",
                "group_name":      group_map.get(w["group_id"]) if w["group_id"] else None,
                "paused":          bool(w["paused"]),
            }
            for w in workers
        ],
        "chains": [
            {
                "name":            c["name"],
                "sched_type":      c["sched_type"],
                "sched_value":     c["sched_value"],
                "stop_on_failure": bool(c["stop_on_failure"]),
                "notify_email":    c["notify_email"] or "",
                "notify_on":       c["notify_on"] or "always",
                "paused":          bool(c["paused"]),
                "group_name":      group_map.get(c["group_id"]) if c["group_id"] else None,
                "steps":           steps_by_chain.get(c["id"], []),
            }
            for c in chains
        ],
    }

    fname = f"redis_operator_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    from flask import Response
    return Response(
        json.dumps(payload, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )

@app.route("/api/import", methods=["POST"])
def import_data():
    data = request.get_json()
    if not data or data.get("version") != 1:
        return jsonify({"error": "Invalid or unsupported export format (expected version 1)"}), 400

    imported_groups  = 0
    imported_workers = 0
    imported_chains  = 0
    skipped          = 0

    # 1. Groups — create missing ones, build name→id map
    group_name_to_id = {}
    with get_db() as conn:
        existing_groups = {g["name"]: g["id"]
                           for g in conn.execute("SELECT id, name FROM groups").fetchall()}
    for g in data.get("groups", []):
        name = g.get("name", "").strip()
        if not name:
            continue
        if name in existing_groups:
            group_name_to_id[name] = existing_groups[name]
        else:
            with get_db() as conn:
                cur = conn.execute("INSERT INTO groups (name) VALUES (?)", (name,))
                group_name_to_id[name] = cur.lastrowid
                conn.commit()
            imported_groups += 1

    # 2. Workers
    with get_db() as conn:
        existing_workers = {r["name"] for r in conn.execute("SELECT name FROM workers").fetchall()}
    for w in data.get("workers", []):
        name = w.get("name", "").strip()
        if not name or name in existing_workers:
            skipped += 1
            continue
        task_path       = w.get("task_path", "")
        sched_type      = w.get("sched_type", "interval")
        sched_value     = w.get("sched_value", "1h")
        output_dir      = w.get("output_dir", "")
        requirements    = w.get("requirements", "")
        new_console     = int(bool(w.get("new_console", False)))
        timeout_minutes = int(w.get("timeout_minutes", 0) or 0)
        env_vars        = w.get("env_vars", "")
        notify_email    = w.get("notify_email", "")
        notify_on       = w.get("notify_on", "always")
        paused          = int(bool(w.get("paused", False)))
        group_id        = group_name_to_id.get(w.get("group_name")) if w.get("group_name") else None
        with get_db() as conn:
            cur = conn.execute(
                """INSERT INTO workers
                   (name, task_path, sched_type, sched_value, output_dir, requirements,
                    new_console, timeout_minutes, env_vars, notify_email, notify_on, group_id, paused)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (name, task_path, sched_type, sched_value, output_dir, requirements,
                 new_console, timeout_minutes, env_vars, notify_email, notify_on, group_id, paused),
            )
            worker_id = cur.lastrowid
            conn.commit()
        if not paused:
            try:
                register_worker_jobs(worker_id, task_path, sched_type, sched_value,
                                     output_dir, new_console=bool(new_console),
                                     timeout_minutes=timeout_minutes, env_vars=env_vars)
            except Exception as e:
                add_log("ERROR", f"Import: failed to schedule worker '{name}': {e}")
        imported_workers += 1

    # 3. Chains
    with get_db() as conn:
        existing_chains = {r["name"] for r in conn.execute("SELECT name FROM chains").fetchall()}
    for c in data.get("chains", []):
        name = c.get("name", "").strip()
        if not name or name in existing_chains:
            skipped += 1
            continue
        sched_type      = c.get("sched_type", "interval")
        sched_value     = c.get("sched_value", "1h")
        stop_on_failure = int(bool(c.get("stop_on_failure", True)))
        notify_email    = c.get("notify_email", "")
        notify_on       = c.get("notify_on", "always")
        paused          = int(bool(c.get("paused", False)))
        group_id        = group_name_to_id.get(c.get("group_name")) if c.get("group_name") else None
        steps           = c.get("steps", [])
        with get_db() as conn:
            cur = conn.execute(
                """INSERT INTO chains (name, sched_type, sched_value, stop_on_failure, notify_email, notify_on, paused, group_id)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (name, sched_type, sched_value, stop_on_failure, notify_email, notify_on, paused, group_id),
            )
            chain_id = cur.lastrowid
            for i, step in enumerate(steps):
                tp = step.get("task_path", "")
                if tp:
                    stage = int(step.get("stage", i))
                    conn.execute(
                        "INSERT INTO chain_steps (chain_id, order_index, task_path, stage) VALUES (?,?,?,?)",
                        (chain_id, i, tp, stage),
                    )
            conn.commit()
        if not paused:
            try:
                register_chain_jobs(chain_id, sched_type, sched_value)
            except Exception as e:
                add_log("ERROR", f"Import: failed to schedule chain '{name}': {e}")
        imported_chains += 1

    add_log("INFO",
        f"Import complete — {imported_workers} worker(s), {imported_chains} chain(s), "
        f"{imported_groups} group(s) added, {skipped} skipped (already exist).")
    return jsonify({
        "ok": True,
        "imported_workers":  imported_workers,
        "imported_chains":   imported_chains,
        "imported_groups":   imported_groups,
        "skipped":           skipped,
    })

# --- Windows startup (Registry Run key) ---
_REG_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_REG_VALUE_NAME = "Redis Operator"

def _get_reg_command() -> str:
    """Build the auto-start command string."""
    python_exe = sys.executable
    launch_py = str(BASE_DIR / "launch.py")
    return f'"{python_exe}" "{launch_py}"'

@app.route("/api/service/status", methods=["GET"])
def service_status():
    if sys.platform != "win32":
        return jsonify({"installed": False, "supported": False})
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY_PATH, 0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, _REG_VALUE_NAME)
            installed = True
        except FileNotFoundError:
            installed = False
        finally:
            winreg.CloseKey(key)
    except Exception:
        installed = False
    return jsonify({"installed": installed, "supported": True})

@app.route("/api/service/install", methods=["POST"])
def service_install():
    if sys.platform != "win32":
        return jsonify({"error": "Only supported on Windows"}), 400
    try:
        import winreg
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _REG_KEY_PATH, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, _REG_VALUE_NAME, 0, winreg.REG_SZ, _get_reg_command())
        winreg.CloseKey(key)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    add_log("INFO", "Auto-start installed (runs at logon).")
    return jsonify({"ok": True})

@app.route("/api/service/uninstall", methods=["POST"])
def service_uninstall():
    if sys.platform != "win32":
        return jsonify({"error": "Only supported on Windows"}), 400
    try:
        import winreg
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _REG_KEY_PATH, 0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(key, _REG_VALUE_NAME)
        winreg.CloseKey(key)
    except FileNotFoundError:
        pass  # already removed
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    add_log("INFO", "Auto-start removed.")
    return jsonify({"ok": True})

# --- Update check ---
_update_info: dict = {"available": False, "latest": None, "url": None}

def _version_tuple(v: str):
    try:
        return tuple(int(x) for x in v.lstrip("v").split("."))
    except Exception:
        return (0,)

def _check_for_update():
    global _update_info
    try:
        import urllib.request as _ur
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        req = _ur.Request(url, headers={"User-Agent": f"redis-operator/{VERSION}"})
        with _ur.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        tag   = data.get("tag_name", "").lstrip("v")
        html  = data.get("html_url", "")
        if tag and _version_tuple(tag) > _version_tuple(VERSION):
            _update_info = {"available": True, "latest": tag, "url": html}
            add_log("INFO", f"Update available: v{tag} — {html}")
        else:
            _update_info = {"available": False, "latest": tag, "url": html}
    except Exception:
        pass  # silently ignore network errors

@app.route("/api/update-check", methods=["GET"])
def update_check():
    return jsonify({**_update_info, "current": VERSION})

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
                                 r["sched_value"], r["output_dir"],
                                 new_console=bool(r["new_console"]),
                                 timeout_minutes=int(r["timeout_minutes"] or 0),
                                 env_vars=r["env_vars"] or "")
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
    scheduler = BackgroundScheduler(timezone=_get_local_tz())
    scheduler.start()
    restore_workers()
    restore_chains()
    atexit.register(lambda: scheduler.shutdown(wait=False))
    atexit.register(stop_redis)
    threading.Thread(target=_check_for_update, daemon=True).start()
    return app

application = create_app()

if __name__ == "__main__":
    application.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
