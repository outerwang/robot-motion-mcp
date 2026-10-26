"""Phase 2 tests: run manager lifecycle, isolation, recovery and validation binding.

Strategy: a synthetic "fake upstream" New_Design-like tree plus a fake planner
script exercise every execution semantic hermetically (no real New_Design file is
executed here). The REAL planner smoke test lives in scripts/smoke_test_real.py and
is run manually with budget control, not inside the unit suite.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from robot_motion_mcp.adapters.new_design import NewDesignAdapter
from robot_motion_mcp.config import WorkspacePaths
from robot_motion_mcp.runner.manager import RunError, RunManager
from robot_motion_mcp.runner.validate import validate_run

FAKE_PLANNER = '''\
import json, os, sys, time
from pathlib import Path
root = Path(__file__).resolve().parents[2]
mode = os.environ.get("FAKE_MODE", "success")
if mode == "fail":
    sys.stderr.write("fake planner explosion\\n")
    sys.exit(3)
if mode == "timeout":
    time.sleep(300)
out = root / "Data_Config" / "All_Roads.json"
if mode == "garbage":
    out.write_text("{not json", encoding="utf-8")
    sys.exit(0)
robots = {
    "R1": {"paths": {"a": {"b": {"found": True}}}},
    "R2": {"paths": {"c": {"d": {"found": True}}}},
}
if mode == "partial":
    robots["R2"]["paths"]["c"]["d"]["found"] = False
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(
    json.dumps({"seed": 46, "algorithm": "Lazy_PRM", "robots": robots, "unsolved_welds": []}),
    encoding="utf-8",
)
sys.exit(0)
'''


def make_fake_upstream(root: Path) -> Path:
    fake = root / "New_Design_fake"
    (fake / "Parameter_Config").mkdir(parents=True)
    (fake / "Data_Config").mkdir()
    (fake / "Robot_System" / "urdf").mkdir(parents=True)
    (fake / "Output" / "Arrange_The_Working_Area" / "seed_46").mkdir(parents=True)
    (fake / "Functional_File" / "Get_All_Roads").mkdir(parents=True)
    (fake / "Welding_Torch_Sampling_and_models").mkdir()
    (fake / "Welding_Torch_Sampling_and_models" / "stl").mkdir()

    (fake / "Parameter_Config" / "Get_All_Roads.json").write_text(
        json.dumps({"seed": 46, "algorithm": "Lazy_PRM"}), encoding="utf-8"
    )
    (fake / "Data_Config" / "Collection_of_Sampling_Points.json").write_text(
        json.dumps({"143-FR69B": []}), encoding="utf-8"
    )
    (fake / "Data_Config" / "Random_XZL.json").write_text(
        json.dumps({"placements": [{"seed": 46, "poses": {"A": {"x": 0.0}}}]}), encoding="utf-8"
    )
    (fake / "Data_Config" / "Allocation_Summary.json").write_text(
        json.dumps({"best_seed": 46, "images": [{"seed": 46, "robots": {"R1": []}}]}),
        encoding="utf-8",
    )
    (fake / "Data_Config" / "Robot_Workspace_Cache.json").write_text("{}", encoding="utf-8")
    (fake / "Data_Config" / "All_Roads.json").write_text(
        json.dumps({"seed": 46, "robots": {"R1": {"paths": {}}}, "welds": {}}), encoding="utf-8"
    )
    (fake / "Robot_System" / "urdf" / "robot_system.urdf").write_text("<robot/>", encoding="utf-8")
    (fake / "Output" / "Arrange_The_Working_Area" / "seed_46" / "seed_46_walls.urdf").write_text(
        "<walls/>", encoding="utf-8"
    )
    (fake / "Functional_File" / "Get_All_Roads" / "Get_All_Roads.py").write_text(
        FAKE_PLANNER, encoding="utf-8"
    )
    return fake


@pytest.fixture()
def env(tmp_path, monkeypatch):
    fake = make_fake_upstream(tmp_path)
    paths = WorkspacePaths(
        workspace=tmp_path,
        new_design_root=fake,
        audit_root=tmp_path / "audit",
        runs_root=tmp_path / "runs",
        planner_python=Path(sys.executable),
    )
    monkeypatch.setenv("FAKE_MODE", "success")
    manager = RunManager(paths, NewDesignAdapter(paths))
    return paths, manager, monkeypatch


def wait_for(manager: RunManager, run_id: str, timeout: float = 40.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = manager.status(run_id)
        if status["execution_status"] not in ("PENDING", "RUNNING"):
            return status
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} did not finish within {timeout}s")


# --- parameter validation -----------------------------------------------------


def test_start_rejects_injection_style_task_ids(env):
    _paths, manager, _mp = env
    for bad in ["ND-SEED-46 & whoami", "../New_Design", "ND-SEED-", "T1_CM2B", "ND-SEED-46; dir"]:
        with pytest.raises(RunError):
            manager.start(bad)


def test_start_rejects_unknown_seed(env):
    _paths, manager, _mp = env
    with pytest.raises(RunError, match="does not exist"):
        manager.start("ND-SEED-999")


def test_start_rejects_non_whitelisted_planner(env):
    _paths, manager, _mp = env
    with pytest.raises(RunError, match="not whitelisted"):
        manager.start("ND-SEED-46", planner="RRT-Connect; rm -rf")


def test_start_rejects_bad_budget(env):
    _paths, manager, _mp = env
    for bad in [5, 99999, "abc", None]:
        with pytest.raises(RunError):
            manager.start("ND-SEED-46", time_budget_seconds=bad)


def test_start_rejects_bad_request_id(env):
    _paths, manager, _mp = env
    with pytest.raises(RunError):
        manager.start("ND-SEED-46", request_id="bad id with spaces")


# --- lifecycle ----------------------------------------------------------------


def test_successful_run_lifecycle(env):
    _paths, manager, _mp = env
    result = manager.start("ND-SEED-46", time_budget_seconds=60, request_id="demo-1")
    assert result["accepted"] is True and result["duplicate"] is False
    run_id = result["run_id"]
    status = wait_for(manager, run_id)
    assert status["execution_status"] == "COMPLETED"
    assert status["exit_code"] == 0
    assert status["plan_result"] == "PATH_FOUND"
    assert status["result_sha256"]
    assert status["upstream_integrity"]["verified"] is True
    assert status["upstream_integrity"]["diff"]["changed"] == []
    # the run must have executed inside its own worktree, not upstream
    assert "worktree" in status["worktree"]
    validation = validate_run(run_id, manager, _make_audit_stub(env))
    assert validation["validation_status"] == "NOT_RUN"
    assert "No independent strict validator" in validation["error_or_incomplete_reason"]


def test_failed_planner_exit(env, monkeypatch):
    _paths, manager, mp = env
    monkeypatch.setenv("FAKE_MODE", "fail")
    run_id = manager.start("ND-SEED-46", time_budget_seconds=60)["run_id"]
    status = wait_for(manager, run_id)
    assert status["execution_status"] == "FAILED"
    assert status["exit_code"] == 3
    assert status["plan_result"] == "NOT_AVAILABLE"


def test_timeout_is_timeout_not_infeasible(env):
    _paths, manager, mp = env
    mp.setenv("FAKE_MODE", "timeout")
    run_id = manager.start("ND-SEED-46", time_budget_seconds=10)["run_id"]
    status = wait_for(manager, run_id, timeout=60)
    assert status["execution_status"] == "TIMEOUT"
    assert status["killed_on_timeout"] is True
    assert status["plan_result"] == "NOT_AVAILABLE"
    assert "not a proof of infeasibility" in (status["error"] or "")


def test_garbage_output_is_error(env, monkeypatch):
    _paths, manager, mp = env
    monkeypatch.setenv("FAKE_MODE", "garbage")
    run_id = manager.start("ND-SEED-46", time_budget_seconds=60)["run_id"]
    status = wait_for(manager, run_id)
    assert status["execution_status"] == "COMPLETED"
    assert status["plan_result"] == "ERROR"


def test_partial_result_reported(env, monkeypatch):
    _paths, manager, mp = env
    monkeypatch.setenv("FAKE_MODE", "partial")
    run_id = manager.start("ND-SEED-46", time_budget_seconds=60)["run_id"]
    status = wait_for(manager, run_id)
    assert status["plan_result"] == "PATH_FOUND"
    details = status["plan_result_details"]
    assert details["paths_found"] == 1 and details["paths_total"] == 2
    assert "partial" in details["note"]


def test_idempotent_request_id(env):
    _paths, manager, _mp = env
    first = manager.start("ND-SEED-46", time_budget_seconds=60, request_id="same-1")
    wait_for(manager, first["run_id"])
    second = manager.start("ND-SEED-46", time_budget_seconds=60, request_id="same-1")
    assert second["duplicate"] is True
    assert second["run_id"] == first["run_id"]


def test_single_flight_policy(env):
    _paths, manager, mp = env
    mp.setenv("FAKE_MODE", "timeout")  # long-running
    first = manager.start("ND-SEED-46", time_budget_seconds=15)
    with pytest.raises(RunError, match="real planning run is active"):
        manager.start("ND-SEED-46", time_budget_seconds=15)
    # cleanup: let the waiter finish (timeout) so other tests are unaffected
    wait_for(manager, first["run_id"], timeout=60)


# --- restart recovery -----------------------------------------------------------


def test_recovery_unknown_when_no_artifact(env, tmp_path):
    _paths, manager, _mp = env
    run_id = "R-20260101-000001-dead01"
    run_dir = manager.runs_root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "task_id": "ND-SEED-46",
                "execution_status": "RUNNING",
                "pid": 999999999,
                "worktree": None,
                "result_file": None,
            }
        ),
        encoding="utf-8",
    )
    status = manager.status(run_id)
    assert status["execution_status"] == "UNKNOWN"
    assert "cannot be determined" in status["error"]
    # reconciliation must be persisted, not recomputed differently next time
    assert manager.load_status(run_id)["execution_status"] == "UNKNOWN"


def test_load_status_rejects_path_traversal(env, tmp_path):
    _paths, manager, _mp = env
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "status.json").write_text(json.dumps({"secret": "not a run"}), encoding="utf-8")
    with pytest.raises(RunError, match="unknown run_id"):
        manager.load_status("../outside")


def test_added_or_removed_upstream_files_fail_integrity_verification(env, monkeypatch):
    from robot_motion_mcp.runner import manager as manager_module

    _paths, manager, _mp = env
    run_id = "R-20260101-000002-dead02"
    run_dir = manager.runs_root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "upstream_manifest_before.json").write_text(
        json.dumps({"kept.txt": "same", "removed.txt": "old"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        manager_module,
        "tree_manifest",
        lambda _root: {"kept.txt": "same", "added.txt": "new"},
    )
    status = {
        "run_id": run_id,
        "execution_status": "COMPLETED",
        "worktree": None,
        "upstream_integrity": {"before_files": 2},
    }

    manager._finalize(run_id, status, "2026-01-01T00:00:00")

    assert status["upstream_integrity"]["diff"] == {
        "changed": [],
        "added": ["added.txt"],
        "removed": ["removed.txt"],
    }
    assert status["upstream_integrity"]["verified"] is False


def test_recovery_completed_from_artifact(env):
    _paths, manager, mp = env
    mp.setenv("FAKE_MODE", "success")
    run_id = manager.start("ND-SEED-46", time_budget_seconds=60)["run_id"]
    wait_for(manager, run_id)
    # simulate a restart that lost the final state write
    status = manager.load_status(run_id)
    status["execution_status"] = "RUNNING"
    status["pid"] = 999999999
    status["result_file"] = None
    (manager.runs_root / run_id / "status.json").write_text(
        json.dumps(status), encoding="utf-8"
    )
    reconciled = manager.status(run_id)
    assert reconciled["execution_status"] == "COMPLETED"
    assert reconciled["recovered"] is True
    assert reconciled["plan_result"] == "PATH_FOUND"


# --- helpers --------------------------------------------------------------------


def _make_audit_stub(env):
    from robot_motion_mcp.adapters.research_audit import ResearchAuditAdapter

    paths, _manager, _mp = env
    return ResearchAuditAdapter(paths)
