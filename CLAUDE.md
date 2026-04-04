# Redis Operator — Claude Context

Redis Operator is a local web dashboard for managing scheduled Python and batch workers via Redis + APScheduler. It runs at http://127.0.0.1:5000 and exposes a REST API that this MCP server wraps.

## What it does

Users register scripts (.py, .bat, .sh, .cmd) and give them a schedule. Redis Operator fires them on time, logs the results, and shows everything in a browser dashboard. No terminal needed after launch.

Scripts can be chained together into sequential or parallel pipelines. Workers can be grouped for organization. Everything persists in SQLite.

## Core concepts

### Workers
A worker is one script on one schedule. Key fields:
- `task_path` — absolute path to the script file
- `sched_type` — "fixed", "interval", or "cron"
- `sched_value` — "09:00,14:30" / "2h 30m" / "0 9 * * 1-5"
- `timeout_minutes` — kills the process after N minutes (0 = no timeout)
- `env_vars` — KEY=VALUE lines injected into subprocess environment
- `requirements` — comma-separated pip packages auto-installed before first run
- `new_console` — opens a new terminal window (Windows only, suppresses stdout capture)
- `output_dir` — working directory for the subprocess
- `group_id` — optional group assignment

Python scripts must have a top-level `run()` function with no parameters. Batch/shell scripts are executed as subprocesses directly.

### Chains
A chain strings multiple scripts into a pipeline. Key fields:
- `stop_on_failure` — if true, chain halts when any step fails (checked between stages, not mid-stage)
- `steps` — list of {task_path, stage}
- `stage` — integer 0-99. Steps with the same stage number run in parallel via ThreadPoolExecutor. Stages run sequentially. Default: each step gets its own unique stage (fully sequential).

**Important:** Chain steps do NOT inherit worker features. No timeout, no env_vars, no requirements, no new_console at the step level. These are bare subprocess calls.

### Schedules
- **fixed**: fires daily at specific times. Value: "09:00" or "09:00,14:30,17:00"
- **interval**: repeats on a loop. Value: "2h", "30m", "1h 30m". Parsing is strict — "1h30" without "m" gives 0 minutes.
- **cron**: full cron expression. Value: "0 9 * * 1-5". Uses APScheduler's CronTrigger.

Job IDs follow the pattern `w{id}_t0`, `w{id}_t1` (fixed), `w{id}_i0` (interval), `w{id}_c0` (cron), `c{id}_*` (chains).

### Groups
Named collections for organizing workers and chains in the dashboard. Deleting a group unassigns members but does not delete them.

### Profiles
Profiles only capture: name, task_path, sched_type, sched_value, output_dir. They do NOT capture env_vars, requirements, timeout, new_console, or group assignment. Loading a profile populates the add-worker form — it does not register workers or affect running state.

### Templates
Built-in script generators that create stdlib-only Python scripts:
- `folder_backup` — config: source, dest, keep (int, number of backups to retain)
- `file_cleanup` — config: folder, pattern (glob like "*.tmp"), days (int)
- `folder_watcher` — config: watch (path), rules (array of {ext, dest})
- `uptime_check` — config: url, log_file
- `open_url` — config: url

**Known issue:** Generated scripts lack a `if __name__ == "__main__"` guard, so they won't execute if run directly from terminal. They work fine when Redis Operator calls `run()` directly.

### Run history
Each run writes a row to run_history: triggered_at, trigger_type (scheduled/manual), success (bool), duration_ms, error_msg. Last 10 runs available per worker/chain via API.

### Logs
In-memory buffer, max 500 entries (oldest drop off). Levels: INFO, OK, FIRE, MANUAL, ERROR, PAUSE, DELETE. Clear in the dashboard only hides entries visually — does not delete from backend.

## Known edge cases

1. No concurrent run protection — if a worker's schedule fires while the previous run is still going, both run simultaneously
2. Import deduplication is by name only — importing a worker with the same name as an existing one is skipped silently
3. Interval parsing fragility — "1h30" parses as 1h 0m. Always use "1h 30m"
4. new_console suppresses stdout — output won't appear in the debug log
5. Log buffer wraps at 500 — old entries are gone permanently
6. Pause All only affects workers, not chains
7. Profile save only captures workers, not chains or groups
8. Group pause/delete is client-side fan-out — not a single atomic backend operation
9. Chain stop_on_failure checks between stages only — parallel steps within a stage all complete before failure is evaluated

## When helping a user set up Redis Operator

Walk through in this order:
1. Confirm Redis Operator is running (check redis status, then list workers)
2. Ask what they want to automate — script path, what it does, when it should run
3. Confirm the script has a `run()` function if it's a .py file
4. Choose the right schedule type based on their description
5. Create the worker, then run it immediately to verify it works
6. Check the logs to confirm success or diagnose failure
7. If chaining scripts, ask about dependencies between them to determine if parallel stages make sense

## API base URL
http://127.0.0.1:5000

All endpoints return JSON. POST/PUT bodies are JSON. Worker and chain IDs are integers assigned by SQLite autoincrement.
