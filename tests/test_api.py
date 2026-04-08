"""
test_api.py — end-to-end API tests for Conductor.

Coverage areas (in priority order):
  1. Worker CRUD
  2. Pause / Resume
  3. Run Now  (manual trigger → run_history row)
  4. Chain CRUD + cascade delete
  5. Chain parallel stages
  6. Import / Export
  7. Templates (all 5 types)
  8. Run history structure + pagination
  9. Log endpoint (offset filtering)
"""

import json
import sqlite3
import threading
import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

import app as _app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_task_file(tmp_path, name: str = "task.py",
                   content: str = "def run():\n    pass\n") -> str:
    """Write a minimal .py task file and return its absolute path string."""
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


def db_query(sql: str, params: tuple = ()):
    """Run a raw SELECT against the current test DB and return all rows."""
    conn = sqlite3.connect(str(_app.DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


# ===========================================================================
# 1. Worker CRUD
# ===========================================================================

class TestWorkerCRUD:
    def test_register_single_worker(self, client, tmp_path):
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "My Worker",
            "task_path": task,
            "sched_type": "interval",
            "sched_value": "1h",
        })
        assert r.status_code == 201
        body = r.get_json()
        assert len(body["added"]) == 1
        assert body["errors"] == []

    def test_registered_worker_appears_in_list(self, client, tmp_path):
        task = make_task_file(tmp_path)
        client.post("/api/workers", json={
            "name": "Listed Worker",
            "task_path": task,
            "sched_type": "interval",
            "sched_value": "30m",
        })
        workers = client.get("/api/workers").get_json()
        assert any(w["name"] == "Listed Worker" for w in workers)

    def test_worker_fields_stored_correctly(self, client, tmp_path):
        task = make_task_file(tmp_path)
        client.post("/api/workers", json={
            "name": "FieldCheck",
            "task_path": task,
            "sched_type": "interval",
            "sched_value": "2h",
            "timeout_minutes": 5,
            "env_vars": "KEY=val",
        })
        workers = client.get("/api/workers").get_json()
        w = next(w for w in workers if w["name"] == "FieldCheck")
        assert w["sched_type"] == "interval"
        assert w["sched_value"] == "2h"
        assert w["timeout_minutes"] == 5
        assert w["env_vars"] == "KEY=val"

    def test_update_worker_name_and_schedule(self, client, tmp_path):
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "ToUpdate",
            "task_path": task,
            "sched_type": "interval",
            "sched_value": "1h",
        })
        worker_id = r.get_json()["added"][0]

        r2 = client.put(f"/api/workers/{worker_id}", json={
            "name": "Updated Name",
            "task_path": task,
            "sched_type": "interval",
            "sched_value": "3h",
        })
        assert r2.status_code == 200

        workers = client.get("/api/workers").get_json()
        names = [w["name"] for w in workers]
        assert "Updated Name" in names
        assert "ToUpdate" not in names

    def test_update_nonexistent_worker_returns_404(self, client, tmp_path):
        task = make_task_file(tmp_path)
        r = client.put("/api/workers/9999", json={
            "name": "Ghost", "task_path": task,
            "sched_type": "interval", "sched_value": "1h",
        })
        assert r.status_code == 404

    def test_delete_worker_removes_from_list(self, client, tmp_path):
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "ToDelete",
            "task_path": task,
            "sched_type": "interval",
            "sched_value": "1h",
        })
        worker_id = r.get_json()["added"][0]

        client.delete(f"/api/workers/{worker_id}")

        workers = client.get("/api/workers").get_json()
        assert not any(w["id"] == worker_id for w in workers)

    def test_delete_nonexistent_worker_returns_404(self, client):
        r = client.delete("/api/workers/9999")
        assert r.status_code == 404

    def test_register_worker_missing_name_returns_400(self, client, tmp_path):
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "task_path": task,
            "sched_type": "interval",
            "sched_value": "1h",
        })
        assert r.status_code == 400


# ===========================================================================
# 2. Pause / Resume
# ===========================================================================

class TestPauseResume:
    def test_pause_sets_paused_flag_in_db(self, client, tmp_path):
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "Pausable",
            "task_path": task,
            "sched_type": "interval",
            "sched_value": "1h",
        })
        worker_id = r.get_json()["added"][0]

        r2 = client.post(f"/api/workers/{worker_id}/pause")
        assert r2.get_json()["paused"] is True

        rows = db_query("SELECT paused FROM workers WHERE id=?", (worker_id,))
        assert rows[0]["paused"] == 1

    def test_resume_clears_paused_flag_in_db(self, client, tmp_path):
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "Resumable",
            "task_path": task,
            "sched_type": "interval",
            "sched_value": "1h",
        })
        worker_id = r.get_json()["added"][0]

        client.post(f"/api/workers/{worker_id}/pause")   # pause
        r2 = client.post(f"/api/workers/{worker_id}/pause")  # resume
        assert r2.get_json()["paused"] is False

        rows = db_query("SELECT paused FROM workers WHERE id=?", (worker_id,))
        assert rows[0]["paused"] == 0

    def test_resume_calls_scheduler_add_job(self, client, tmp_path):
        """Resuming a worker must re-register it with the scheduler."""
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "AddJobCheck",
            "task_path": task,
            "sched_type": "interval",
            "sched_value": "1h",
        })
        worker_id = r.get_json()["added"][0]

        client.post(f"/api/workers/{worker_id}/pause")   # pause

        _app.scheduler.reset_mock()
        _app.scheduler.get_jobs.return_value = []

        client.post(f"/api/workers/{worker_id}/pause")   # resume

        assert _app.scheduler.add_job.called

    def test_toggle_pause_twice_returns_to_active(self, client, tmp_path):
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "Toggle",
            "task_path": task,
            "sched_type": "interval",
            "sched_value": "1h",
        })
        worker_id = r.get_json()["added"][0]

        r1 = client.post(f"/api/workers/{worker_id}/pause")
        assert r1.get_json()["paused"] is True

        _app.scheduler.reset_mock()
        _app.scheduler.get_jobs.return_value = []

        r2 = client.post(f"/api/workers/{worker_id}/pause")
        assert r2.get_json()["paused"] is False

    def test_pause_chain(self, client, tmp_path):
        step = make_task_file(tmp_path)
        r = client.post("/api/chains", json={
            "name": "PausableChain",
            "sched_type": "interval",
            "sched_value": "1h",
            "steps": [{"task_path": step, "stage": 0}],
        })
        chain_id = r.get_json()["chain_id"]

        r2 = client.post(f"/api/chains/{chain_id}/pause")
        assert r2.get_json()["paused"] is True

        rows = db_query("SELECT paused FROM chains WHERE id=?", (chain_id,))
        assert rows[0]["paused"] == 1


# ===========================================================================
# 3. Run Now
# ===========================================================================

class TestRunNow:
    def test_run_now_writes_manual_history_row(self, client, tmp_path):
        task = make_task_file(tmp_path, content="def run():\n    pass\n")
        r = client.post("/api/workers", json={
            "name": "RunNowWorker",
            "task_path": task,
            "sched_type": "interval",
            "sched_value": "1h",
        })
        worker_id = r.get_json()["added"][0]

        # Use an Event to know exactly when the background thread finishes
        done = threading.Event()

        def fake_task_runner(w_id, t_path, o_dir, trigger_type="scheduled", **kwargs):
            _app._record_run(
                worker_id=w_id,
                trigger_type=trigger_type,
                success=True,
                duration_ms=42,
                error_msg="",
            )
            done.set()

        with patch.object(_app, "_task_runner", side_effect=fake_task_runner):
            r2 = client.post(f"/api/workers/{worker_id}/run-now")
            assert r2.status_code == 200
            done.wait(timeout=3.0)

        r3 = client.get(f"/api/workers/{worker_id}/history")
        data = r3.get_json()
        assert data["total"] >= 1
        assert data["rows"][0]["trigger_type"] == "manual"
        assert data["rows"][0]["success"] == 1

    def test_run_now_returns_404_for_missing_worker(self, client):
        r = client.post("/api/workers/9999/run-now")
        assert r.status_code == 404

    def test_run_chain_now_writes_manual_history_row(self, client, tmp_path):
        step = make_task_file(tmp_path)
        r = client.post("/api/chains", json={
            "name": "RunNowChain",
            "sched_type": "interval",
            "sched_value": "1h",
            "steps": [{"task_path": step, "stage": 0}],
        })
        chain_id = r.get_json()["chain_id"]

        done = threading.Event()

        def fake_chain_runner(c_id, trigger_type="scheduled"):
            _app._record_run(
                chain_id=c_id,
                trigger_type=trigger_type,
                success=True,
                duration_ms=99,
                error_msg="",
            )
            done.set()

        with patch.object(_app, "_chain_runner", side_effect=fake_chain_runner):
            r2 = client.post(f"/api/chains/{chain_id}/run-now")
            assert r2.status_code == 200
            done.wait(timeout=3.0)

        r3 = client.get(f"/api/chains/{chain_id}/history")
        data = r3.get_json()
        assert data["total"] >= 1
        assert data["rows"][0]["trigger_type"] == "manual"


# ===========================================================================
# 4. Chain CRUD
# ===========================================================================

class TestChainCRUD:
    def test_create_chain_with_two_steps(self, client, tmp_path):
        s1 = make_task_file(tmp_path, "s1.py")
        s2 = make_task_file(tmp_path, "s2.py")
        r = client.post("/api/chains", json={
            "name": "TwoStepChain",
            "sched_type": "interval",
            "sched_value": "1h",
            "steps": [
                {"task_path": s1, "stage": 0},
                {"task_path": s2, "stage": 1},
            ],
        })
        assert r.status_code == 201
        chain_id = r.get_json()["chain_id"]

        chains = client.get("/api/chains").get_json()
        chain = next(c for c in chains if c["id"] == chain_id)
        assert len(chain["steps"]) == 2

    def test_chain_steps_stored_in_db(self, client, tmp_path):
        s1 = make_task_file(tmp_path, "db1.py")
        s2 = make_task_file(tmp_path, "db2.py")
        r = client.post("/api/chains", json={
            "name": "DBStepsChain",
            "sched_type": "interval",
            "sched_value": "1h",
            "steps": [
                {"task_path": s1, "stage": 0},
                {"task_path": s2, "stage": 1},
            ],
        })
        chain_id = r.get_json()["chain_id"]

        steps = db_query(
            "SELECT * FROM chain_steps WHERE chain_id=? ORDER BY order_index",
            (chain_id,)
        )
        assert len(steps) == 2
        assert steps[0]["order_index"] == 0
        assert steps[1]["order_index"] == 1

    def test_delete_chain_cascades_steps(self, client, tmp_path):
        s1 = make_task_file(tmp_path, "cas1.py")
        s2 = make_task_file(tmp_path, "cas2.py")
        r = client.post("/api/chains", json={
            "name": "CascadeChain",
            "sched_type": "interval",
            "sched_value": "1h",
            "steps": [
                {"task_path": s1, "stage": 0},
                {"task_path": s2, "stage": 1},
            ],
        })
        chain_id = r.get_json()["chain_id"]

        client.delete(f"/api/chains/{chain_id}")

        steps = db_query(
            "SELECT * FROM chain_steps WHERE chain_id=?", (chain_id,)
        )
        assert len(steps) == 0

    def test_delete_chain_removes_from_list(self, client, tmp_path):
        s1 = make_task_file(tmp_path, "gone.py")
        r = client.post("/api/chains", json={
            "name": "GoneChain",
            "sched_type": "interval",
            "sched_value": "1h",
            "steps": [{"task_path": s1, "stage": 0}],
        })
        chain_id = r.get_json()["chain_id"]

        client.delete(f"/api/chains/{chain_id}")

        chains = client.get("/api/chains").get_json()
        assert not any(c["id"] == chain_id for c in chains)

    def test_chain_missing_steps_returns_400(self, client):
        r = client.post("/api/chains", json={
            "name": "EmptyChain",
            "sched_type": "interval",
            "sched_value": "1h",
            "steps": [],
        })
        assert r.status_code == 400

    def test_chain_missing_name_returns_400(self, client, tmp_path):
        s = make_task_file(tmp_path)
        r = client.post("/api/chains", json={
            "sched_type": "interval",
            "sched_value": "1h",
            "steps": [{"task_path": s, "stage": 0}],
        })
        assert r.status_code == 400

    def test_delete_nonexistent_chain_returns_404(self, client):
        r = client.delete("/api/chains/9999")
        assert r.status_code == 404


# ===========================================================================
# 5. Chain Parallel Stages
# ===========================================================================

class TestChainParallelStages:
    def test_parallel_stage_stored_in_db(self, client, tmp_path):
        """Two steps at stage 0, one at stage 1 — verify stored stages."""
        p1 = make_task_file(tmp_path, "par1.py")
        p2 = make_task_file(tmp_path, "par2.py")
        p3 = make_task_file(tmp_path, "par3.py")
        r = client.post("/api/chains", json={
            "name": "ParallelChain",
            "sched_type": "interval",
            "sched_value": "1h",
            "steps": [
                {"task_path": p1, "stage": 0},
                {"task_path": p2, "stage": 0},   # parallel with p1
                {"task_path": p3, "stage": 1},   # sequential after stage 0
            ],
        })
        assert r.status_code == 201
        chain_id = r.get_json()["chain_id"]

        steps = db_query(
            "SELECT stage FROM chain_steps WHERE chain_id=? ORDER BY order_index",
            (chain_id,)
        )
        stages = [s["stage"] for s in steps]
        assert stages.count(0) == 2
        assert stages.count(1) == 1

    def test_parallel_stages_returned_via_api(self, client, tmp_path):
        """GET /api/chains should include the stage field on each step."""
        p1 = make_task_file(tmp_path, "api_p1.py")
        p2 = make_task_file(tmp_path, "api_p2.py")
        r = client.post("/api/chains", json={
            "name": "APIParallel",
            "sched_type": "interval",
            "sched_value": "1h",
            "steps": [
                {"task_path": p1, "stage": 0},
                {"task_path": p2, "stage": 0},
            ],
        })
        chain_id = r.get_json()["chain_id"]

        chains = client.get("/api/chains").get_json()
        chain = next(c for c in chains if c["id"] == chain_id)
        stages = [s["stage"] for s in chain["steps"]]
        assert all(stage == 0 for stage in stages)


def test_stage_groupby_logic():
    """Unit-test the itertools.groupby logic used in _chain_runner."""
    from itertools import groupby

    # Simulate sorted chain_steps (dicts matching sqlite3.Row behaviour)
    steps = [
        {"stage": 0, "order_index": 0, "task_path": "/a.py"},
        {"stage": 0, "order_index": 1, "task_path": "/b.py"},
        {"stage": 1, "order_index": 2, "task_path": "/c.py"},
    ]
    sorted_steps = sorted(steps, key=lambda s: (s["stage"], s["order_index"]))
    groups = [
        (stg, list(grp))
        for stg, grp in groupby(sorted_steps, key=lambda s: s["stage"])
    ]

    assert len(groups) == 2
    assert groups[0][0] == 0 and len(groups[0][1]) == 2   # stage 0: 2 parallel steps
    assert groups[1][0] == 1 and len(groups[1][1]) == 1   # stage 1: 1 sequential step


# ===========================================================================
# 6. Import / Export
# ===========================================================================

class TestImportExport:
    def test_export_returns_valid_json_structure(self, client, tmp_path):
        task = make_task_file(tmp_path)
        client.post("/api/workers", json={
            "name": "ExportWorker",
            "task_path": task,
            "sched_type": "interval",
            "sched_value": "1h",
        })
        r = client.get("/api/export")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert data["version"] == 1
        assert "workers" in data
        assert "chains" in data
        assert "groups" in data
        assert any(w["name"] == "ExportWorker" for w in data["workers"])

    def test_export_includes_chain_steps(self, client, tmp_path):
        s = make_task_file(tmp_path)
        client.post("/api/chains", json={
            "name": "ExportChain",
            "sched_type": "interval",
            "sched_value": "1h",
            "steps": [{"task_path": s, "stage": 0}],
        })
        data = json.loads(client.get("/api/export").data)
        chain = next(c for c in data["chains"] if c["name"] == "ExportChain")
        assert len(chain["steps"]) == 1
        assert chain["steps"][0]["stage"] == 0

    def test_export_wipe_import_restores_workers_and_chains(self, client, tmp_path):
        task = make_task_file(tmp_path, "exp_task.py")
        step = make_task_file(tmp_path, "exp_step.py")

        client.post("/api/workers", json={
            "name": "RestoredWorker",
            "task_path": task,
            "sched_type": "interval",
            "sched_value": "1h",
        })
        client.post("/api/chains", json={
            "name": "RestoredChain",
            "sched_type": "interval",
            "sched_value": "2h",
            "steps": [{"task_path": step, "stage": 0}],
        })

        # Export
        payload = json.loads(client.get("/api/export").data)

        # Wipe everything
        client.delete("/api/workers/all")
        for c in client.get("/api/chains").get_json():
            client.delete(f"/api/chains/{c['id']}")

        assert client.get("/api/workers").get_json() == []
        assert client.get("/api/chains").get_json() == []

        # Import
        r2 = client.post("/api/import", json=payload)
        assert r2.status_code == 200
        body = r2.get_json()
        assert body["imported_workers"] == 1
        assert body["imported_chains"] == 1

        workers = client.get("/api/workers").get_json()
        chains  = client.get("/api/chains").get_json()
        assert any(w["name"] == "RestoredWorker" for w in workers)
        assert any(c["name"] == "RestoredChain" for c in chains)

    def test_import_skips_existing_names(self, client, tmp_path):
        task = make_task_file(tmp_path)
        client.post("/api/workers", json={
            "name": "AlreadyHere",
            "task_path": task,
            "sched_type": "interval",
            "sched_value": "1h",
        })
        payload = json.loads(client.get("/api/export").data)

        # Re-import without wiping — same name should be skipped
        r2 = client.post("/api/import", json=payload)
        body = r2.get_json()
        assert body["imported_workers"] == 0
        assert body["skipped"] >= 1

    def test_import_invalid_format_returns_400(self, client):
        r = client.post("/api/import", json={"version": 999, "workers": []})
        assert r.status_code == 400

    def test_import_creates_missing_groups(self, client, tmp_path):
        payload = {
            "version": 1,
            "groups": [{"name": "AutoCreatedGroup"}],
            "workers": [],
            "chains": [],
        }
        r = client.post("/api/import", json=payload)
        assert r.status_code == 200
        assert r.get_json()["imported_groups"] == 1

        groups = client.get("/api/groups").get_json()
        assert any(g["name"] == "AutoCreatedGroup" for g in groups)


# ===========================================================================
# 7. Templates
# ===========================================================================

_TEMPLATE_CASES = [
    ("folder_backup",  {"source": "/tmp/src", "dest": "/tmp/dst", "keep": 3}),
    ("file_cleanup",   {"folder": "/tmp/cleanup", "pattern": "*.log", "days": 7}),
    ("folder_watcher", {"watch": "/tmp/watch",
                        "rules": [{"ext": ".pdf", "dest": "/tmp/pdf"}]}),
    ("uptime_check",   {"url": "https://example.com", "log_file": "/tmp/up.log"}),
    ("open_url",       {"url": "https://example.com"}),
]


@pytest.mark.parametrize("template_type,config", _TEMPLATE_CASES,
                         ids=[t for t, _ in _TEMPLATE_CASES])
def test_template_generates_file_with_run_function(client, template_type, config):
    r = client.post("/api/templates", json={
        "template_type": template_type,
        "config": config,
        "worker_name": f"Test {template_type}",
        "sched_type": "interval",
        "sched_value": "1h",
    })
    assert r.status_code == 201, r.get_json()

    workers = client.get("/api/workers").get_json()
    # Worker name is exactly "Test <template_type>"
    w = next(
        (w for w in workers if w["name"] == f"Test {template_type}"),
        None,
    )
    assert w is not None, f"Worker 'Test {template_type}' not found in worker list"

    script_path = Path(w["task_path"])
    assert script_path.exists(), f"Generated script not found: {script_path}"
    source = script_path.read_text(encoding="utf-8")
    assert "if __name__" in source or "def run():" in source, f"No entry point found in {script_path}"


def test_unknown_template_type_returns_400(client):
    r = client.post("/api/templates", json={
        "template_type": "does_not_exist",
        "config": {},
        "worker_name": "Bad Worker",
        "sched_type": "interval",
        "sched_value": "1h",
    })
    assert r.status_code == 400


def test_template_worker_name_required(client):
    r = client.post("/api/templates", json={
        "template_type": "open_url",
        "config": {"url": "https://example.com"},
        "worker_name": "",
        "sched_type": "interval",
        "sched_value": "1h",
    })
    assert r.status_code == 400


# ===========================================================================
# 8. Run History
# ===========================================================================

class TestRunHistory:
    def test_history_endpoint_returns_correct_structure(self, client, tmp_path):
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "HistWorker",
            "task_path": task,
            "sched_type": "interval",
            "sched_value": "1h",
        })
        worker_id = r.get_json()["added"][0]

        _app._record_run(worker_id=worker_id, trigger_type="manual",
                         success=True, duration_ms=123, error_msg="")

        r2 = client.get(f"/api/workers/{worker_id}/history")
        data = r2.get_json()

        assert "rows" in data
        assert "total" in data
        assert "offset" in data
        assert "limit" in data
        assert data["total"] == 1

        row = data["rows"][0]
        assert row["trigger_type"] == "manual"
        assert row["success"] == 1
        assert row["duration_ms"] == 123
        assert "triggered_at" in row
        assert "error_msg" in row

    def test_history_success_and_failure_rows(self, client, tmp_path):
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "SuccFail",
            "task_path": task,
            "sched_type": "interval",
            "sched_value": "1h",
        })
        worker_id = r.get_json()["added"][0]

        _app._record_run(worker_id=worker_id, trigger_type="scheduled",
                         success=True, duration_ms=10, error_msg="")
        _app._record_run(worker_id=worker_id, trigger_type="scheduled",
                         success=False, duration_ms=20, error_msg="Boom")

        data = client.get(f"/api/workers/{worker_id}/history").get_json()
        assert data["total"] == 2
        # Ordered most-recent first (ORDER BY id DESC)
        assert data["rows"][0]["success"] == 0
        assert data["rows"][0]["error_msg"] == "Boom"
        assert data["rows"][1]["success"] == 1

    def test_history_pagination_limit_and_offset(self, client, tmp_path):
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "PagWorker",
            "task_path": task,
            "sched_type": "interval",
            "sched_value": "1h",
        })
        worker_id = r.get_json()["added"][0]

        for i in range(7):
            _app._record_run(worker_id=worker_id, trigger_type="scheduled",
                             success=True, duration_ms=i * 10, error_msg="")

        # Page 1
        d1 = client.get(f"/api/workers/{worker_id}/history?limit=3&offset=0").get_json()
        assert len(d1["rows"]) == 3
        assert d1["total"] == 7

        # Page 2
        d2 = client.get(f"/api/workers/{worker_id}/history?limit=3&offset=3").get_json()
        assert len(d2["rows"]) == 3

        # Page 3 (last page — only 1 row left)
        d3 = client.get(f"/api/workers/{worker_id}/history?limit=3&offset=6").get_json()
        assert len(d3["rows"]) == 1

        # All row IDs should be unique across pages
        all_ids = (
            [row["triggered_at"] + str(row["duration_ms"]) for row in d1["rows"]] +
            [row["triggered_at"] + str(row["duration_ms"]) for row in d2["rows"]] +
            [row["triggered_at"] + str(row["duration_ms"]) for row in d3["rows"]]
        )
        assert len(all_ids) == len(set(all_ids))

    def test_chain_history_endpoint(self, client, tmp_path):
        step = make_task_file(tmp_path)
        r = client.post("/api/chains", json={
            "name": "HistChain",
            "sched_type": "interval",
            "sched_value": "1h",
            "steps": [{"task_path": step, "stage": 0}],
        })
        chain_id = r.get_json()["chain_id"]

        _app._record_run(chain_id=chain_id, trigger_type="scheduled",
                         success=True, duration_ms=500, error_msg="")

        data = client.get(f"/api/chains/{chain_id}/history").get_json()
        assert data["total"] == 1
        assert data["rows"][0]["trigger_type"] == "scheduled"


# ===========================================================================
# 9. Log Endpoint
# ===========================================================================

class TestLogEndpoint:
    def test_logs_returns_required_fields(self, client):
        _app.add_log("INFO", "hello from test")
        r = client.get("/api/logs?since=0")
        assert r.status_code == 200
        data = r.get_json()
        assert "entries" in data
        assert "total" in data
        assert len(data["entries"]) >= 1
        entry = data["entries"][0]
        assert "ts" in entry
        assert "level" in entry
        assert "msg" in entry

    def test_logs_since_offset_returns_only_new_entries(self, client):
        # Snapshot current buffer size
        baseline = client.get("/api/logs?since=0").get_json()["total"]

        # Add exactly 3 new entries
        _app.add_log("INFO",  "new entry 1")
        _app.add_log("OK",    "new entry 2")
        _app.add_log("ERROR", "new entry 3")

        r = client.get(f"/api/logs?since={baseline}")
        data = r.get_json()
        assert len(data["entries"]) == 3
        msgs = [e["msg"] for e in data["entries"]]
        assert "new entry 1" in msgs
        assert "new entry 2" in msgs
        assert "new entry 3" in msgs

    def test_logs_since_past_end_returns_empty(self, client):
        total = client.get("/api/logs?since=0").get_json()["total"]
        r = client.get(f"/api/logs?since={total + 100}")
        assert r.get_json()["entries"] == []

    def test_logs_level_is_preserved(self, client):
        baseline = client.get("/api/logs?since=0").get_json()["total"]
        _app.add_log("ERROR", "something broke")
        r = client.get(f"/api/logs?since={baseline}")
        entries = r.get_json()["entries"]
        assert entries[0]["level"] == "ERROR"
        assert entries[0]["msg"] == "something broke"
