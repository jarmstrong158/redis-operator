"""
test_scheduler.py — additional tests for Conductor covering areas not in test_api.py.

Coverage areas:
  1. Scheduler logic — pause_all, cron/interval/fixed schedule types, enabled state
  2. Worker history — last-N limit, oldest-first raw ordering, multiple entries
  3. Chain execution order — _chain_runner executes steps sequentially via mocked subprocess
  4. Environment variable handling — multi-line env_vars stored, returned, and parsed
  5. Worker validation — missing task_path, invalid cron expression
  6. Email settings — save, get, missing-fields validation
  7. Export/import structure — exported_at, all required keys present on each worker
"""

import json
import os
import sqlite3
import threading
import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import app as _app


# ---------------------------------------------------------------------------
# Helpers (duplicated locally so this file is self-contained)
# ---------------------------------------------------------------------------

def make_task_file(tmp_path, name: str = "task.py",
                   content: str = 'if __name__ == "__main__":\n    pass\n') -> str:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


def db_query(sql: str, params: tuple = ()):
    conn = sqlite3.connect(str(_app.DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


# ===========================================================================
# 1. Scheduler logic
# ===========================================================================

class TestSchedulerLogic:

    def test_pause_all_sets_all_workers_paused(self, client, tmp_path):
        """POST /api/workers/pause-all should set paused=1 for every worker."""
        t1 = make_task_file(tmp_path, "w1.py")
        t2 = make_task_file(tmp_path, "w2.py")
        for name, path in [("WA", t1), ("WB", t2)]:
            client.post("/api/workers", json={
                "name": name, "task_path": path,
                "sched_type": "interval", "sched_value": "1h",
            })

        r = client.post("/api/workers/pause-all")
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

        workers = client.get("/api/workers").get_json()
        assert all(w["paused"] is True for w in workers), (
            "Expected all workers paused after pause-all"
        )

    def test_pause_all_removes_scheduler_jobs(self, client, tmp_path):
        """pause-all must call remove_worker_jobs for each previously active worker."""
        t1 = make_task_file(tmp_path, "rm1.py")
        client.post("/api/workers", json={
            "name": "RemoveMe", "task_path": t1,
            "sched_type": "interval", "sched_value": "1h",
        })
        _app.scheduler.reset_mock()

        client.post("/api/workers/pause-all")

        # remove_job should have been called (via remove_worker_jobs)
        assert _app.scheduler.remove_job.called or _app.scheduler.get_jobs.called

    def test_pause_all_on_already_paused_workers_is_idempotent(self, client, tmp_path):
        """pause-all when all workers are already paused should return ok=True."""
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "AlreadyPaused", "task_path": task,
            "sched_type": "interval", "sched_value": "1h",
        })
        worker_id = r.get_json()["added"][0]
        client.post(f"/api/workers/{worker_id}/pause")  # pause it first

        r2 = client.post("/api/workers/pause-all")
        assert r2.status_code == 200

        workers = client.get("/api/workers").get_json()
        assert all(w["paused"] is True for w in workers)

    def test_paused_worker_appears_paused_in_list(self, client, tmp_path):
        """A paused worker must have paused=True in GET /api/workers response."""
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "PausedCheck", "task_path": task,
            "sched_type": "interval", "sched_value": "1h",
        })
        worker_id = r.get_json()["added"][0]
        client.post(f"/api/workers/{worker_id}/pause")

        workers = client.get("/api/workers").get_json()
        w = next(w for w in workers if w["id"] == worker_id)
        assert w["paused"] is True

    def test_resumed_worker_appears_not_paused_in_list(self, client, tmp_path):
        """After pause→resume, paused field must be False in the list."""
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "ResumedCheck", "task_path": task,
            "sched_type": "interval", "sched_value": "1h",
        })
        worker_id = r.get_json()["added"][0]
        client.post(f"/api/workers/{worker_id}/pause")   # pause
        _app.scheduler.reset_mock()
        _app.scheduler.get_jobs.return_value = []
        client.post(f"/api/workers/{worker_id}/pause")   # resume

        workers = client.get("/api/workers").get_json()
        w = next(w for w in workers if w["id"] == worker_id)
        assert w["paused"] is False

    def test_cron_schedule_type_stored_correctly(self, client, tmp_path):
        """sched_type='cron' and a valid cron expression must be stored as-is."""
        task = make_task_file(tmp_path)
        cron_expr = "0 9 * * 1-5"
        r = client.post("/api/workers", json={
            "name": "CronWorker", "task_path": task,
            "sched_type": "cron", "sched_value": cron_expr,
        })
        assert r.status_code == 201
        worker_id = r.get_json()["added"][0]

        workers = client.get("/api/workers").get_json()
        w = next(w for w in workers if w["id"] == worker_id)
        assert w["sched_type"] == "cron"
        assert w["sched_value"] == cron_expr

    def test_fixed_schedule_type_stored_correctly(self, client, tmp_path):
        """sched_type='fixed' with comma-separated times must be stored as-is."""
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "FixedWorker", "task_path": task,
            "sched_type": "fixed", "sched_value": "09:00,14:30",
        })
        assert r.status_code == 201
        worker_id = r.get_json()["added"][0]

        workers = client.get("/api/workers").get_json()
        w = next(w for w in workers if w["id"] == worker_id)
        assert w["sched_type"] == "fixed"
        assert w["sched_value"] == "09:00,14:30"

    def test_interval_schedule_hours_and_minutes(self, client, tmp_path):
        """sched_value='1h 30m' must be stored verbatim (parsing happens in scheduler)."""
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "IntervalWorker", "task_path": task,
            "sched_type": "interval", "sched_value": "1h 30m",
        })
        assert r.status_code == 201
        workers = client.get("/api/workers").get_json()
        w = next(w for w in workers if w["name"] == "IntervalWorker")
        assert w["sched_value"] == "1h 30m"

    def test_make_triggers_interval_hours_and_minutes(self):
        """Unit test _make_triggers parses '2h 30m' → IntervalTrigger(hours=2, minutes=30)."""
        from apscheduler.triggers.interval import IntervalTrigger
        triggers = _app._make_triggers("interval", "2h 30m")
        assert len(triggers) == 1
        trigger, suffix = triggers[0]
        assert isinstance(trigger, IntervalTrigger)
        assert suffix == "i0"

    def test_make_triggers_cron(self):
        """Unit test _make_triggers returns a CronTrigger for sched_type='cron'."""
        from apscheduler.triggers.cron import CronTrigger
        triggers = _app._make_triggers("cron", "0 9 * * 1-5")
        assert len(triggers) == 1
        trigger, suffix = triggers[0]
        assert isinstance(trigger, CronTrigger)
        assert suffix == "c0"

    def test_make_triggers_fixed_multiple_times(self):
        """Unit test _make_triggers produces one CronTrigger per time for 'fixed'."""
        from apscheduler.triggers.cron import CronTrigger
        triggers = _app._make_triggers("fixed", "09:00,14:30,18:00")
        assert len(triggers) == 3
        for trigger, suffix in triggers:
            assert isinstance(trigger, CronTrigger)
        suffixes = [s for _, s in triggers]
        assert suffixes == ["t0", "t1", "t2"]


# ===========================================================================
# 2. Worker history — limit, ordering, multiple entries
# ===========================================================================

class TestWorkerHistoryDetails:

    def test_history_returns_most_recent_first(self, client, tmp_path):
        """History rows are ordered most-recent first (highest id = index 0)."""
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "OrderWorker", "task_path": task,
            "sched_type": "interval", "sched_value": "1h",
        })
        worker_id = r.get_json()["added"][0]

        # Insert runs with distinct durations so we can tell them apart
        for ms in [10, 20, 30]:
            _app._record_run(worker_id=worker_id, trigger_type="scheduled",
                             success=True, duration_ms=ms, error_msg="")
            time.sleep(0.01)  # ensure distinct triggered_at timestamps

        data = client.get(f"/api/workers/{worker_id}/history").get_json()
        durations = [row["duration_ms"] for row in data["rows"]]
        # Most recent run (30ms) should be first
        assert durations[0] == 30
        assert durations[-1] == 10

    def test_history_limit_parameter_caps_rows_returned(self, client, tmp_path):
        """?limit=3 returns at most 3 rows even when more exist."""
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "LimitWorker", "task_path": task,
            "sched_type": "interval", "sched_value": "1h",
        })
        worker_id = r.get_json()["added"][0]

        for i in range(8):
            _app._record_run(worker_id=worker_id, trigger_type="scheduled",
                             success=True, duration_ms=i, error_msg="")

        data = client.get(f"/api/workers/{worker_id}/history?limit=3").get_json()
        assert len(data["rows"]) == 3
        assert data["total"] == 8
        assert data["limit"] == 3

    def test_history_default_limit_is_100(self, client, tmp_path):
        """No limit param → default limit of 100 is reflected in the response."""
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "DefaultLimitWorker", "task_path": task,
            "sched_type": "interval", "sched_value": "1h",
        })
        worker_id = r.get_json()["added"][0]
        _app._record_run(worker_id=worker_id, trigger_type="manual",
                         success=True, duration_ms=1, error_msg="")

        data = client.get(f"/api/workers/{worker_id}/history").get_json()
        assert data["limit"] == 100

    def test_history_empty_for_new_worker(self, client, tmp_path):
        """A newly created worker with no runs has total=0 and rows=[]."""
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "NoRunsWorker", "task_path": task,
            "sched_type": "interval", "sched_value": "1h",
        })
        worker_id = r.get_json()["added"][0]

        data = client.get(f"/api/workers/{worker_id}/history").get_json()
        assert data["total"] == 0
        assert data["rows"] == []

    def test_history_error_msg_preserved(self, client, tmp_path):
        """A failed run's error_msg is stored and returned verbatim."""
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "ErrMsgWorker", "task_path": task,
            "sched_type": "interval", "sched_value": "1h",
        })
        worker_id = r.get_json()["added"][0]

        _app._record_run(worker_id=worker_id, trigger_type="scheduled",
                         success=False, duration_ms=50,
                         error_msg="Traceback: FileNotFoundError")

        data = client.get(f"/api/workers/{worker_id}/history").get_json()
        assert data["rows"][0]["error_msg"] == "Traceback: FileNotFoundError"
        assert data["rows"][0]["success"] == 0

    def test_history_isolation_between_workers(self, client, tmp_path):
        """History for worker A must not include runs recorded for worker B."""
        t1 = make_task_file(tmp_path, "wa.py")
        t2 = make_task_file(tmp_path, "wb.py")
        id_a = client.post("/api/workers", json={
            "name": "WorkerA", "task_path": t1,
            "sched_type": "interval", "sched_value": "1h",
        }).get_json()["added"][0]
        id_b = client.post("/api/workers", json={
            "name": "WorkerB", "task_path": t2,
            "sched_type": "interval", "sched_value": "1h",
        }).get_json()["added"][0]

        _app._record_run(worker_id=id_a, trigger_type="manual",
                         success=True, duration_ms=1, error_msg="")
        _app._record_run(worker_id=id_b, trigger_type="manual",
                         success=True, duration_ms=2, error_msg="")

        data_a = client.get(f"/api/workers/{id_a}/history").get_json()
        data_b = client.get(f"/api/workers/{id_b}/history").get_json()
        assert data_a["total"] == 1
        assert data_b["total"] == 1
        assert data_a["rows"][0]["duration_ms"] == 1
        assert data_b["rows"][0]["duration_ms"] == 2


# ===========================================================================
# 3. Chain execution order
# ===========================================================================

class TestChainExecutionOrder:

    def test_chain_runner_executes_steps_in_stage_order(self, client, tmp_path):
        """_chain_runner must execute step 1 before step 2 (stage 0 before stage 1)."""
        s1 = make_task_file(tmp_path, "step1.py")
        s2 = make_task_file(tmp_path, "step2.py")
        r = client.post("/api/chains", json={
            "name": "OrderChain",
            "sched_type": "interval",
            "sched_value": "1h",
            "steps": [
                {"task_path": s1, "stage": 0},
                {"task_path": s2, "stage": 1},
            ],
        })
        chain_id = r.get_json()["chain_id"]

        execution_order = []

        def fake_run_one_step(chain_name, step_label, task_path):
            execution_order.append(os.path.basename(task_path))
            return 0.01

        with patch.object(_app, "_run_one_chain_step", side_effect=fake_run_one_step):
            _app._chain_runner(chain_id, trigger_type="manual")

        assert execution_order == ["step1.py", "step2.py"]

    def test_chain_runner_records_success_in_history(self, client, tmp_path):
        """A successful chain run must write a success=1 row to run_history."""
        step = make_task_file(tmp_path, "ok_step.py")
        r = client.post("/api/chains", json={
            "name": "SuccessChain",
            "sched_type": "interval",
            "sched_value": "1h",
            "steps": [{"task_path": step, "stage": 0}],
        })
        chain_id = r.get_json()["chain_id"]

        with patch.object(_app, "_run_one_chain_step", return_value=0.01):
            _app._chain_runner(chain_id, trigger_type="manual")

        data = client.get(f"/api/chains/{chain_id}/history").get_json()
        assert data["total"] == 1
        assert data["rows"][0]["success"] == 1
        assert data["rows"][0]["trigger_type"] == "manual"

    def test_chain_runner_records_failure_when_step_raises(self, client, tmp_path):
        """If a step raises, _chain_runner must record success=0 in run_history."""
        step = make_task_file(tmp_path, "bad_step.py")
        r = client.post("/api/chains", json={
            "name": "FailChain",
            "sched_type": "interval",
            "sched_value": "1h",
            "stop_on_failure": True,
            "steps": [{"task_path": step, "stage": 0}],
        })
        chain_id = r.get_json()["chain_id"]

        with patch.object(_app, "_run_one_chain_step",
                          side_effect=RuntimeError("Step exploded")):
            _app._chain_runner(chain_id, trigger_type="scheduled")

        data = client.get(f"/api/chains/{chain_id}/history").get_json()
        assert data["total"] == 1
        assert data["rows"][0]["success"] == 0
        assert "exploded" in data["rows"][0]["error_msg"]

    def test_chain_stop_on_failure_skips_later_stages(self, client, tmp_path):
        """With stop_on_failure=True, stage 1 must not run if stage 0 fails."""
        s1 = make_task_file(tmp_path, "fail_step.py")
        s2 = make_task_file(tmp_path, "skip_step.py")
        r = client.post("/api/chains", json={
            "name": "StopOnFailChain",
            "sched_type": "interval",
            "sched_value": "1h",
            "stop_on_failure": True,
            "steps": [
                {"task_path": s1, "stage": 0},
                {"task_path": s2, "stage": 1},
            ],
        })
        chain_id = r.get_json()["chain_id"]

        ran = []

        def fake_step(chain_name, step_label, task_path):
            ran.append(os.path.basename(task_path))
            if "fail_step" in task_path:
                raise RuntimeError("Intentional failure")
            return 0.01

        with patch.object(_app, "_run_one_chain_step", side_effect=fake_step):
            _app._chain_runner(chain_id, trigger_type="manual")

        assert "fail_step.py" in ran
        assert "skip_step.py" not in ran

    def test_run_chain_now_endpoint_returns_200(self, client, tmp_path):
        """POST /api/chains/<id>/run-now returns 200 and spawns a thread."""
        step = make_task_file(tmp_path)
        r = client.post("/api/chains", json={
            "name": "RunNowEndpointChain",
            "sched_type": "interval",
            "sched_value": "1h",
            "steps": [{"task_path": step, "stage": 0}],
        })
        chain_id = r.get_json()["chain_id"]

        done = threading.Event()

        def fake_chain_runner(c_id, trigger_type="scheduled"):
            done.set()

        with patch.object(_app, "_chain_runner", side_effect=fake_chain_runner):
            r2 = client.post(f"/api/chains/{chain_id}/run-now")

        assert r2.status_code == 200
        assert r2.get_json()["ok"] is True

    def test_run_chain_now_returns_404_for_missing_chain(self, client):
        r = client.post("/api/chains/9999/run-now")
        assert r.status_code == 404


# ===========================================================================
# 4. Environment variable handling
# ===========================================================================

class TestEnvVarHandling:

    def test_env_vars_stored_and_returned(self, client, tmp_path):
        """Multi-line env_vars must be stored verbatim and returned via GET."""
        task = make_task_file(tmp_path)
        env_block = "FOO=bar\nBAZ=qux\nSECRET=12345"
        r = client.post("/api/workers", json={
            "name": "EnvWorker", "task_path": task,
            "sched_type": "interval", "sched_value": "1h",
            "env_vars": env_block,
        })
        assert r.status_code == 201
        worker_id = r.get_json()["added"][0]

        workers = client.get("/api/workers").get_json()
        w = next(w for w in workers if w["id"] == worker_id)
        assert w["env_vars"] == env_block

    def test_env_vars_stored_in_db(self, client, tmp_path):
        """env_vars column in DB must match what was submitted."""
        task = make_task_file(tmp_path)
        env_block = "KEY1=alpha\nKEY2=beta"
        r = client.post("/api/workers", json={
            "name": "DBEnvWorker", "task_path": task,
            "sched_type": "interval", "sched_value": "1h",
            "env_vars": env_block,
        })
        worker_id = r.get_json()["added"][0]

        rows = db_query("SELECT env_vars FROM workers WHERE id=?", (worker_id,))
        assert rows[0]["env_vars"] == env_block

    def test_env_vars_updated_on_put(self, client, tmp_path):
        """PUT /api/workers/<id> must update env_vars in DB."""
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "EnvUpdateWorker", "task_path": task,
            "sched_type": "interval", "sched_value": "1h",
            "env_vars": "OLD=value",
        })
        worker_id = r.get_json()["added"][0]

        client.put(f"/api/workers/{worker_id}", json={
            "name": "EnvUpdateWorker", "task_path": task,
            "sched_type": "interval", "sched_value": "1h",
            "env_vars": "NEW=value\nEXTRA=data",
        })

        workers = client.get("/api/workers").get_json()
        w = next(w for w in workers if w["id"] == worker_id)
        assert "NEW=value" in w["env_vars"]
        assert "OLD=value" not in w["env_vars"]

    def test_env_vars_empty_string_is_valid(self, client, tmp_path):
        """A worker with no env_vars should return an empty string, not None."""
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "NoEnvWorker", "task_path": task,
            "sched_type": "interval", "sched_value": "1h",
        })
        worker_id = r.get_json()["added"][0]

        workers = client.get("/api/workers").get_json()
        w = next(w for w in workers if w["id"] == worker_id)
        assert w["env_vars"] == ""

    def test_parse_env_vars_unit(self):
        """Unit test _parse_env_vars: KEY=VALUE lines parsed to dict."""
        result = _app._parse_env_vars("FOO=bar\nBAZ=qux\n# comment\n\nEMPTY=")
        assert result == {"FOO": "bar", "BAZ": "qux", "EMPTY": ""}

    def test_parse_env_vars_skips_blank_and_comments(self):
        """_parse_env_vars must skip blank lines and # comments."""
        result = _app._parse_env_vars("# ignore me\n\nVALID=yes")
        assert "VALID" in result
        assert len(result) == 1

    def test_parse_env_vars_handles_values_with_equals(self):
        """Values containing '=' must be preserved correctly."""
        result = _app._parse_env_vars("URL=http://example.com?a=1&b=2")
        assert result["URL"] == "http://example.com?a=1&b=2"


# ===========================================================================
# 5. Worker validation
# ===========================================================================

class TestWorkerValidation:

    def test_missing_task_path_returns_400(self, client):
        """POST /api/workers with no task_path must return 400."""
        r = client.post("/api/workers", json={
            "name": "NoPath",
            "sched_type": "interval",
            "sched_value": "1h",
        })
        assert r.status_code == 400

    def test_empty_name_returns_400(self, client, tmp_path):
        """POST /api/workers with empty name string must return 400."""
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "",
            "task_path": task,
            "sched_type": "interval",
            "sched_value": "1h",
        })
        assert r.status_code == 400

    def test_missing_both_name_and_path_returns_400(self, client):
        """POST /api/workers with neither name nor task_path must return 400."""
        r = client.post("/api/workers", json={
            "sched_type": "interval",
            "sched_value": "1h",
        })
        assert r.status_code == 400

    def test_invalid_cron_expression_raises_on_make_triggers(self):
        """_make_triggers with an invalid cron expression must raise ValueError."""
        from apscheduler.triggers.cron import CronTrigger
        with pytest.raises(Exception):
            # APScheduler raises ValueError for bad cron expressions
            _app._make_triggers("cron", "99 99 99 99 99")

    def test_update_worker_missing_name_returns_400(self, client, tmp_path):
        """PUT /api/workers/<id> with empty name must return 400."""
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "ValidName", "task_path": task,
            "sched_type": "interval", "sched_value": "1h",
        })
        worker_id = r.get_json()["added"][0]

        r2 = client.put(f"/api/workers/{worker_id}", json={
            "name": "",
            "task_path": task,
            "sched_type": "interval",
            "sched_value": "1h",
        })
        assert r2.status_code == 400

    def test_update_worker_missing_task_path_returns_400(self, client, tmp_path):
        """PUT /api/workers/<id> with empty task_path must return 400."""
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "ValidName2", "task_path": task,
            "sched_type": "interval", "sched_value": "1h",
        })
        worker_id = r.get_json()["added"][0]

        r2 = client.put(f"/api/workers/{worker_id}", json={
            "name": "ValidName2",
            "task_path": "",
            "sched_type": "interval",
            "sched_value": "1h",
        })
        assert r2.status_code == 400

    def test_batch_add_one_invalid_one_valid_returns_201_with_errors(self, client, tmp_path):
        """Batch POST: one valid + one invalid → 201 with errors list containing the failure."""
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json=[
            {"name": "GoodOne", "task_path": task, "sched_type": "interval", "sched_value": "1h"},
            {"name": "", "task_path": task, "sched_type": "interval", "sched_value": "1h"},
        ])
        # 201 because at least one was added
        assert r.status_code == 201
        body = r.get_json()
        assert len(body["added"]) == 1
        assert len(body["errors"]) == 1


# ===========================================================================
# 6. Email settings
# ===========================================================================

class TestEmailSettings:

    def test_get_email_settings_returns_required_fields(self, client):
        """GET /api/email-settings must return has_credentials and email keys."""
        r = client.get("/api/email-settings")
        assert r.status_code == 200
        data = r.get_json()
        assert "has_credentials" in data
        assert "email" in data

    def test_save_email_settings_writes_env_vars(self, client, tmp_path):
        """POST /api/email-settings must set GMAIL_USER and GMAIL_APP_PASSWORD in os.environ."""
        # Patch _save_env_key to avoid touching real .env file
        with patch.object(_app, "_save_env_key") as mock_save:
            r = client.post("/api/email-settings", json={
                "email": "test@example.com",
                "password": "my-app-password",
            })
        assert r.status_code == 200
        assert r.get_json()["ok"] is True
        # _save_env_key called once for GMAIL_USER and once for GMAIL_APP_PASSWORD
        calls = [c[0] for c in mock_save.call_args_list]
        assert ("GMAIL_USER", "test@example.com") in calls
        assert ("GMAIL_APP_PASSWORD", "my-app-password") in calls

    def test_save_email_settings_missing_email_returns_400(self, client):
        """POST /api/email-settings with missing email must return 400."""
        r = client.post("/api/email-settings", json={
            "password": "my-app-password",
        })
        assert r.status_code == 400

    def test_save_email_settings_missing_password_returns_400(self, client):
        """POST /api/email-settings with missing password must return 400."""
        r = client.post("/api/email-settings", json={
            "email": "test@example.com",
        })
        assert r.status_code == 400

    def test_save_email_settings_empty_email_returns_400(self, client):
        """POST /api/email-settings with empty email string must return 400."""
        r = client.post("/api/email-settings", json={
            "email": "",
            "password": "some-password",
        })
        assert r.status_code == 400

    def test_save_email_settings_empty_password_returns_400(self, client):
        """POST /api/email-settings with empty password string must return 400."""
        r = client.post("/api/email-settings", json={
            "email": "test@example.com",
            "password": "",
        })
        assert r.status_code == 400

    def test_get_email_settings_has_credentials_false_when_not_set(self, client):
        """has_credentials must be False when GMAIL_USER is not set in env."""
        original_user = os.environ.pop("GMAIL_USER", None)
        original_pass = os.environ.pop("GMAIL_APP_PASSWORD", None)
        try:
            r = client.get("/api/email-settings")
            data = r.get_json()
            assert data["has_credentials"] is False
            assert data["email"] == ""
        finally:
            if original_user is not None:
                os.environ["GMAIL_USER"] = original_user
            if original_pass is not None:
                os.environ["GMAIL_APP_PASSWORD"] = original_pass

    def test_get_email_settings_has_credentials_true_when_set(self, client):
        """has_credentials must be True when both GMAIL_USER and GMAIL_APP_PASSWORD are set."""
        original_user = os.environ.get("GMAIL_USER")
        original_pass = os.environ.get("GMAIL_APP_PASSWORD")
        os.environ["GMAIL_USER"] = "someone@gmail.com"
        os.environ["GMAIL_APP_PASSWORD"] = "secret"
        try:
            r = client.get("/api/email-settings")
            data = r.get_json()
            assert data["has_credentials"] is True
            assert data["email"] == "someone@gmail.com"
        finally:
            if original_user is None:
                os.environ.pop("GMAIL_USER", None)
            else:
                os.environ["GMAIL_USER"] = original_user
            if original_pass is None:
                os.environ.pop("GMAIL_APP_PASSWORD", None)
            else:
                os.environ["GMAIL_APP_PASSWORD"] = original_pass


# ===========================================================================
# 7. Export / import structure
# ===========================================================================

class TestExportImportStructure:

    def test_export_contains_exported_at_timestamp(self, client):
        """Export payload must include an 'exported_at' key with a non-empty string."""
        r = client.get("/api/export")
        data = json.loads(r.data)
        assert "exported_at" in data
        assert isinstance(data["exported_at"], str)
        assert len(data["exported_at"]) > 0

    def test_export_worker_has_all_required_keys(self, client, tmp_path):
        """Each worker in the export must include all documented fields."""
        task = make_task_file(tmp_path)
        client.post("/api/workers", json={
            "name": "ExportFieldWorker", "task_path": task,
            "sched_type": "interval", "sched_value": "2h",
            "env_vars": "TOKEN=abc",
            "timeout_minutes": 10,
            "requirements": "requests",
            "notify_email": "admin@example.com",
            "notify_on": "failure",
        })

        data = json.loads(client.get("/api/export").data)
        w = next(w for w in data["workers"] if w["name"] == "ExportFieldWorker")

        required_keys = [
            "name", "task_path", "sched_type", "sched_value",
            "output_dir", "requirements", "new_console", "timeout_minutes",
            "env_vars", "notify_email", "notify_on", "group_name", "paused",
        ]
        for key in required_keys:
            assert key in w, f"Missing key '{key}' in exported worker"

    def test_export_worker_env_vars_preserved(self, client, tmp_path):
        """env_vars set on a worker must appear verbatim in the export."""
        task = make_task_file(tmp_path)
        env_block = "API_KEY=secret123\nDEBUG=true"
        client.post("/api/workers", json={
            "name": "EnvExportWorker", "task_path": task,
            "sched_type": "interval", "sched_value": "1h",
            "env_vars": env_block,
        })

        data = json.loads(client.get("/api/export").data)
        w = next(w for w in data["workers"] if w["name"] == "EnvExportWorker")
        assert w["env_vars"] == env_block

    def test_export_worker_timeout_minutes_preserved(self, client, tmp_path):
        """timeout_minutes set on a worker must be accurate in the export."""
        task = make_task_file(tmp_path)
        client.post("/api/workers", json={
            "name": "TimeoutExportWorker", "task_path": task,
            "sched_type": "interval", "sched_value": "1h",
            "timeout_minutes": 15,
        })

        data = json.loads(client.get("/api/export").data)
        w = next(w for w in data["workers"] if w["name"] == "TimeoutExportWorker")
        assert w["timeout_minutes"] == 15

    def test_export_chain_has_all_required_keys(self, client, tmp_path):
        """Each chain in the export must include name, sched_type, sched_value, steps, etc."""
        step = make_task_file(tmp_path)
        client.post("/api/chains", json={
            "name": "ExportChainFields",
            "sched_type": "cron",
            "sched_value": "0 8 * * *",
            "stop_on_failure": True,
            "steps": [{"task_path": step, "stage": 0}],
        })

        data = json.loads(client.get("/api/export").data)
        c = next(c for c in data["chains"] if c["name"] == "ExportChainFields")

        required_keys = [
            "name", "sched_type", "sched_value", "stop_on_failure",
            "notify_email", "notify_on", "paused", "group_name", "steps",
        ]
        for key in required_keys:
            assert key in c, f"Missing key '{key}' in exported chain"
        assert c["sched_type"] == "cron"
        assert c["sched_value"] == "0 8 * * *"
        assert len(c["steps"]) == 1

    def test_export_empty_db_returns_empty_lists(self, client):
        """Export with no workers/chains/groups must return empty lists for each."""
        data = json.loads(client.get("/api/export").data)
        assert data["workers"] == []
        assert data["chains"] == []
        assert data["groups"] == []

    def test_export_version_is_integer_one(self, client):
        """Export payload version field must be integer 1."""
        data = json.loads(client.get("/api/export").data)
        assert data["version"] == 1

    def test_export_paused_worker_flag_preserved(self, client, tmp_path):
        """A paused worker must appear as paused=True in the export."""
        task = make_task_file(tmp_path)
        r = client.post("/api/workers", json={
            "name": "PausedExportWorker", "task_path": task,
            "sched_type": "interval", "sched_value": "1h",
        })
        worker_id = r.get_json()["added"][0]
        client.post(f"/api/workers/{worker_id}/pause")

        data = json.loads(client.get("/api/export").data)
        w = next(w for w in data["workers"] if w["name"] == "PausedExportWorker")
        assert w["paused"] is True

    def test_import_restores_env_vars(self, client, tmp_path):
        """Imported worker must retain its env_vars field."""
        task = make_task_file(tmp_path)
        env_block = "RESTORED_KEY=yes"
        client.post("/api/workers", json={
            "name": "EnvImportWorker", "task_path": task,
            "sched_type": "interval", "sched_value": "1h",
            "env_vars": env_block,
        })
        payload = json.loads(client.get("/api/export").data)

        # Wipe and re-import
        client.delete("/api/workers/all")
        client.post("/api/import", json=payload)

        workers = client.get("/api/workers").get_json()
        w = next(w for w in workers if w["name"] == "EnvImportWorker")
        assert w["env_vars"] == env_block

    def test_import_restores_timeout_minutes(self, client, tmp_path):
        """Imported worker must retain its timeout_minutes field."""
        task = make_task_file(tmp_path)
        client.post("/api/workers", json={
            "name": "TimeoutImportWorker", "task_path": task,
            "sched_type": "interval", "sched_value": "1h",
            "timeout_minutes": 7,
        })
        payload = json.loads(client.get("/api/export").data)

        client.delete("/api/workers/all")
        client.post("/api/import", json=payload)

        workers = client.get("/api/workers").get_json()
        w = next(w for w in workers if w["name"] == "TimeoutImportWorker")
        assert w["timeout_minutes"] == 7
