[![Tests](https://github.com/jarmstrong158/Conductor/actions/workflows/tests.yml/badge.svg)](https://github.com/jarmstrong158/Conductor/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

# Conductor

**Tell Claude what to automate. It handles the rest.**

Conductor is a local task orchestration platform that Claude controls directly. Install it, restart Claude Desktop, and Claude can create scheduled workers, build multi-step pipelines, send email reports, and monitor everything — all through conversation. No coding required.

![Conductor Dashboard](Readmejpegs/Screenshot%202026-04-01%20210159.png)

## Claude Integration

Conductor registers itself as an MCP server in Claude Desktop on first launch. After restarting Claude Desktop once, Claude has 21 tools for full control:

**Example conversation:**

> **You:** Run my metrics script every night at 8:30 PM and email me the report
>
> **Claude:** I'll set that up using the Run + Email template.
> *(creates the worker, configures the schedule, wires up Gmail, fires it to test)*
> Done — worker "Daily Metrics Report" is live. Check your email for the test run.

> **You:** That uptime check keeps failing. What's going on?
>
> **Claude:** *(pulls the last 10 runs, reads the error logs)*
> The site returned a 503 three times in the last hour. The SSL certificate expired yesterday. Here's the full error from the last run...

> **You:** Chain my backup script and cleanup script together. Run backup first, then cleanup after it finishes. Every day at 2 AM.
>
> **Claude:** *(creates a chain with stage 0 = backup, stage 1 = cleanup, cron schedule "0 2 * * *")*
> Chain "Nightly Maintenance" created with 2 steps. Backup runs first, cleanup runs after it succeeds.

**How it works:**
1. Install Conductor (Windows installer or from source)
2. Conductor writes its MCP entry to Claude Desktop's config on first launch
3. Restart Claude Desktop once
4. Claude now controls Conductor through natural language — create workers, build chains, fire tasks, check logs, send emails

## Features

- **Claude-Controlled** — 21 MCP tools let Claude create workers, chains, groups, templates, and more. Non-technical users can automate anything through conversation.
- **Flexible Scheduling** — Fixed times (`09:00, 14:30`), intervals (`2h 30m`), or cron expressions (`0 9 * * 1-5` with plain-English preview).
- **Task Support** — Python (`.py`), batch (`.bat`, `.cmd`), and shell (`.sh`) scripts. Selenium and GUI scripts run in a visible terminal window via `new_console`.
- **Built-in Email** — configure Gmail once, then any template or worker can send emails. Run + Email template for automated reports. Folder Watcher emails files on arrival. Uptime Check sends DOWN alerts. Worker notifications on success/failure.
- **Task Chains** — multi-step pipelines with parallel execution. Same stage number = run in parallel. Stop-on-failure toggle.
- **Worker Groups** — collapsible named groups for organization. Bulk pause/delete per group.
- **Run History** — per-worker/chain history with status, duration, and error details. Colored dots show last run status at a glance.
- **Built-in Templates** — Folder Backup, File Cleanup, Folder Watcher, Uptime Check, Open URL, Run + Email. Generated scripts are stdlib-only and self-contained.
- **Per-worker Environment Variables** — inject `KEY=VALUE` pairs into subprocess environments.
- **Worker Timeout** — kill and log workers that run over a set number of minutes.
- **Email Notifications** — per-worker/chain email alerts: Always, On Failure, or On Success.
- **Import / Export** — portable JSON snapshot of all workers, chains, and groups.
- **Saved Profiles** — save and load named worker configurations.
- **Auto-start on Login** — one-click Registry Run key install. No admin required.
- **Auto-update Check** — modal dialog when a newer release is available.
- **System Tray Icon** — right-click for Open Dashboard or Stop.
- **AI Error Analysis** — send error logs to Claude for diagnosis directly in the dashboard.
- **Redis Auto-Start** — bundled Redis, starts automatically. No separate install needed.
- **Persistent State** — SQLite database. Workers and chains restore on restart.

### Add Workers Form

![Add Workers form](Readmejpegs/Screenshot%202026-04-01%20210231.png)

### Built-in Templates

![Template picker modal](Readmejpegs/Screenshot%202026-04-01%20210243.png)

### Task Chains

![New Task Chain modal](Readmejpegs/Screenshot%202026-04-01%20210257.png)

### Debug Log

![Debug log panel](Readmejpegs/Screenshot%202026-04-01%20210327.png)

## Installation (Windows)

Download and run **`Conductor_Setup.exe`** from the [latest release](https://github.com/jarmstrong158/conductor/releases/latest).

- No Python required — everything is bundled
- Redis is bundled — no separate install needed
- Installs to `%AppData%\Conductor` — no admin/UAC required
- Adds Start Menu shortcut and optional desktop shortcut
- Optional auto-start at login
- Uninstaller included

## Setup from Source

### Requirements

- Python 3.10+
- Redis server (Windows: `winget install Redis.Redis` — macOS: `brew install redis` — Ubuntu: `sudo apt install redis-server`)

```bash
git clone https://github.com/jarmstrong158/conductor.git
cd conductor
pip install -r requirements.txt
python launch.py
```

The dashboard opens at [http://127.0.0.1:5000](http://127.0.0.1:5000). Press `Ctrl+C` to stop.

## Building the Installer

```
.\build.bat
```

Forces a full rebuild:
```
.\build.bat --rebuild
```

Output: `Output\Conductor_Setup.exe`. Requires [Inno Setup](https://jrsoftware.org/isdl.php).

## Task File Format

### Python (`.py`)

Scripts run as subprocesses:

```python
if __name__ == "__main__":
    print("Task executed!")
```

### Batch / Shell (`.bat`, `.sh`, `.cmd`)

Executed as subprocesses. The worker's output directory (if set) is used as the working directory.

## Schedule Formats

| Type | Format | Example |
|------|--------|---------|
| Fixed | `HH:MM` or `HH:MM,HH:MM` | `09:00, 14:30, 17:00` |
| Interval | `Xh Ym` | `2h 30m`, `1h`, `45m` |
| Cron | 5-field crontab | `0 9 * * 1-5` (weekdays at 9am) |

## Task Chains

Chains run multiple scripts in sequence. Each step has a **stage** number — steps with the same stage run in parallel, stages execute in order. **Stop on failure** halts the chain when any step fails.

```
Stage 0: backup.py  ──┐
Stage 0: cleanup.py ──┤  (both run at once)
                      ↓
Stage 1: report.py    (runs after both finish)
```

## Templates

| Template | What it does | Email capability |
|----------|-------------|-----------------|
| Folder Backup | Copy folder, keep N backups | Summary email |
| File Cleanup | Delete old files by pattern | Summary email |
| Folder Watcher | Move/email files by extension | Per-rule: move, email, or both |
| Uptime Check | Monitor URL, log status | Alert email on DOWN |
| Open URL | Open URL in browser | — |
| Run + Email | Run script, email output file | Sends output as attachment |

All generated scripts use Python stdlib only.

## Architecture

```
conductor/
├── launch.py          # Entry point — Flask, browser, tray, MCP registration
├── app.py             # Flask backend + APScheduler + SQLite + Redis
├── server.py          # MCP server for Claude Desktop
├── CLAUDE.md          # Context file for Claude Code / Desktop
├── static/
│   └── index.html     # Frontend — HTML + CSS + JS (no frameworks)
├── tasks/
│   └── example_task.py
├── templates/
│   └── generated/     # Auto-generated template scripts
├── build.bat          # One-click installer build
├── build_icon.py      # Generates conductor.ico
├── conductor.spec     # PyInstaller spec
├── installer.iss      # Inno Setup script
└── conductor.db       # SQLite database (auto-created)
```

| Layer | Technology |
|-------|-----------|
| Backend | Python 3, Flask |
| Scheduling | APScheduler (BackgroundScheduler + MemoryJobStore) |
| Persistence | SQLite |
| Worker State | Redis (port 6379, bundled on Windows) |
| Frontend | Vanilla JS, single HTML file |
| AI Integration | Claude via MCP (21 tools) + Anthropic API (error analysis) |

## AI Error Analysis

The debug log includes an **Analyze Errors** button. When ERROR entries are present, click it to send them to Claude for diagnosis. Requires an Anthropic API key in `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
```

## Email Setup

Click **📧 Email** in the header to configure Gmail credentials. Requires a [Gmail App Password](https://myaccount.google.com/apppasswords) (2-Step Verification must be enabled first). Credentials are stored locally in `.env` and never transmitted except to Gmail's SMTP server.

Once configured, all templates and worker notifications can send email.

## API

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/workers` | List all workers |
| `POST` | `/api/workers` | Register worker(s) |
| `PUT` | `/api/workers/<id>` | Update worker |
| `POST` | `/api/workers/<id>/pause` | Toggle pause/resume |
| `POST` | `/api/workers/<id>/run-now` | Fire immediately |
| `GET` | `/api/workers/<id>/history` | Run history |
| `POST` | `/api/workers/<id>/assign-group` | Assign to group |
| `DELETE` | `/api/workers/<id>` | Delete worker |
| `POST` | `/api/workers/pause-all` | Pause all workers |
| `DELETE` | `/api/workers/all` | Delete all workers |
| `GET` | `/api/chains` | List all chains |
| `POST` | `/api/chains` | Create chain |
| `PUT` | `/api/chains/<id>` | Update chain |
| `POST` | `/api/chains/<id>/pause` | Toggle pause/resume |
| `POST` | `/api/chains/<id>/run-now` | Fire immediately |
| `GET` | `/api/chains/<id>/history` | Run history |
| `DELETE` | `/api/chains/<id>` | Delete chain |
| `GET` | `/api/groups` | List groups |
| `POST` | `/api/groups` | Create group |
| `DELETE` | `/api/groups/<id>` | Delete group |
| `GET` | `/api/profiles` | List profiles |
| `POST` | `/api/profiles` | Save profile |
| `GET` | `/api/profiles/<id>` | Load profile |
| `DELETE` | `/api/profiles/<id>` | Delete profile |
| `POST` | `/api/templates` | Create from template |
| `GET` | `/api/export` | Export all as JSON |
| `POST` | `/api/import` | Import from JSON |
| `GET/POST` | `/api/email-settings` | Email configuration |
| `GET` | `/api/service/status` | Auto-start status |
| `POST` | `/api/service/install` | Install auto-start |
| `POST` | `/api/service/uninstall` | Remove auto-start |
| `GET` | `/api/update-check` | Check for updates |
| `GET` | `/api/logs?since=N` | Log entries |
| `GET` | `/api/redis-status` | Redis status |
| `GET` | `/api/browse?mode=file\|dir` | Native file picker |

## License

MIT
