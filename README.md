# Redis Operator

A local web dashboard for managing scheduled Python and batch workers via Redis + APScheduler. Register tasks, set schedules, and monitor everything from the browser — no terminal needed after launch.

![Redis Operator Dashboard](screenshot.png)

## Features

- **Active Workers Panel** — live table of all running workers and chains with next trigger time and remaining runs today. Pause/resume or delete individual items, or bulk pause/delete all at once.
- **Flexible Scheduling** — Fixed Times (e.g. `09:00, 14:30, 17:00` — fires daily at each listed time) or Interval (e.g. `2h 30m` — repeats on a loop).
- **Task Support** — Python files (`.py` with a `run()` function), batch files (`.bat`, `.cmd`), and shell scripts (`.sh`).
- **Run Now** — fire any worker or chain immediately outside its schedule. Logged with a purple MANUAL tag in the debug panel.
- **Task History** — last 10 runs per worker/chain stored in SQLite. A colored dot on each row (green = last run OK, red = failed, grey = no runs yet) opens the history modal with timestamps, duration, trigger type, and error details.
- **Built-in Templates** — five template types that generate and manage stdlib-only Python scripts: Folder Backup, File Cleanup, Folder Watcher, Uptime Check, Open URL.
- **Task Chains** — string multiple scripts into a sequential pipeline. Each step runs via subprocess with its own exit code. Stop-on-failure toggle. Chains appear in the Active Workers table alongside regular workers with a ⛓ badge.
- **Worker Groups** — collapsible named groups in the Active Workers panel. Assign workers and chains to groups from their Edit modal. Group-level Pause All and Delete All buttons. Click ▾ to collapse/expand.
- **Native File Picker** — OS-native file/directory browser via tkinter. No need to type paths manually.
- **Saved Profiles** — save and load named worker configurations. Switch between setups instantly.
- **Debug Log** — live scrolling log panel with color-coded entries (INFO, OK, FIRE, MANUAL, ERROR, PAUSE, DELETE). Auto-scroll toggle and clear display.
- **Redis Auto-Start** — detects whether Redis is already running; starts it automatically if not.
- **Persistent State** — all workers, chains, groups, and profiles stored in SQLite. Workers and chains automatically restore on restart via APScheduler's SQLAlchemy job store.

## Requirements

- Python 3.10+
- Redis server installed and on PATH

### Installing Redis

**Windows:**
```
winget install Redis.Redis
```
Or download from: https://github.com/tporadowski/redis/releases

**macOS:**
```
brew install redis
```

**Ubuntu/Debian:**
```
sudo apt install redis-server
```

## Setup & Usage

### Windows (easiest)

1. Install [Python 3.10+](https://www.python.org/downloads/) — check **"Add Python to PATH"** during install
2. Install [Redis](https://github.com/tporadowski/redis/releases) (or `winget install Redis.Redis`)
3. Clone or download this repo
4. Double-click **`start.bat`**

On first run `start.bat` creates a virtual environment and installs all dependencies automatically. Subsequent launches skip straight to the dashboard.

### Manual

```bash
git clone https://github.com/jarmstrong158/redis-operator.git
cd redis-operator
pip install -r requirements.txt
python launch.py
```

The dashboard opens automatically in your default browser at [http://127.0.0.1:5000](http://127.0.0.1:5000).
Press `Ctrl+C` to stop everything cleanly.

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

Chains run multiple scripts in sequence. Each step is executed as an isolated subprocess (full stdout/stderr capture). If **Stop on failure** is checked, the chain halts as soon as any step returns a non-zero exit code. Chains appear in the Active Workers table with a ⛓ chain badge showing the step count.

To create a chain: click **⛓ New Chain** in the Active Workers panel header, add steps with the file picker, set a schedule, and click **Create Chain**.

## Built-in Templates

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
├── launch.py                    # Entry point — starts Flask, opens browser
├── app.py                       # Flask backend + APScheduler + SQLite + Redis
├── static/
│   └── index.html               # Entire frontend — HTML + CSS + JS (no frameworks)
├── tasks/
│   └── example_task.py          # Sample task with run() function
├── templates/
│   └── generated/               # Auto-generated template scripts (gitignored)
├── requirements.txt             # Python dependencies
└── redis_operator.db            # Auto-created SQLite database (gitignored)
```

| Layer | Detail |
|---|---|
| Backend | Python 3, Flask |
| Scheduling | APScheduler — BackgroundScheduler with SQLAlchemy job store |
| Persistence | SQLite (workers, chains, chain_steps, groups, profiles, run_history) |
| Worker State | Redis (port 6379, auto-started if needed) |
| Frontend | Vanilla JS, no build step, single HTML file |

## AI Error Analysis

The debug log panel includes an **Analyze Errors** button that becomes active when ERROR-level entries are present. Clicking it sends those errors to Claude and displays a plain-English diagnosis and recommended fix directly in the dashboard.

### Setup

The feature requires an Anthropic API key. You have two options:

**Option A — prompted in the UI:**
Click "Analyze Errors" when no key is configured. A modal will ask for your key and save it to a local `.env` file automatically. The key is loaded on every subsequent launch.

**Option B — set it manually:**
Create a `.env` file in the project directory:
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
| `GET` | `/api/logs?since=N` | Log entries since offset N |
| `GET` | `/api/redis-status` | Redis connection status |
| `GET` | `/api/browse?mode=file\|dir` | Native OS file picker |

## License

MIT
