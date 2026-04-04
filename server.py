#!/usr/bin/env python3
"""
Redis Operator MCP Server
Exposes Redis Operator's REST API as MCP tools for Claude.
Run alongside Redis Operator: python server.py
"""

import json
import sys
import urllib.request
import urllib.error
from typing import Any

BASE_URL = "http://127.0.0.1:5000"

def api(method: str, path: str, body: dict = None) -> Any:
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode(), "status": e.code}
    except Exception as e:
        return {"error": str(e)}

TOOLS = [
    {
        "name": "list_workers",
        "description": "List all workers and chains in Redis Operator. Returns id, name, task_path, schedule, paused state, next trigger time, remaining runs today, last run status, and group assignment for each.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "create_worker",
        "description": "Register a new scheduled worker. sched_type: 'fixed' (comma-separated times like '09:00,14:30'), 'interval' (like '2h 30m' or '1h'), or 'cron' (like '0 9 * * 1-5'). timeout_minutes=0 means no timeout. env_vars is KEY=VALUE lines.",
        "inputSchema": {
            "type": "object",
            "required": ["name", "task_path", "sched_type", "sched_value"],
            "properties": {
                "name": {"type": "string"},
                "task_path": {"type": "string", "description": "Absolute path to .py, .bat, .sh, or .cmd file"},
                "sched_type": {"type": "string", "enum": ["fixed", "interval", "cron"]},
                "sched_value": {"type": "string"},
                "output_dir": {"type": "string", "default": ""},
                "requirements": {"type": "string", "description": "Comma-separated pip packages to install", "default": ""},
                "timeout_minutes": {"type": "integer", "default": 0},
                "env_vars": {"type": "string", "description": "KEY=VALUE lines injected into subprocess env", "default": ""},
                "new_console": {"type": "boolean", "default": False}
            }
        }
    },
    {
        "name": "update_worker",
        "description": "Update an existing worker by ID. All fields optional except name and task_path which are required.",
        "inputSchema": {
            "type": "object",
            "required": ["worker_id", "name", "task_path"],
            "properties": {
                "worker_id": {"type": "integer"},
                "name": {"type": "string"},
                "task_path": {"type": "string"},
                "sched_type": {"type": "string", "enum": ["fixed", "interval", "cron"]},
                "sched_value": {"type": "string"},
                "output_dir": {"type": "string"},
                "requirements": {"type": "string"},
                "timeout_minutes": {"type": "integer"},
                "env_vars": {"type": "string"},
                "new_console": {"type": "boolean"},
                "group_id": {"type": "integer"}
            }
        }
    },
    {
        "name": "delete_worker",
        "description": "Delete a worker by ID. Removes from scheduler and database.",
        "inputSchema": {
            "type": "object",
            "required": ["worker_id"],
            "properties": {"worker_id": {"type": "integer"}}
        }
    },
    {
        "name": "pause_worker",
        "description": "Toggle pause/resume for a worker by ID. If paused, resumes it. If running, pauses it.",
        "inputSchema": {
            "type": "object",
            "required": ["worker_id"],
            "properties": {"worker_id": {"type": "integer"}}
        }
    },
    {
        "name": "run_worker_now",
        "description": "Fire a worker immediately outside its schedule. Logged as MANUAL trigger type.",
        "inputSchema": {
            "type": "object",
            "required": ["worker_id"],
            "properties": {"worker_id": {"type": "integer"}}
        }
    },
    {
        "name": "get_worker_history",
        "description": "Get the last 10 run history entries for a worker. Returns triggered_at, trigger_type, success, duration_ms, error_msg.",
        "inputSchema": {
            "type": "object",
            "required": ["worker_id"],
            "properties": {"worker_id": {"type": "integer"}}
        }
    },
    {
        "name": "pause_all_workers",
        "description": "Pause all workers at once.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "delete_all_workers",
        "description": "Delete ALL workers and chains. Irreversible. Use with caution.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "list_chains",
        "description": "List all task chains. Returns id, name, schedule, steps with stage numbers, stop_on_failure, paused state, next trigger, last run status.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "create_chain",
        "description": "Create a new task chain. Steps run sequentially by default. Assign same stage number to multiple steps to run them in parallel. Stage numbers are integers 0-99.",
        "inputSchema": {
            "type": "object",
            "required": ["name", "sched_type", "sched_value", "steps"],
            "properties": {
                "name": {"type": "string"},
                "sched_type": {"type": "string", "enum": ["fixed", "interval", "cron"]},
                "sched_value": {"type": "string"},
                "stop_on_failure": {"type": "boolean", "default": True},
                "steps": {
                    "type": "array",
                    "description": "List of steps. Each step needs task_path and stage. Same stage = parallel execution.",
                    "items": {
                        "type": "object",
                        "required": ["task_path"],
                        "properties": {
                            "task_path": {"type": "string"},
                            "stage": {"type": "integer", "default": 0}
                        }
                    }
                }
            }
        }
    },
    {
        "name": "run_chain_now",
        "description": "Fire a chain immediately outside its schedule. Logged as MANUAL trigger.",
        "inputSchema": {
            "type": "object",
            "required": ["chain_id"],
            "properties": {"chain_id": {"type": "integer"}}
        }
    },
    {
        "name": "delete_chain",
        "description": "Delete a chain and all its steps by ID.",
        "inputSchema": {
            "type": "object",
            "required": ["chain_id"],
            "properties": {"chain_id": {"type": "integer"}}
        }
    },
    {
        "name": "get_chain_history",
        "description": "Get the last 10 run history entries for a chain.",
        "inputSchema": {
            "type": "object",
            "required": ["chain_id"],
            "properties": {"chain_id": {"type": "integer"}}
        }
    },
    {
        "name": "list_groups",
        "description": "List all worker groups. Groups are collapsible collections of workers and chains in the dashboard.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "create_group",
        "description": "Create a new worker group.",
        "inputSchema": {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}}
        }
    },
    {
        "name": "delete_group",
        "description": "Delete a group by ID. Unassigns all members but does not delete them.",
        "inputSchema": {
            "type": "object",
            "required": ["group_id"],
            "properties": {"group_id": {"type": "integer"}}
        }
    },
    {
        "name": "create_worker_from_template",
        "description": "Create a worker using a built-in template. Template types: folder_backup (source, dest, keep), file_cleanup (folder, pattern, days), folder_watcher (watch, rules as [{ext, dest}]), uptime_check (url, log_file), open_url (url). Generated scripts use stdlib only.",
        "inputSchema": {
            "type": "object",
            "required": ["template_type", "name", "sched_type", "sched_value", "config"],
            "properties": {
                "template_type": {"type": "string", "enum": ["folder_backup", "file_cleanup", "folder_watcher", "uptime_check", "open_url"]},
                "name": {"type": "string"},
                "sched_type": {"type": "string", "enum": ["fixed", "interval", "cron"]},
                "sched_value": {"type": "string"},
                "config": {"type": "object", "description": "Template-specific config fields"}
            }
        }
    },
    {
        "name": "export_all",
        "description": "Export all workers, chains, chain steps, and groups as a JSON snapshot. Use for backup or migration.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_logs",
        "description": "Get debug log entries. Returns entries since a given offset (use 0 for all). Log levels: INFO, OK, FIRE, MANUAL, ERROR, PAUSE, DELETE. Max 500 entries in buffer (oldest drop off).",
        "inputSchema": {
            "type": "object",
            "properties": {"since": {"type": "integer", "default": 0, "description": "Return entries after this offset index"}}
        }
    },
    {
        "name": "get_redis_status",
        "description": "Check if Redis is running and connected on port 6379.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "check_for_update",
        "description": "Check GitHub for a newer version of Redis Operator.",
        "inputSchema": {"type": "object", "properties": {}}
    }
]

def handle_tool(name: str, args: dict) -> str:
    if name == "list_workers":
        return json.dumps(api("GET", "/api/workers"), indent=2)
    elif name == "create_worker":
        return json.dumps(api("POST", "/api/workers", args), indent=2)
    elif name == "update_worker":
        wid = args.pop("worker_id")
        return json.dumps(api("PUT", f"/api/workers/{wid}", args), indent=2)
    elif name == "delete_worker":
        return json.dumps(api("DELETE", f"/api/workers/{args['worker_id']}"), indent=2)
    elif name == "pause_worker":
        return json.dumps(api("POST", f"/api/workers/{args['worker_id']}/pause"), indent=2)
    elif name == "run_worker_now":
        return json.dumps(api("POST", f"/api/workers/{args['worker_id']}/run-now"), indent=2)
    elif name == "get_worker_history":
        return json.dumps(api("GET", f"/api/workers/{args['worker_id']}/history"), indent=2)
    elif name == "pause_all_workers":
        return json.dumps(api("POST", "/api/workers/pause-all"), indent=2)
    elif name == "delete_all_workers":
        return json.dumps(api("DELETE", "/api/workers/all"), indent=2)
    elif name == "list_chains":
        return json.dumps(api("GET", "/api/chains"), indent=2)
    elif name == "create_chain":
        return json.dumps(api("POST", "/api/chains", args), indent=2)
    elif name == "run_chain_now":
        return json.dumps(api("POST", f"/api/chains/{args['chain_id']}/run-now"), indent=2)
    elif name == "delete_chain":
        return json.dumps(api("DELETE", f"/api/chains/{args['chain_id']}"), indent=2)
    elif name == "get_chain_history":
        return json.dumps(api("GET", f"/api/chains/{args['chain_id']}/history"), indent=2)
    elif name == "list_groups":
        return json.dumps(api("GET", "/api/groups"), indent=2)
    elif name == "create_group":
        return json.dumps(api("POST", "/api/groups", args), indent=2)
    elif name == "delete_group":
        return json.dumps(api("DELETE", f"/api/groups/{args['group_id']}"), indent=2)
    elif name == "create_worker_from_template":
        return json.dumps(api("POST", "/api/templates", args), indent=2)
    elif name == "export_all":
        return json.dumps(api("GET", "/api/export"), indent=2)
    elif name == "get_logs":
        since = args.get("since", 0)
        return json.dumps(api("GET", f"/api/logs?since={since}"), indent=2)
    elif name == "get_redis_status":
        return json.dumps(api("GET", "/api/redis-status"), indent=2)
    elif name == "check_for_update":
        return json.dumps(api("GET", "/api/update-check"), indent=2)
    else:
        return json.dumps({"error": f"Unknown tool: {name}"})

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_id = msg.get("id")
        method = msg.get("method", "")

        if method == "initialize":
            response = {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "redis-operator-mcp", "version": "1.0.0"}
                }
            }
        elif method == "tools/list":
            response = {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
        elif method == "tools/call":
            tool_name = msg["params"]["name"]
            tool_args = msg["params"].get("arguments", {})
            result = handle_tool(tool_name, tool_args)
            response = {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {"content": [{"type": "text", "text": result}]}
            }
        elif method == "notifications/initialized":
            continue
        else:
            response = {
                "jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"}
            }

        print(json.dumps(response), flush=True)

if __name__ == "__main__":
    main()
