# Redis Operator

A local web dashboard for managing scheduled Python and batch workers via Redis + APScheduler. Register tasks, set schedules, and monitor everything from the browser — no terminal needed after launch.

![Redis Operator Dashboard](Readmejpegs/Screenshot%202026-04-01%20210159.png)

## Features

- **Active Workers Panel** — live table of all running workers and chains with next trigger time and remaining runs today. Pause/resume or delete individual items, or bulk pause/delete all at once.
- **Flexible Scheduling** — Fixed Times (e.g. `09:00, 14:30, 17:00` — fires daily at each listed time), Interval (e.g. `2h 30m` — repeats on a loop), or Cron expression (e.g. `0 9 * * 1-5` with a plain-English preview).
- **Task Support** — Python files (`.py` with a `run()` function), batch files (`.bat`, `.cmd`), and shell scripts (`.sh`). Interactive scripts (menus, input prompts, GUI tools) can run in a **new terminal window** via a per-worker checkbox.
- **Worker Timeout** — optional per-worker timeout (minutes). If a task runs over, it is killed and logged as an error.
- **Per-worker Environment Variables** — inject `KEY=VALUE` pairs (one per line) into a worker's subprocess environment.
- **Run Now** — fire any worker or chain immediately outside its schedule. Logged with a purple MANUAL tag in the debug panel.
- **Task History** — last 10 runs per worker/chain stored in SQLite. A colored dot on each row (green = last run OK, red = failed, grey = no runs yet) opens the history modal with timestamps, duration, trigger type, and error details. Filter by status and date range; paginate with Load More.
- **Built-in Templates** — five template types that generate and manage stdlib-only Python scripts: Folder Backup, File Cleanup, Folder Watcher, Uptime Check, Open URL. Accessible via the **⚙ From Template** button in the Add Workers panel.
- **Task Chains** — string multiple scripts into a sequential pipeline. Each step runs via subprocess with its own exit code. Stop-on-failure toggle. Chains appear in the Active Workers table alongside regular workers with a ⛓ badge.
- **Parallel Chain Steps** — assign the same stage number to multiple steps in a chain to run them concurrently. Steps in different stages execute sequentially. Default stage = step index (fully sequential).
- **Worker Groups** — collapsible named groups in the Active Workers panel. Assign workers and chains to groups from their Edit modal. Group-level Pause All and Delete All buttons. Click ▾ to collapse/expand.
- **Native File Picker** — OS-native file/directory browser via tkinter. No need to type paths manually.
- **Saved Profiles** — save and load named worker configurations. Switch between setups instantly.
- **Import / Export** — export all workers, chains, chain steps, and groups to a portable JSON file. Import on any machine; groups are created if missing, name conflicts are skipped.
- **System Tray Icon** — a tray icon appears with Open Dashboard and Stop menu items. Right-click to stop cleanly without a terminal.
- **Auto-start on Login (Windows)** — one-click install via Task Scheduler (⚙ Auto-start in the header). Redis Operator launches automatically at logon. No admin required.
- **Auto-update Check** — on startup, silently checks GitHub for a newer release. If one is available, a dismissible banner appears with the version number and a direct download link.
- **Debug Log** — live scrolling log panel with color-coded entries (INFO, OK, FIRE, MANUAL, ERROR, PAUSE, DELETE). Filter by level pill or keyword search. Auto-scroll toggle and clear display.
- **Redis Auto-Start** — detects whether Redis is already running; starts it automatically if not. The Windows installer bundles Redis — no separate install needed.
- **Persistent State** — all workers, chains, groups, and profiles stored in SQLite. Workers and chains automatically restore on restart.

### Add Workers Form

![Add Workers form](Readmejpegs/Screenshot%202026-04-01%20210231.png)

### Built-in Templates

![Template picker modal](Readmejpegs/Screenshot%202026-04-01%20210243.png)

### Task Chains

![New Task Chain modal](Readmejpegs/Screenshot%202026-04-01%20210257.png)

### Debug Log

![Debug log panel](Readmejpegs/Screenshot%202026-04-01%20210327.png)

### Windows Installer

![Windows installer — Select Additional Tasks step](Readmejpegs/Screenshot%202026-04-01%20210345.png)

## Installation (Windows — recommended)

Download and run **`Redis_Operator_Setup.exe`** from the [latest release](https://github.com/jarmstrong158/redis-operator/releases/latest).

- No Python required — everything is bundled
- Redis is bundled — no separate install needed
- Installs to `%AppData%\Redis Operator` — no admin/UAC required
- Adds a Start Menu shortcut and optional desktop shortcut
- Optional auto-start at login (checkbox during install, or via ⚙ Auto-start in the dashboard)
- Uninstaller included in Add/Remove Programs

## Setup from Source

### Requirements

- Python 3.10+
- Redis server (Windows: `winget install Redis.Redis` or https://github.com/tporadowski/redis/releases — macOS: `brew install redis` — Ubuntu: `sudo apt install redis-server`)

### Windows (dev)

1. Install [Python 3.10+](https://www.python.org/downloads/) — check **"Add Python to PATH"**
2. Install Redis (see above)
3. Clone this repo
4. Double-click **`start.bat`**

`start.bat` creates a virtual environment and installs all dependencies on first run. Subsequent launches skip straight to the dashboard.

### Manual

```bash
git clone https://github.com/jarmstrong158/redis-operator.git
cd redis-operator
pip install -r requirements.txt
python launch.py
```

The dashboard opens automatically at [http://127.0.0.1:5000](http://127.0.0.1:5000). Press `Ctrl+C` to stop.

## Building the Installer

To build `Redis_Operator_Setup.exe` yourself:

1. Install [Inno Setup](https://jrsoftware.org/isdl.php) (free)
2. Run:
   ```
   .\build.bat
   ```

`build.bat` handles everything: downloads the bundled Redis binary, generates the icon, runs PyInstaller, and compiles the installer. Output is at `Output\Redis_Operator_Setup.exe`.

To force a full PyInstaller rebuild (e.g. after changing Python code):
```
.\build.bat --rebuild
```

## Task File Format

### Python (`.py`)

Must have a module-level `run()` function with no parameters:

```python
def run():
    print("Task executed!")
```

### Batch / Shell (`.bat`, `.sh`, `.cmd`)

Executed as a subprocess. The worker's output directory (if set) is used as the working directory.

## Task Chains

Chains run multiple scripts in sequence. Each step is an isolated subprocess with full stdout/stderr capture. If **Stop on failure** is checked, the chain halts as soon as any step returns a non-zero exit code. Chains appear in the Active Workers table with a ⛓ badge showing the step count.

To create a chain: click **⛓ New Chain** in the Active Workers panel header, add steps with the file picker, set a schedule, and click **Create Chain**.

### Parallel Steps

Each step has a **stage** number. Steps with the same stage number run concurrently; stages execute in order. By default each step gets its own stage (fully sequential). To run steps in parallel, assign them the same stage number in the chain builder.

## System Tray

A tray icon appears when Redis Operator launches. Right-click it for:

- **Open Dashboard** — opens the browser
- **Stop Redis Operator** — shuts down cleanly

## Auto-start on Login (Windows)

Click **⚙ Auto-start** in the top-right header. Click **Install** to register a Windows Task Scheduler task that launches Redis Operator at every login. Click **Remove** to uninstall it. No administrator privileges required.

## Auto-update

On startup, Redis Operator silently checks the GitHub API for a newer release. If one is found, a blue banner appears at the top of the dashboard:

> 🎉 Redis Operator v3.1 is available! Download →

Click the link to go directly to the release page. Dismiss with ✕. No data is sent — it's a single unauthenticated GET request to the public GitHub API.

## Import / Export

In the **Saved Profiles** panel, use **⬇ Export** to download a JSON snapshot of all workers, chains, and groups. Use **⬆ Import** to restore from a file — groups are created if missing, and any worker or chain whose name already exists is skipped. The JSON format is portable across machines.

## Built-in Templates

Access via the **⚙ From Template** button at the bottom of the Add Workers panel.

| Template | What it does |
|---|---|
| Folder Backup | `shutil.copytree` a folder to a destination; prune to keep N copies |
| File Cleanup | `glob` + age check; deletes files matching a pattern older than X days |
| Folder Watcher | Moves files by extension to configured destination folders |
| Uptime Check | `urllib.request` pings a URL; logs status to file; raises error if DOWN |
| Open URL | `webbrowser.open` a URL on schedule |

All templates use Python stdlib only — no extra packages required.

## Architecture

```
redis_operator/
├── launch.py                    # Entry point — starts Flask, opens browser, system tray
├── app.py                       # Flask backend + APScheduler + SQLite + Redis
├── static/
│   └── index.html               # Entire frontend — HTML + CSS + JS (no frameworks)
├── tasks/
│   └── example_task.py          # Sample task with run() function
├── templates/
│   └── generated/               # Auto-generated template scripts (gitignored)
├── build.bat                    # One-click installer build script
├── build_icon.py                # Generates redis_operator.ico
├── download_redis.py            # Downloads bundled redis-server.exe
├── redis_operator.spec          # PyInstaller spec
├── installer.iss                # Inno Setup installer script
├── run_inno.py                  # Finds and runs ISCC.exe (cross-drive)
├── requirements.txt             # Python dependencies
└── redis_operator.db            # Auto-created SQLite database (gitignored)
```

| Layer | Detail |
|---|---|
| Backend | Python 3, Flask |
| Scheduling | APScheduler — BackgroundScheduler with MemoryJobStore |
| Persistence | SQLite (workers, chains, chain_steps, groups, profiles, run_history) |
| Worker State | Redis (port 6379, bundled on Windows, auto-started) |
| Frontend | Vanilla JS, no build step, single HTML file |

## AI Error Analysis

The debug log panel includes an **Analyze Errors** button that becomes active when ERROR-level entries are present. Clicking it sends those errors to Claude and displays a plain-English diagnosis and recommended fix directly in the dashboard.

Requires an Anthropic API key. Click "Analyze Errors" to be prompted, or create a `.env` file manually:
```
ANTHROPIC_API_KEY=sk-ant-...
```

The `.env` file is gitignored and your key is never transmitted anywhere except directly to the Anthropic API.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/workers` | List all workers |
| `POST` | `/api/workers` | Register worker(s) |
| `PUT` | `/api/workers/<id>` | Update worker |
| `POST` | `/api/workers/<id>/pause` | Toggle pause/resume |
| `POST` | `/api/workers/<id>/run-now` | Fire immediately |
| `GET` | `/api/workers/<id>/history` | Last 10 runs |
| `POST` | `/api/workers/<id>/assign-group` | Assign to group |
| `DELETE` | `/api/workers/<id>` | Delete worker |
| `POST` | `/api/workers/pause-all` | Pause all workers |
| `DELETE` | `/api/workers/all` | Delete all workers |
| `GET` | `/api/chains` | List all chains |
| `POST` | `/api/chains` | Create chain |
| `PUT` | `/api/chains/<id>` | Update chain |
| `POST` | `/api/chains/<id>/pause` | Toggle pause/resume |
| `POST` | `/api/chains/<id>/run-now` | Fire immediately |
| `GET` | `/api/chains/<id>/history` | Last 10 runs |
| `POST` | `/api/chains/<id>/assign-group` | Assign to group |
| `DELETE` | `/api/chains/<id>` | Delete chain |
| `GET` | `/api/groups` | List all groups |
| `POST` | `/api/groups` | Create group |
| `PUT` | `/api/groups/<id>` | Rename group |
| `DELETE` | `/api/groups/<id>` | Delete group (unassigns members) |
| `GET` | `/api/profiles` | List saved profiles |
| `POST` | `/api/profiles` | Save profile |
| `GET` | `/api/profiles/<id>` | Load profile |
| `DELETE` | `/api/profiles/<id>` | Delete profile |
| `POST` | `/api/templates` | Create worker from template |
| `GET` | `/api/export` | Export all data as JSON |
| `POST` | `/api/import` | Import from JSON |
| `GET` | `/api/service/status` | Check auto-start task status |
| `POST` | `/api/service/install` | Install auto-start task |
| `POST` | `/api/service/uninstall` | Remove auto-start task |
| `GET` | `/api/update-check` | Check for newer GitHub release |
| `GET` | `/api/logs?since=N` | Log entries since offset N |
| `GET` | `/api/redis-status` | Redis connection status |
| `GET` | `/api/browse?mode=file\|dir` | Native OS file picker |

## License

MIT
