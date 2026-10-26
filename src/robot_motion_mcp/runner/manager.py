"""Persistent run management for controlled real planning executions.

State machine (execution_status):
    PENDING -> RUNNING -> COMPLETED | FAILED | TIMEOUT
                        -> UNKNOWN (only after service restart when the process
                           can no longer be observed; never silently promoted)

plan_result (independent of execution_status):
    PATH_FOUND | NO_CANDIDATE_WITHIN_BUDGET | ERROR | NOT_AVAILABLE

Search timeout is never reported as proof of infeasibility.

Durability: every run owns ``runs/<run_id>/`` with an atomically replaced
``status.json`` (temp file + os.replace), the full planner log, the isolated
worktree and the upstream integrity manifests. A restart re-reads status.json and
reconciles RUNNING entries against the real process table before reporting.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .. import __version__
from ..adapters.new_design import NewDesignAdapter
from ..config import WorkspacePaths
from . import worktree as worktree_mod
from .integrity import diff_manifests, file_sha256, tree_manifest

EXECUTION_STATES = ("PENDING", "RUNNING", "COMPLETED", "FAILED", "TIMEOUT", "UNKNOWN")
PLAN_RESULT_STATES = ("PATH_FOUND", "NO_CANDIDATE_WITHIN_BUDGET", "ERROR", "NOT_AVAILABLE")
RUN_VALIDATION_STATES = ("NOT_RUN", "PASS", "FAIL", "INCOMPLETE", "ERROR")

TASK_ID_RE = re.compile(r"^ND-SEED-(\d+)$")
RUN_ID_RE = re.compile(r"^R-\d{8}-\d{6}-[0-9a-f]{6}$")
REQUEST_ID_RE = re.compile(r"^[\w.\-]{1,64}$")
PLANNER_WHITELIST = worktree_mod.ALGORITHM_WHITELIST

MIN_BUDGET_S = 10.0
MAX_BUDGET_S = 1800.0
DEFAULT_BUDGET_S = 120.0

# Files whose identity binds a run to the exact upstream inputs/models used.
_INPUT_VERSION_FILES = (
    "Data_Config/Collection_of_Sampling_Points.json",
    "Data_Config/Random_XZL.json",
    "Data_Config/Allocation_Summary.json",
    "Data_Config/Robot_Workspace_Cache.json",
    "Robot_System/urdf/robot_system.urdf",
)


class RunError(Exception):
    pass


class RunNotFoundError(RunError):
    pass


def _atomic_write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _pid_alive(pid: int | None) -> bool | None:
    """Windows-only liveness probe. None = cannot determine."""
    if pid is None or pid <= 0:
        return None
    try:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    except Exception:
        return None


class RunManager:
    """Owns ``runs_root``; one real planning subprocess at a time (v1 policy)."""

    def __init__(self, paths: WorkspacePaths, new_design: NewDesignAdapter):
        self.paths = paths
        self.new_design = new_design
        self.runs_root = paths.runs_root
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._active_run_id: str | None = None
        self._requests_path = self.runs_root / "requests.json"

    # -- discovery ----------------------------------------------------------

    def _requests(self) -> dict:
        if self._requests_path.is_file():
            try:
                return json.loads(self._requests_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _record_request(self, request_id: str, run_id: str) -> None:
        mapping = self._requests()
        mapping[request_id] = run_id
        _atomic_write_json(self._requests_path, mapping)

    def list_run_ids(self) -> list[str]:
        if not self.runs_root.is_dir():
            return []
        return sorted(
            p.parent.name
            for p in self.runs_root.glob("*/status.json")
        )

    def load_status(self, run_id: str) -> dict:
        if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
            raise RunNotFoundError(f"unknown run_id {run_id!r}")
        path = self.runs_root / run_id / "status.json"
        if not path.is_file():
            raise RunNotFoundError(f"unknown run_id {run_id!r}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RunError(f"run record for {run_id!r} is unreadable: {exc}") from exc

    # -- start --------------------------------------------------------------

    def start(
        self,
        task_id: str,
        planner: str = "Lazy_PRM",
        time_budget_seconds: float = DEFAULT_BUDGET_S,
        request_id: str | None = None,
    ) -> dict:
        if not isinstance(task_id, str) or not TASK_ID_RE.match(task_id):
            raise RunError(
                f"invalid task_id {task_id!r}; expected 'ND-SEED-<seed>' (digits only). "
                "Shell metacharacters and paths are not accepted."
            )
        if planner not in PLANNER_WHITELIST:
            raise RunError(
                f"planner {planner!r} is not whitelisted; available: {list(PLANNER_WHITELIST)}. "
                "Selection is applied to the isolated worktree config, never to upstream files."
            )
        try:
            budget = float(time_budget_seconds)
        except (TypeError, ValueError) as exc:
            raise RunError(f"time_budget_seconds must be a number, got {time_budget_seconds!r}") from exc
        if not (MIN_BUDGET_S <= budget <= MAX_BUDGET_S):
            raise RunError(
                f"time_budget_seconds must be within [{MIN_BUDGET_S:.0f}, {MAX_BUDGET_S:.0f}], got {budget}"
            )
        if request_id is not None:
            if not isinstance(request_id, str) or not REQUEST_ID_RE.match(request_id):
                raise RunError(
                    "request_id must match [\\w.-]{1,64} when provided"
                )
            existing = self._requests().get(request_id)
            if existing:
                status = self.load_status(existing)
                return {
                    "run_id": existing,
                    "accepted": False,
                    "duplicate": True,
                    "execution_status": status.get("execution_status"),
                    "message": "request_id already bound to an existing run; returning that run",
                }

        seed = int(TASK_ID_RE.match(task_id).group(1))  # type: ignore[union-attr]
        self._validate_seed_startable(seed)

        with self._lock:
            if self._active_run_id is not None:
                active = self.load_status(self._active_run_id)
                if active.get("execution_status") == "RUNNING" and _pid_alive(active.get("pid")):
                    raise RunError(
                        f"another real planning run is active ({self._active_run_id}); "
                        "this server executes at most one real planner at a time"
                    )
                self._active_run_id = None

            run_id = f"R-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
            run_dir = self.runs_root / run_id
            run_dir.mkdir(parents=True, exist_ok=False)
            if request_id:
                self._record_request(request_id, run_id)

            status = self._new_status(run_id, task_id, seed, planner, budget, request_id)
            _atomic_write_json(run_dir / "status.json", status)
            log_lines: list[str] = []

            def log(msg: str) -> None:
                log_lines.append(f"{time.strftime('%H:%M:%S')} {msg}")

            try:
                # upstream protection evidence: hash the whole upstream tree first
                log("hashing upstream New_Design tree (before)")
                manifest_before = tree_manifest(self.paths.new_design_root)
                status["upstream_integrity"] = {
                    "before_files": len(manifest_before),
                    "manifest_before_sha256": file_sha256(
                        self._manifest_snapshot(run_dir, manifest_before)
                    ),
                    "diff": None,
                    "verified": False,
                }
                status["execution_status"] = "PENDING"
                _atomic_write_json(run_dir / "status.json", status)

                wt = worktree_mod.build_worktree(
                    self.paths, run_dir, seed, planner, log=log
                )
                status["worktree"] = str(wt)
                status["worktree_config_sha256"] = file_sha256(
                    wt / "Parameter_Config" / "Get_All_Roads.json"
                )
                status["execution_status"] = "PENDING"
                _atomic_write_json(run_dir / "status.json", status)
                status["planner_log"] = str(run_dir / "planner.log")
                _atomic_write_json(run_dir / "status.json", status)
                self._write_run_log(run_dir, log_lines)

                self._spawn(run_id, status)
                return {
                    "run_id": run_id,
                    "accepted": True,
                    "duplicate": False,
                    "execution_status": "RUNNING",
                    "task_id": task_id,
                    "seed": seed,
                    "planner": planner,
                    "budget_s": budget,
                    "message": (
                        "real planner started inside an isolated worktree; "
                        "poll get_plan_status for progress"
                    ),
                }
            except Exception as exc:
                status["execution_status"] = "FAILED"
                status["error"] = f"start failed: {exc}"
                status["finished_at"] = _now()
                _atomic_write_json(run_dir / "status.json", status)
                self._write_run_log(run_dir, log_lines + [f"ERROR {exc}"])
                raise

    def _new_status(self, run_id, task_id, seed, planner, budget, request_id) -> dict:
        return {
            "run_id": run_id,
            "task_id": task_id,
            "seed": seed,
            "planner": planner,
            "request_id": request_id,
            "mock": False,
            "execution_status": "PENDING",
            "accepted": True,
            "created_at": _now(),
            "started_at": None,
            "finished_at": None,
            "pid": None,
            "exit_code": None,
            "budget_s": budget,
            "wall_s": None,
            "killed_on_timeout": False,
            "worktree": None,
            "worktree_config_sha256": None,
            "planner_log": None,
            "result_file": None,
            "result_sha256": None,
            "recovered": False,
            "error": None,
            "input_versions": {
                rel: file_sha256(self.paths.new_design_root / rel)
                for rel in _INPUT_VERSION_FILES
                if (self.paths.new_design_root / rel).is_file()
            },
            "upstream_integrity": None,
            "plan_result": None,
            "plan_result_details": None,
            "validation": None,
            "tool_version": __version__,
        }

    def _validate_seed_startable(self, seed: int) -> None:
        tasks = {t["task_id"]: t for t in self.new_design.list_tasks()}
        task_id = f"ND-SEED-{seed}"
        if task_id not in tasks:
            raise RunError(
                f"{task_id} does not exist in the New_Design snapshot "
                "(seed not present in Random_XZL/Allocation_Summary); task ids are never invented"
            )
        info = self.new_design.get_task_info(task_id)
        completeness = info.get("data_completeness", {})
        walls_ok = info.get("placement", {}).get("recorded") and completeness.get(
            "allocation_summary_entry_present"
        )
        if not walls_ok:
            raise RunError(
                f"{task_id} has no allocation record; the roads step requires walls produced "
                "by the allocate step, which this server does not run"
            )
        walls = (
            self.paths.new_design_root
            / "Output"
            / "Arrange_The_Working_Area"
            / f"seed_{seed:02d}"
            / f"seed_{seed:02d}_walls.urdf"
        )
        if not walls.is_file():
            raise RunError(f"walls URDF missing upstream: {walls}")

    # -- subprocess lifecycle -------------------------------------------------

    def _spawn(self, run_id: str, status: dict) -> None:
        worktree = Path(status["worktree"])
        script = worktree / "Functional_File" / "Get_All_Roads" / "Get_All_Roads.py"
        log_path = Path(status["planner_log"])
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["MPLBACKEND"] = "Agg"
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        log_fh = log_path.open("ab")
        try:
            proc = subprocess.Popen(  # noqa: S603 - fixed executable/argv, never model-provided
                [str(self.paths.planner_python), str(script)],
                cwd=str(worktree),
                env=env,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        finally:
            log_fh.close()  # child keeps its inherited handle
        status["pid"] = proc.pid
        status["execution_status"] = "RUNNING"
        status["started_at"] = _now()
        _atomic_write_json(self.runs_root / run_id / "status.json", status)
        self._active_run_id = run_id
        waiter = threading.Thread(
            target=self._wait,
            args=(run_id, proc, float(status["budget_s"])),
            name=f"run-waiter-{run_id}",
            daemon=True,
        )
        waiter.start()

    def _wait(self, run_id: str, proc: subprocess.Popen, budget: float) -> None:
        status = self.load_status(run_id)
        started = time.monotonic()
        try:
            proc.wait(timeout=budget)
            status["exit_code"] = proc.returncode
            status["wall_s"] = time.monotonic() - started
            status["execution_status"] = "COMPLETED" if proc.returncode == 0 else "FAILED"
            if proc.returncode != 0:
                status["error"] = f"planner exited with code {proc.returncode}; see planner log"
        except subprocess.TimeoutExpired:
            status["killed_on_timeout"] = True
            status["wall_s"] = time.monotonic() - started
            self._kill_tree(proc)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            status["exit_code"] = proc.returncode
            status["execution_status"] = "TIMEOUT"
            status["error"] = (
                f"planner exceeded budget of {budget:.0f}s and was terminated; "
                "timeout is not a proof of infeasibility"
            )
        finished = _now()
        self._finalize(run_id, status, finished)

    def _kill_tree(self, proc: subprocess.Popen) -> None:
        if os.name == "nt":
            subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
        try:
            proc.kill()
        except OSError:
            pass

    def _finalize(self, run_id: str, status: dict, finished: str) -> None:
        run_dir = self.runs_root / run_id
        status["finished_at"] = finished
        try:
            self._attach_results(run_dir, status)
        except Exception as exc:  # noqa: BLE001 - finalize must not crash the waiter
            status["error"] = (status.get("error") or "") + f" | finalize error: {exc}"
        # upstream protection evidence: re-hash and diff
        try:
            manifest_after = tree_manifest(self.paths.new_design_root)
            before = status.get("upstream_integrity") or {}
            diff = diff_manifests({}, {})  # placeholder replaced below
            before_manifest_path = run_dir / "upstream_manifest_before.json"
            if before_manifest_path.is_file():
                before_manifest = json.loads(before_manifest_path.read_text(encoding="utf-8"))
                diff = diff_manifests(before_manifest, manifest_after)
            else:
                diff = {"changed": [], "added": sorted(manifest_after), "removed": []}
            status["upstream_integrity"] = {
                "before_files": before.get("before_files"),
                "after_files": len(manifest_after),
                "diff": diff,
                "verified": not any(
                    diff.get(key) for key in ("changed", "added", "removed")
                ),
                "checked_at": _now(),
            }
        except Exception as exc:  # noqa: BLE001
            status["upstream_integrity"] = {
                "verified": None,
                "error": f"post-run manifest failed: {exc}",
            }
        _atomic_write_json(run_dir / "status.json", status)
        if self._active_run_id == run_id:
            self._active_run_id = None

    def _attach_results(self, run_dir: str | Path, status: dict) -> None:
        worktree = Path(status["worktree"]) if status.get("worktree") else None
        result_rel = "Data_Config/All_Roads.json"
        result_file = (worktree / result_rel) if worktree else None
        if result_file and result_file.is_file():
            status["result_file"] = str(result_file)
            status["result_sha256"] = file_sha256(result_file)
            status["plan_result"], status["plan_result_details"] = _parse_plan_result(
                result_file
            )
        else:
            status["plan_result"] = "NOT_AVAILABLE"
            status["plan_result_details"] = {
                "reason": "planner output Data_Config/All_Roads.json absent from the worktree"
            }

    # -- status & recovery ----------------------------------------------------

    def status(self, run_id: str) -> dict:
        status = self.load_status(run_id)
        if status.get("execution_status") == "RUNNING":
            alive = _pid_alive(status.get("pid"))
            if alive is False:
                status = self._reconcile(run_id, status)
        return status

    def _reconcile(self, run_id: str, status: dict) -> dict:
        """Process died while we recorded RUNNING (e.g. service restart)."""
        result_file = status.get("result_file")
        worktree = status.get("worktree")
        candidate = Path(worktree) / "Data_Config" / "All_Roads.json" if worktree else None
        if candidate and candidate.is_file() and not result_file:
            try:
                self._attach_results(run_dir=self.runs_root / run_id, status=status)
            except Exception:  # noqa: BLE001
                pass
        if status.get("result_file") and status.get("plan_result") not in (None, "NOT_AVAILABLE"):
            status["execution_status"] = "COMPLETED"
            status["recovered"] = True
            status["error"] = (
                "process ended before its exit was recorded (service restart); "
                "status reconciled from the planner output artifact, exit code unknown"
            )
        else:
            status["execution_status"] = "UNKNOWN"
            status["error"] = (
                "recorded RUNNING but the process is no longer alive and no result "
                "artifact exists; the actual outcome cannot be determined and is "
                "deliberately not reported as success or failure"
            )
        status["finished_at"] = status.get("finished_at") or _now()
        _atomic_write_json(self.runs_root / run_id / "status.json", status)
        return status

    def set_validation(self, run_id: str, validation: dict) -> None:
        status = self.load_status(run_id)
        status["validation"] = validation
        _atomic_write_json(self.runs_root / run_id / "status.json", status)

    # -- helpers --------------------------------------------------------------

    def _manifest_snapshot(self, run_dir: Path, manifest: dict) -> Path:
        path = run_dir / "upstream_manifest_before.json"
        _atomic_write_json(path, manifest)
        return path

    def _write_run_log(self, run_dir: Path, lines: list[str]) -> None:
        (run_dir / "manager.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_plan_result(result_file: Path) -> tuple[str, dict]:
    try:
        data = json.loads(result_file.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return "ERROR", {"reason": f"planner output is not valid JSON: {exc}"}
    robots = data.get("robots", {})
    found = 0
    total = 0
    per_robot = {}
    for rid, rdata in robots.items():
        paths = rdata.get("paths", {})
        r_total = sum(len(v) for v in paths.values())
        r_found = sum(1 for v in paths.values() for p in v.values() if p.get("found"))
        per_robot[rid] = {"found": r_found, "total": r_total}
        found += r_found
        total += r_total
    unsolved = data.get("unsolved_welds", [])
    if total == 0:
        return "NO_CANDIDATE_WITHIN_BUDGET", {
            "per_robot": per_robot,
            "unsolved_welds": unsolved,
            "note": "planner produced no path pairs",
        }
    result = "PATH_FOUND" if found > 0 else "NO_CANDIDATE_WITHIN_BUDGET"
    details = {
        "seed": data.get("seed"),
        "algorithm": data.get("algorithm"),
        "per_robot": per_robot,
        "paths_found": found,
        "paths_total": total,
        "unsolved_welds": unsolved,
    }
    if unsolved or found < total:
        details["note"] = "partial result; see unsolved_welds and per_robot counts"
    return result, details


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
