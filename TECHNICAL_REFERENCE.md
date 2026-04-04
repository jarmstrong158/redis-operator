# Redis Operator — Technical Reference

Version: 3.0
Backend: Flask + APScheduler + SQLite + Redis
GitHub: jarmstrong158/redis-operator

---

## API Endpoints

### Workers

#### `GET /api/workers`
Returns all workers with computed schedule metadata.

Response fields per worker:
- `id` (int)
- `name` (string)
- `task_path` (string) — absolute path to .py/.bat/.sh/.cmd
- `sched_type` (string) — "fixed", "interval", or "cron"
- `sched_value` (string) — schedule expression (see Schedule Types)
- `output_dir` (string) — working directory for subprocess, defaults to task file's directory
- `requirements` (string) — comma-separated pip packages
- `new_console` (bool) — Windows only: spawn in new console window
- `timeout_minutes` (int) — 0 = no timeout
- `env_vars` (string) — KEY=VALUE lines, newline-separated
- `paused` (bool)
- `group_id` (int|null)
- `next_trigger` (string) — "YYYY-MM-DD HH:MM:SS" or "—" if paused/none
- `remaining_today` (int) — scheduled fires remaining before midnight, 0 if paused
- `schedule_display` (string) — human-readable label: "Fixed: HH:MM,HH:MM", "Cron: ...", "Every Xh Ym"
- `last_run_status` (string|null) — "ok", "error", or null (never run)
- `entity_type` — always "worker"

#### `POST /api/workers`
Create one or more workers. Accepts a single object or an array.

Body fields:
- `name` (string, required)
- `task_path` (string, required) — converted to absolute if relative
- `sched_type` (string, default "interval")
- `sched_value` (string, default "1h")
- `output_dir` (string, optional) — converted to absolute if relative
- `requirements` (string, optional) — comma-separated packages, pip-installed on create
- `new_console` (bool, default false)
- `timeout_minutes` (int, default 0)
- `env_vars` (string, optional)

Returns `201 {"added": [ids], "errors": [...]}` or `400` if all failed.

Side effects: Auto-detects missing imports in .py files via AST parsing, pip-installs them + declared requirements. Registers APScheduler jobs immediately (unless paused is somehow set, but POST doesn't accept paused — new workers always start active).

#### `PUT /api/workers/<id>`
Update a worker. Same body fields as POST (name and task_path required). Additionally:
- `group_id` (int|null, optional) — preserved from DB if not sent

Preserves current paused state. Re-registers scheduler jobs with new config. Re-runs dependency install.

#### `POST /api/workers/<id>/pause`
Toggle pause state. No body needed.
- Pausing: removes all APScheduler jobs for this worker.
- Resuming: re-registers jobs from DB config.

Returns `{"paused": bool}`.

#### `POST /api/workers/<id>/run-now`
Fire worker immediately in a daemon thread. Trigger type = "manual" (affects log level: MANUAL instead of FIRE). Records run in history. Works even if worker is paused.

#### `GET /api/workers/<id>/history`
Query params: `limit` (int, default 100), `offset` (int, default 0).

Returns `{"rows": [...], "total": int, "offset": int, "limit": int}`.

Row fields: `triggered_at`, `trigger_type` ("scheduled"|"manual"), `success` (0|1), `duration_ms`, `error_msg`.

#### `DELETE /api/workers/<id>`
Deletes worker from DB, removes APScheduler jobs. Does NOT delete run_history rows (they remain orphaned).

#### `POST /api/workers/pause-all`
Pauses all active workers. Sets paused=1 in DB, removes all worker jobs.

#### `DELETE /api/workers/all`
Deletes all workers from DB, removes all worker jobs.

#### `POST /api/workers/<id>/assign-group`
Body: `{"group_id": int|null}`. Assigns worker to a group or unassigns (null).

---

### Chains

#### `GET /api/chains`
Returns all chains with steps and computed metadata.

Response fields per chain:
- `id`, `name`, `sched_type`, `sched_value`, `stop_on_failure` (bool), `paused` (bool), `group_id`
- `steps` (array) — each: `{id, chain_id, order_index, task_path, stage}`
- `next_trigger`, `remaining_today`, `schedule_display`, `last_run_status`, `entity_type` ("chain")

#### `POST /api/chains`
Body:
- `name` (string, required)
- `sched_type` (string, default "interval")
- `sched_value` (string, default "1h")
- `stop_on_failure` (bool, default true)
- `steps` (array, required, min 1) — each: `{task_path: string, stage: int}`
  - `stage` defaults to the step's array index if not provided
  - task_path converted to absolute if relative

Returns `201 {"ok": true, "chain_id": int}`.

#### `PUT /api/chains/<id>`
Same body as POST. Additionally accepts `group_id`. Steps are fully replaced (all old steps deleted, new ones inserted). Preserves paused state.

#### `POST /api/chains/<id>/pause`
Toggle pause state. Same behavior as worker pause.

#### `POST /api/chains/<id>/run-now`
Fire chain immediately. Trigger type = "manual".

#### `GET /api/chains/<id>/history`
Same schema as worker history. Queries by chain_id instead of worker_id.

#### `DELETE /api/chains/<id>`
Deletes chain and all chain_steps (manual cascade). Removes APScheduler jobs.

#### `POST /api/chains/<id>/assign-group`
Body: `{"group_id": int|null}`.

---

### Groups

#### `GET /api/groups`
Returns all groups ordered by `order_index, id`. Fields: `id`, `name`, `order_index`, `created_at`.

#### `POST /api/groups`
Body: `{"name": string}`. Name must be unique (409 on conflict). Returns `201 {"ok": true, "group_id": int}`.

#### `PUT /api/groups/<id>`
Body: `{"name": string}`. 409 on duplicate name.

#### `DELETE /api/groups/<id>`
Sets `group_id=NULL` on all workers and chains in this group, then deletes the group. Does NOT delete members.

#### `POST /api/groups/<id>/assign`
Body: `{"entity_type": "worker"|"chain", "entity_id": int}`. Assigns entity to this group.

---

### Profiles

#### `GET /api/profiles`
Returns `[{id, name, created_at}]` sorted by name.

#### `POST /api/profiles`
Body: `{"name": string, "config": object}`. Uses INSERT OR REPLACE — same name overwrites. The `config` object is opaque JSON (typically the full worker/chain/group state snapshot from the frontend). Returns 201.

#### `GET /api/profiles/<id>`
Returns `{"name": string, "config": object}` where config is the parsed JSON.

#### `DELETE /api/profiles/<id>`
Deletes profile.

---

### Templates

#### `POST /api/templates`
Body:
- `template_type` (string, required) — one of: "folder_backup", "file_cleanup", "folder_watcher", "uptime_check", "open_url"
- `config` (object) — template-specific fields (see below)
- `worker_name` (string, required)
- `sched_type` (string, default "fixed")
- `sched_value` (string, default "09:00")

Generates a Python script at `<BASE_DIR>/templates/generated/<safe_name>_<template_type>.py` and registers it as a worker.

Template configs:

**folder_backup**: `{source: string, dest: string, keep: int}` — copies source tree to dest with timestamped name, prunes to `keep` most recent.

**file_cleanup**: `{folder: string, pattern: string, days: int}` — deletes files matching glob `pattern` older than `days` days. Defaults: pattern="*.tmp", days=7.

**folder_watcher**: `{watch: string, rules: [{ext: string, dest: string}]}` — moves files from watch dir by extension.

**uptime_check**: `{url: string, log_file: string}` — HTTP ping, logs UP/DOWN, raises RuntimeError on DOWN (so the run records failure).

**open_url**: `{url: string}` — opens URL in default browser.

All generated scripts use stdlib only. Each has a `run()` function but is executed as a subprocess (the run() function is the entry point only if the script is structured that way — actually, generated scripts define run() but have no `if __name__` guard, so they only define it; the subprocess just imports and... wait, looking at the code more carefully: the generated scripts define `run()` but have no `if __name__ == "__main__": run()` block. However, _task_runner runs them via `subprocess.run([sys.executable, task_path])` which would execute the top-level code only. Since the functions are just defined at top level with `def run():`, they WON'T actually execute unless called. This is a potential issue — BUT the example_task.py also just defines `run()` with no main guard. Let me check...

Actually, looking at example_task.py more carefully and the template generators: the generated scripts define constants at module level and a `run()` function but nothing calls `run()` at module level. This means running them as subprocess would only define the function without executing it. This appears to be a design quirk — either there's something I'm missing or the generated template scripts don't actually do anything when run. The example_task.py pattern suggests tasks are expected to define a `run()` function that gets called... but the _task_runner subprocess execution doesn't import and call `run()`, it just runs the script as a subprocess.

**Edge case: Template scripts as-generated will NOT execute their `run()` function** because they lack a `if __name__ == "__main__": run()` block and are executed as subprocesses, not imported.

---

### Import / Export

#### `GET /api/export`
Returns a JSON file download with Content-Disposition header. Filename: `redis_operator_YYYYMMDD_HHMMSS.json`.

Export schema:
```json
{
  "version": 1,
  "exported_at": "YYYY-MM-DD HH:MM:SS",
  "groups": [{"name": "..."}],
  "workers": [{
    "name", "task_path", "sched_type", "sched_value", "output_dir",
    "requirements", "new_console", "timeout_minutes", "env_vars",
    "group_name" (string|null, resolved from group_id),
    "paused"
  }],
  "chains": [{
    "name", "sched_type", "sched_value", "stop_on_failure", "paused",
    "group_name",
    "steps": [{"task_path", "order_index", "stage"}]
  }]
}
```

Groups are exported by name only (no IDs). Workers/chains reference groups by `group_name`.

#### `POST /api/import`
Body: the export JSON object. Requires `version: 1`.

Import behavior:
1. Groups: creates any that don't exist by name. Existing groups are reused (matched by name).
2. Workers: skips any with a name that already exists in DB. Creates others, assigns group_id by group_name lookup. Non-paused workers are scheduled immediately. Failed scheduling logs error but doesn't abort.
3. Chains: same skip-by-name logic. Steps are created with their stage values.

Returns `{"ok": true, "imported_workers": int, "imported_chains": int, "imported_groups": int, "skipped": int}`.

**Edge case**: Import deduplication is by name only. A worker with the same name but different config will be skipped, not updated.

---

### AI Analysis

#### `GET /api/api-key-status`
Returns `{"has_key": bool}` — checks `ANTHROPIC_API_KEY` env var.

#### `POST /api/save-api-key`
Body: `{"key": string}`. Writes/updates ANTHROPIC_API_KEY in `.env` file. Also sets it in `os.environ` immediately.

#### `POST /api/analyze-logs`
Body: `{"entries": [{ts, level, msg}]}`. Sends ERROR entries to Claude claude-sonnet-4-6 with a system prompt about Redis Operator context. Returns `{"analysis": string}`. Uses streaming internally but returns the full response (not streamed to client).

---

### Logs

#### `GET /api/logs`
Query param: `since` (int, default 0) — index offset into the circular buffer.

Returns `{"entries": [...], "total": int}` where total is current buffer length (max 500).

Each entry: `{ts: "YYYY-MM-DD HH:MM:SS", level: string, msg: string}`.

The `since` parameter slices the buffer: `list(LOG_BUFFER)[since:]`. As the deque is maxlen=500, old entries drop off the front. The frontend polls with `since=total` from the previous response to get only new entries.

**Edge case**: If the buffer wraps (total was 500, items got evicted), using the old `since` value could miss entries or return stale offsets. The frontend would need to detect `total < since` and reset.

---

### Redis Status

#### `GET /api/redis-status`
Returns `{"running": bool}` — checks TCP connection to 127.0.0.1:6379.

---

### File Browser

#### `GET /api/browse`
Query param: `mode` — "file" (default) or "dir".

Opens a native tkinter file dialog (blocks the request until user picks or cancels). Returns `{"path": string}` or `{"path": ""}` if cancelled.

File mode filters: `*.py *.bat *.sh *.cmd` and "All files".

---

### Windows Startup

#### `GET /api/service/status`
Returns `{"installed": bool, "supported": bool}`. Non-Windows always returns `{installed: false, supported: false}`.

#### `POST /api/service/install`
Creates Windows Task Scheduler task named "Redis Operator" with `/sc ONLOGON` trigger. Command: `"<python.exe>" "<BASE_DIR>/launch.py"`. Uses `/f` flag to force-replace existing.

#### `POST /api/service/uninstall`
Deletes the "Redis Operator" scheduled task.

---

### Update Check

#### `GET /api/update-check`
Returns `{"available": bool, "latest": string|null, "url": string|null, "current": "3.0"}`.

Checked once at startup via GitHub API (`/repos/{GITHUB_REPO}/releases/latest`). Compares version tuples. Network errors are silently ignored.

---

### Shutdown

#### `POST /api/shutdown`
Attempts werkzeug shutdown, falls back to `SIGTERM` to self. Returns `{"ok": true}`.

---

## Worker Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | required | Display name. Used as dedup key in import. |
| `task_path` | string | required | Absolute path to .py, .bat, .sh, or .cmd file. Converted to absolute on save if relative. |
| `sched_type` | string | "interval" | "fixed", "interval", or "cron" |
| `sched_value` | string | "1h" | Schedule expression (format depends on sched_type) |
| `output_dir` | string | "" | Working directory for subprocess. Falls back to: task file's directory → BASE_DIR |
| `requirements` | string | "" | Comma-separated pip packages to install |
| `new_console` | bool | false | Windows: CREATE_NEW_CONSOLE flag. When true, stdout/stderr are NOT captured (empty strings). |
| `timeout_minutes` | int | 0 | Subprocess timeout in minutes. 0 = no timeout. |
| `env_vars` | string | "" | KEY=VALUE pairs, newline-separated. Merged over os.environ for subprocess. Supports comments (#) and blank lines. |
| `paused` | bool | false | When true, no APScheduler jobs exist. |
| `group_id` | int\|null | null | FK to groups table. |

---

## Chain Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | required | Display name. Dedup key in import. |
| `sched_type` | string | "interval" | Same as worker. |
| `sched_value` | string | "1h" | Same as worker. |
| `stop_on_failure` | bool | true | If true, stops executing remaining stages when any step in a stage fails. |
| `paused` | bool | false | Same as worker. |
| `group_id` | int\|null | null | Same as worker. |

### Chain Steps

| Field | Type | Description |
|---|---|---|
| `chain_id` | int | FK to chains |
| `order_index` | int | Position in the step list (0-based) |
| `task_path` | string | Absolute path to script |
| `stage` | int | Stage number for parallel grouping |

### How Stages Work

Steps are sorted by `(stage, order_index)`. Steps with the same `stage` value run in parallel via `ThreadPoolExecutor(max_workers=len(stage_steps))`. Different stages run sequentially in ascending order.

Example: steps with stages `[0, 0, 1, 2, 2]` → stage 0 (2 steps parallel) → stage 1 (1 step) → stage 2 (2 steps parallel).

### How Stop-on-Failure Works

When `stop_on_failure=true`:
- If any step in a parallel stage fails, `overall_success` is set to false.
- Before starting the NEXT stage, the chain checks `overall_success`. If false, it breaks out of the stage loop.
- Steps already running in the current parallel stage continue to completion (ThreadPoolExecutor doesn't cancel in-flight futures).
- For sequential stages (1 step), failure immediately stops the chain before the next stage.

When `stop_on_failure=false`: All stages execute regardless of failures.

### Chain Step Execution Details

- Chain steps do NOT support `new_console`, `timeout_minutes`, or `env_vars` (those are worker-only features).
- Chain steps do support auto-pip-install on ModuleNotFoundError (same retry-once logic as workers).
- Chain steps run in the task file's directory (no configurable output_dir).
- One `run_history` row is created per chain execution (not per step).

---

## Schedule Types

### Fixed (`sched_type: "fixed"`)
Format: `"HH:MM"` or `"HH:MM,HH:MM,..."` (comma-separated times).

Each time creates a separate CronTrigger job: `CronTrigger(hour=H, minute=M)`. Job IDs: `w<id>_t0`, `w<id>_t1`, etc.

### Interval (`sched_type: "interval"`)
Format: `"Xh Ym"`, `"Xh"`, or `"Ym"` (case-insensitive, spaces optional).

Examples: "1h", "30m", "2h 15m", "1h30m".

Creates a single `IntervalTrigger(hours=X, minutes=Y)`. If both are 0, falls back to 5 minutes. Job ID: `w<id>_i0`.

Parsing: strips spaces, finds "h" to split hours, finds "m" to split minutes. Fragile — "1h30" without trailing "m" would parse hours=1, minutes=0 (the "30" after "h" is leftover in `parts` but no "m" is found).

### Cron (`sched_type: "cron"`)
Format: standard 5-field crontab expression (minute hour day month weekday).

Creates a single `CronTrigger.from_crontab(value)`. Job ID: `w<id>_c0`.

### Shared Behavior
- All triggers use `misfire_grace_time=60` (seconds).
- All triggers use the local timezone (via `tzlocal.get_localzone()`, fallback to UTC).
- Paused workers/chains have NO scheduler jobs (jobs are removed, not paused).

---

## Job ID Format

Workers: `w<worker_id>_<suffix>` where suffix is:
- `t0`, `t1`, ... for fixed schedule times
- `i0` for interval
- `c0` for cron

Chains: `c<chain_id>_<suffix>` with same suffix patterns.

This prefix scheme is used to find/remove all jobs for a given worker/chain.

---

## Run History

Table: `run_history`

| Column | Type | Description |
|---|---|---|
| `id` | int | Auto-increment PK |
| `worker_id` | int\|null | Set for worker runs |
| `chain_id` | int\|null | Set for chain runs |
| `triggered_at` | text | SQLite datetime('now') default |
| `trigger_type` | text | "scheduled" or "manual" |
| `success` | int | 1 or 0 |
| `duration_ms` | int | Wall-clock execution time |
| `error_msg` | text | Empty string on success; traceback or timeout message on failure |

One row per worker execution. One row per chain execution (not per step). Worker deletions do NOT cascade to run_history — orphaned rows remain.

---

## Log Levels

| Level | Color | Trigger |
|---|---|---|
| `INFO` | default | Worker registered, updated, restored, dependency installs, chain stage info, import results, service install/uninstall, profile save/delete, update available, Redis status |
| `OK` | green | Worker completed successfully, chain completed, chain step completed, pip install succeeded |
| `FIRE` | orange | Scheduled worker/chain fired |
| `MANUAL` | blue | Manual run-now worker/chain fired |
| `ERROR` | red | Worker failed, chain step failed, chain failed, pip install failed, worker restore failed, Redis start failed, import scheduling failed |
| `PAUSE` | yellow | Worker/chain paused, all workers paused |
| `DELETE` | red | Worker/chain/group deleted, all workers deleted |

Buffer: in-memory `deque(maxlen=500)`, thread-safe via `LOG_LOCK`. No persistence — logs are lost on restart.

---

## Profiles

Profiles store an opaque `config_json` blob. The backend doesn't interpret this — it's a frontend-defined snapshot. The frontend saves the current worker/chain/group state as JSON and restores by re-creating entities.

Profile names are UNIQUE — saving with an existing name overwrites (INSERT OR REPLACE).

---

## Import/Export vs Profiles

- **Export/Import**: Structured format with `version: 1`. Backend extracts workers/chains/groups from DB, serializes with group names (not IDs). Import creates entities, skipping duplicates by name.
- **Profiles**: Opaque JSON blob stored in DB. Frontend interprets the config. No dedup logic.

---

## Groups

Groups are named containers. Workers and chains have an optional `group_id` FK.

- Groups have `order_index` (default 0) for display ordering but no API to update it.
- Group names must be UNIQUE.
- Deleting a group sets `group_id=NULL` on all members (unassigns, doesn't delete).
- Groups have no functional effect on scheduling — purely organizational.

---

## Task Execution Details

### Supported File Types
- `.py` — executed with `sys.executable` (same Python that runs the server)
- `.bat`, `.cmd` — executed directly (Windows batch)
- `.sh` — executed directly (requires executable bit on Unix)
- Anything else → `ValueError: Unsupported file type`

### Working Directory
Priority: `output_dir` (if set) → `os.path.dirname(task_path)` (if non-empty) → `BASE_DIR`

### Auto-Dependency Install
1. On worker create/update: AST-parses .py file, finds all imports, filters out stdlib, checks `importlib.util.find_spec()`, pip-installs missing ones. Also installs declared `requirements`.
2. On execution failure: if stderr contains "ModuleNotFoundError"/"No module named", extracts module name via regex, pip-installs it, retries the script ONCE.
3. Both mechanisms work for workers and chain steps.

### Environment Variables
Worker `env_vars` are parsed as KEY=VALUE lines (supports blank lines and `#` comments). Merged over `os.environ` — worker env vars override system vars. Passed to `subprocess.run(env=...)`.

Chain steps do NOT support per-step env vars.

### new_console (Windows)
When `new_console=True` on Windows: uses `subprocess.Popen` with `CREATE_NEW_CONSOLE` flag. The process runs in a visible console window. stdout/stderr are NOT captured (returned as empty strings). `proc.wait(timeout=...)` is used for timeout.

On non-Windows, `new_console` is accepted but ignored (the normal subprocess.run path is taken).

### Timeout
`timeout_minutes` is converted to seconds. `subprocess.run(timeout=seconds)` or `proc.wait(timeout=seconds)`. On `TimeoutExpired`, the error is logged and run_history records failure.

---

## Remaining Today Calculation

`_remaining_today_for_prefix(prefix)` counts scheduled fires before midnight:
1. For each job matching the prefix, if `next_run_time` is between now and 23:59:59 today, count it.
2. For IntervalTrigger jobs, iteratively calls `trigger.get_next_fire_time()` to count all future fires today.
3. For CronTrigger (fixed schedule), each time is a separate job, so each counts as 0 or 1.

**Edge case**: The interval counting loop calls `trigger.get_next_fire_time(cur, cur)` which may not advance properly depending on APScheduler internals. If `get_next_fire_time` returns the same time, the loop could infinite-loop. In practice, IntervalTrigger should advance by the interval.

---

## SQLite Schema Migrations

On startup, `init_db()` adds columns that don't exist yet:
- `workers.group_id`, `workers.requirements`, `workers.new_console`, `workers.timeout_minutes`, `workers.env_vars`
- `chain_steps.stage` — on migration, sets `stage = order_index` for existing steps (preserves sequential behavior)

This allows upgrading from V1 databases without data loss.

---

## Redis

Redis is started on launch and stopped on exit (via atexit). It's checked by TCP connect to 127.0.0.1:6379.

Binary search order: bundled `redis_bundled/redis-server.exe` → system PATH.

**Important**: Despite the name "Redis Operator", Redis is used minimally. The actual state management uses SQLite (persistence) and APScheduler's MemoryJobStore (scheduling). The Redis client is stored in `_redis_client` but doesn't appear to be used for any application logic in the current codebase. Redis serves as an available service rather than a core dependency for the scheduling logic.

---

## Startup Sequence

1. `_load_dotenv()` — load `.env` into `os.environ` (only unset keys)
2. Create `templates/generated/` directory
3. `init_db()` — create tables, run migrations
4. `start_redis()` — connect or launch Redis
5. Create `BackgroundScheduler` with local timezone
6. `scheduler.start()`
7. `restore_workers()` — re-register non-paused workers from DB
8. `restore_chains()` — re-register non-paused chains from DB
9. Register atexit handlers (scheduler shutdown, Redis stop)
10. Background thread: check GitHub for updates

---

## launch.py Entry Point

- Detects frozen (PyInstaller) vs source
- Starts Flask in a daemon thread on 127.0.0.1:5000
- Opens browser to http://127.0.0.1:5000
- Creates system tray icon (pystray + Pillow, optional) with "Open Dashboard" and "Quit" options
- If no tray support, blocks on `Event.wait()` until interrupted
- Graceful shutdown via atexit: stops scheduler, stops Redis

---

## Edge Cases and Non-Obvious Behaviors

1. **Template scripts don't execute**: Generated template scripts define a `run()` function but have no `if __name__ == "__main__": run()` guard. When executed as a subprocess, the function is defined but never called. (The example_task.py correctly has this guard, but the template generators omit it.)

2. **Import dedup is name-only**: Workers/chains with the same name are skipped on import even if their config differs. No update/merge logic.

3. **Run history not cleaned up**: Deleting a worker/chain leaves orphaned run_history rows. No cascade, no cleanup.

4. **Pause is a toggle**: `POST .../pause` flips the current state. There's no way to explicitly set paused=true or paused=false — it always toggles.

5. **Worker creation always active**: POST /api/workers doesn't accept a `paused` field. New workers always start active. Import does honor the paused field.

6. **No concurrent run protection**: If a worker/chain is still running when the next trigger fires, another instance starts. No locking or "skip if running" logic.

7. **Log buffer wraparound**: The `since` offset can become stale if the deque wraps. Client needs to handle `total < since`.

8. **Interval parsing fragility**: "1h30" (no trailing "m") parses as 1h 0m, not 1h 30m. The "m" delimiter is required.

9. **Chain step counter display**: The step numbering in logs (`step X/Y`) uses a running counter that increments by the number of steps in each stage before they execute, so all parallel steps in a stage show the same step number range.

10. **new_console suppresses output**: When new_console=True, stdout/stderr are empty strings — no output is captured or logged. Only the return code determines success/failure.

11. **Profiles are opaque**: The backend stores and returns raw JSON. It doesn't validate the config structure or use it to restore state — that's entirely frontend logic.

12. **Group order_index unused**: Groups have an `order_index` column but no API to modify it. All groups default to order_index=0.

13. **export uses group names, not IDs**: This means import works across different databases (IDs differ), but group name collisions are handled by reusing the existing group.

14. **Scheduler timezone**: Uses tzlocal for local timezone. Falls back to UTC if tzlocal fails. All cron/fixed triggers fire in local time.

15. **misfire_grace_time=60**: If the scheduler misses a fire time by more than 60 seconds (e.g., system was sleeping), the job is skipped rather than catching up.

16. **File browser blocks request**: `GET /api/browse` opens a tkinter dialog that blocks the HTTP response until the user picks a file or cancels. This is a synchronous blocking call.

17. **Chain steps ignore worker-level features**: Chain steps don't support output_dir, new_console, timeout_minutes, env_vars, or requirements. They always run in the task file's directory with default env.

18. **Pause All only pauses workers**: The "Pause All" button calls `POST /api/workers/pause-all` which only pauses workers, not chains. Similarly, "Delete All" only deletes workers. Chains are unaffected.

19. **Profile save only captures workers**: When saving a profile, the frontend fetches `/api/workers` and saves only worker data (name, task_path, sched_type, sched_value, output_dir). It does NOT capture chains, groups, requirements, env_vars, timeout, or new_console. Profiles are a subset snapshot.

20. **Group pause/delete is client-side fan-out**: Pausing or deleting all items in a group is done by the frontend issuing individual pause/delete requests in parallel via `Promise.all`. There's no backend bulk-group endpoint.

---

## Frontend Behavior Layer

### Polling & Refresh Intervals

| What | Endpoint(s) | Interval | Notes |
|---|---|---|---|
| Workers + Chains | `GET /api/workers` + `GET /api/chains` (parallel) | 4 seconds | Both fetched together in `refreshWorkers()`. Results merged into a single combined array for rendering. |
| Groups | `GET /api/groups` | 15 seconds | Also triggers `renderGroupChips()` and repopulates group dropdowns in modals. |
| Redis status | `GET /api/redis-status` | 8 seconds | Updates the header badge dot (green/red) and text. |
| Logs | `GET /api/logs?since=<offset>` | 3 seconds | Appends new entries to local `logEntries` array. Only fetches entries since last known offset. |
| Profiles | `GET /api/profiles` | On-demand only | Refreshed after save/delete/import, NOT polled. |
| Update check | `GET /api/update-check` | Once at startup | Shows banner if newer version available. Dismissible via X button (no persistence — reappears on reload). |

### Startup Sequence (`init()` on DOMContentLoaded)

1. `addWorkerCard()` — create the first empty worker card in the form
2. `refreshGroups()` → then `refreshWorkers()` (chained, groups must load first for dropdowns)
3. `refreshProfiles()` — populate profile list and dropdown
4. `checkRedisStatus()` — initial Redis check
5. `checkForUpdate()` — one-time update check
6. `pollLogs()` — initial log fetch
7. Start all `setInterval` timers

### Worker/Chain Table Rendering

The table combines workers and chains into a single list. Each row shows:
- ID, name, task file (path for workers, "chain" badge + step count for chains)
- Schedule badge, next trigger time (or "Paused"), remaining today count
- **Last Run dot**: green (ok), red (error), or gray (none/never run). Clickable → opens history modal.
- Action buttons: Edit, Run Now, Pause/Resume toggle, Group dropdown, Delete

**Grouping**: Items with `group_id` are rendered under collapsible group header rows. Ungrouped items appear first. Groups display in the order returned by `GET /api/groups` (by `order_index, id`).

### What Happens When a Worker Fires / Errors / Completes

The frontend has NO real-time events (no WebSocket, no SSE). All status updates arrive through polling:

1. **Worker fires**: The backend logs a FIRE or MANUAL entry. Within 3 seconds, `pollLogs()` picks it up and renders it in the debug log panel (orange for FIRE, purple for MANUAL).
2. **Worker completes**: The backend logs an OK entry and writes a run_history row. Within 4 seconds, `refreshWorkers()` re-fetches worker data including the updated `last_run_status` ("ok"), and the history dot turns green.
3. **Worker errors**: The backend logs an ERROR entry and writes a run_history row with `success=0`. Within 3-4 seconds, the log panel shows the red ERROR entry. The history dot turns red on next worker refresh. The "Analyze Errors" button becomes enabled.
4. **Toast notifications**: Only shown for user-initiated actions (run-now, pause, delete, save, etc.). NOT shown for scheduled fires/completions — those are log-only.

### Debug Log Panel

**Structure**: Fixed-height (220px) scrollable panel with monospace entries.

**Polling**: Every 3 seconds, `pollLogs()` calls `GET /api/logs?since=<logOffset>`. New entries are appended to the local `logEntries` array. `logOffset` tracks the cumulative count of entries received.

**Level filtering**: 7 toggle pills at the top: INFO, OK, FIRE, MANUAL, ERROR, PAUSE, DELETE. All active by default. Stored in `activeLevels` Set. Toggling a pill adds/removes the level and re-renders. This is client-side filtering — all entries are fetched regardless of filter state.

**Search**: Text input filters entries by substring match (case-insensitive) on `msg` or `ts` fields. Also client-side.

**Auto-scroll**: Checkbox (default: checked). When enabled AND no search text is active, the panel auto-scrolls to the bottom after each render (`panel.scrollTop = panel.scrollHeight`). When disabled or when searching, scroll position is preserved.

**Clear**: The "Clear" button does NOT delete entries from the backend buffer. It sets `logDisplayOffset = logEntries.length`, which causes `renderLogs()` to slice from that offset forward. Effectively hides all current entries. New entries arriving after clear are shown.

**Log level colors**:
- INFO: cyan (#00c8ff)
- OK: green (#00e5a0)
- FIRE: orange (#ff8c42)
- MANUAL: purple (#c084fc)
- ERROR: red (#ff4060)
- PAUSE: yellow (#ffcc00)
- DELETE: red (#ff4060)

**Analyze Errors button**: Enabled only when there are ERROR-level entries in the visible log (after `logDisplayOffset`). Clicking it:
1. Checks `GET /api/api-key-status` — if no key, opens the API Key modal first
2. Filters ERROR entries from visible log
3. Sends them to `POST /api/analyze-logs`
4. Shows analysis result in the AI panel below the log

### Profile Save/Load Behavior

**What gets captured in a profile**:
- Worker name, task_path, sched_type, sched_value, output_dir
- That's it. **NOT captured**: chains, groups, requirements, env_vars, timeout_minutes, new_console, paused state, group assignments

**Save flow**:
1. User clicks "Save Current Setup" → modal asks for profile name
2. Frontend fetches `GET /api/workers` to get current worker list
3. If no workers exist, shows error toast and aborts
4. Maps workers to `{name, task_path, sched_type, sched_value, output_dir}` array
5. POSTs to `/api/profiles` with `{name, config: [...]}`
6. Same name overwrites (backend uses INSERT OR REPLACE)

**Load flow**:
1. User selects profile from dropdown or clicks "Load" on a profile item
2. Frontend fetches `GET /api/profiles/<id>`
3. Clears the worker form ("Add Workers" section)
4. For each entry in `config`, calls `addWorkerCard(prefill)` to populate form cards
5. Scrolls to the form section
6. **Does NOT register workers** — the user must click "Register Workers" manually
7. **Does NOT affect currently running workers** — existing active workers keep running

**Key implication**: Loading a profile does NOT restore a running state. It populates the creation form. The user reviews and submits. This means profiles are really "form presets" not "state snapshots."

### Import/Export vs Profiles

| Feature | Export/Import | Profiles |
|---|---|---|
| Data captured | Workers + chains + groups (full) | Workers only (partial) |
| V2 fields | Yes (requirements, env_vars, timeout, new_console, paused) | No (only name, path, schedule, output_dir) |
| Chains included | Yes | No |
| Groups included | Yes | No |
| On load | Creates entities immediately in DB + scheduler | Populates form cards for review |
| Dedup behavior | Skip by name | Overwrites by profile name |
| Format | JSON file download/upload | Stored in DB |

### Chain Builder UI

**Opening**: "New Chain" button on the workers panel header, or "Edit" on an existing chain row.

**Step management**:
- Each step is a row with: step number, file path input + Browse button, stage number input, remove button
- "Add Step" appends a new row at the bottom
- Default stage number for a new step = `chainStepCount - 1` (0-indexed), so each new step gets a unique stage by default (sequential execution)
- To make steps parallel, the user manually sets them to the same stage number
- Stage input is `type="number"`, min=0, max=99

**Stage assignment UX**:
- Steps default to sequential: step 1 = stage 0, step 2 = stage 1, step 3 = stage 2
- To make steps 2 and 3 parallel, user changes both to the same stage number (e.g., both = 1)
- The label "same stage # runs in parallel" is shown as a hint
- There is no drag-and-drop reordering — order is determined by DOM position (order_index = array index when reading)

**Edit mode**: When editing an existing chain, steps are pre-populated from the chain's current `steps` array with their existing `task_path` and `stage` values. The schedule type toggle and all fields are pre-set.

**Schedule options**: Same 3-way toggle as workers: Fixed Times, Interval, Cron. Same field rendering. Cron has a live English preview.

**Group assignment**: Dropdown at the bottom of the modal, populated from current groups.

**Stop on failure**: Checkbox, default checked. Persisted as `stop_on_failure` boolean.

**Submit**: Creates or updates the chain via `POST /api/chains` or `PUT /api/chains/<id>`. All steps are sent as array with `{task_path, stage}`.

### Frontend-Only State (Not Persisted to Backend)

| State Variable | Type | Purpose | Lifetime |
|---|---|---|---|
| `logEntries` | Array | All log entries received since page load | Lost on page reload |
| `logOffset` | int | Cursor into backend log buffer | Lost on page reload |
| `logDisplayOffset` | int | "Clear" offset — entries before this are hidden | Lost on page reload |
| `activeLevels` | Set | Which log levels are visible (all 7 active by default) | Lost on page reload |
| `collapsedGroups` | Set | Which group IDs are collapsed in the table | Lost on page reload |
| `currentWorkers` | Array | Cached worker list from last poll | Refreshed every 4s |
| `currentChains` | Array | Cached chain list from last poll | Refreshed every 4s |
| `currentGroups` | Array | Cached group list from last poll | Refreshed every 15s |
| `workerCardCount` | int | Counter for worker form cards | Reset on form clear |
| `editingWorkerId` | int\|null | Which worker is being edited in the modal | Cleared on modal close |
| `editingChainId` | int\|null | Which chain is being edited | Cleared on modal close |
| `editSchedType` | string | Current schedule type in edit worker modal | Per-modal-open |
| `chainSchedType` | string | Current schedule type in chain modal | Per-modal-open |
| `chainStepCount` | int | Counter for chain step rows | Reset per modal open |
| `selectedTemplate` | string\|null | Which template card is selected | Reset per modal open |
| `tmplSchedType` | string | Schedule type in template modal | Per-modal-open |
| `watcherRuleCount` | int | Counter for folder watcher rules | Reset per modal open |
| `historyRows` | Array | Run history for the currently-viewed entity | Per-history-modal-open |
| `historyFilter` | string | "all", "ok", or "error" filter in history modal | Per-history-modal-open |
| Update banner dismissed | CSS class | Whether user dismissed the update banner | Lost on page reload |

### Toast Notification System

- Positioned fixed bottom-right
- 3 types: `ok` (green left border), `error` (red), `info` (cyan)
- Default duration: 3.5 seconds, auto-removed from DOM
- Some error toasts use 6 seconds (`duration = 6000`)
- Slide-in animation from right
- No stacking limit — multiple toasts can pile up

### Cron Preview (Client-Side)

The `cronToEnglish()` function converts 5-field cron expressions to readable English, displayed live below the cron input. Supports:
- Every minute, every N minutes, every hour at :MM
- Daily at HH:MM, weekdays at HH:MM, weekends at HH:MM
- Specific weekday at HH:MM
- Monthly on day N, yearly on month/day
- Falls back to showing the raw expression for unrecognized patterns
- Shows "Enter all 5 fields" hint if incomplete

### History Modal

- Opened by clicking the colored dot in the Last Run column
- Fetches 100 rows at a time (`HIST_PAGE = 100`) from `/api/workers/<id>/history` or `/api/chains/<id>/history`
- "Load more" button appears if `historyRows.length < historyTotal`
- Client-side filters: All / Success / Failed toggle, date range (from/to)
- Date filtering: string comparison on `triggered_at` (YYYY-MM-DD prefix)
- Failed rows show truncated error message (first 400 chars) in a sub-row
- Duration shown as ms (< 1s) or seconds with 1 decimal (>= 1s)

### XSS Prevention

All user-visible values are passed through `escHtml()` which escapes `&`, `<`, `>`, `"`, `'`. Used consistently in all template literals that render user data (worker names, paths, error messages, etc.).

### Responsive Behavior

Single breakpoint at 640px: form grid columns collapse from 2/3-column to single-column. Profile row stacks vertically. No other responsive adjustments — the app is designed for desktop use.
