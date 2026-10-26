"""MCP server (stdio) exposing six tools over existing planning assets.

Phase 1 (read-only): list_tasks / get_task_info / get_experiment_report
Phase 2 (controlled execution): start_motion_plan / get_plan_status / validate_motion

start_motion_plan runs the upstream planner inside a per-run isolated worktree copy
(never upstream), tracked by a persistent run manager. validate_motion reports
strictly bound validation status and never upgrades an unvalidated candidate.

Deliberately NOT provided: real robot execution of any kind, or any way to relax
collision or process constraints.
"""

from __future__ import annotations

import re
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from . import SERVER_NAME, __version__
from .adapters.base import AdapterError
from .config import WorkspacePaths
from .registry import Registry
from .runner.manager import (
    DEFAULT_BUDGET_S,
    MAX_BUDGET_S,
    MIN_BUDGET_S,
    PLANNER_WHITELIST,
    RunError,
    RunManager,
)
from .runner.validate import ValidateError, validate_run

_TASK_ID_RE = re.compile(r"^ND-SEED-\d+$")


class TaskSummary(BaseModel):
    task_id: str
    source_id: str
    mock: bool
    kind: str
    summary: str | None = None


class SourceInfo(BaseModel):
    source_id: str
    available: bool
    root: str | None = None
    read_only: bool = True
    mock: bool = False
    description: str = ""


class TaskListOutput(BaseModel):
    ok: bool
    server: str
    server_version: str
    task_count: int
    sources: list[SourceInfo]
    tasks: list[TaskSummary]


class TaskInfoOutput(BaseModel):
    ok: bool
    task_id: str
    source_id: str
    mock: bool = False
    kind: str
    info: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)


class ExperimentReportOutput(BaseModel):
    ok: bool
    run_id: str
    mock: bool = False
    report: dict[str, Any]


class StartPlanOutput(BaseModel):
    ok: bool
    run_id: str
    accepted: bool
    duplicate: bool = False
    execution_status: str
    task_id: str
    seed: int | None = None
    planner: str | None = None
    budget_s: float | None = None
    message: str


class PlanStatusOutput(BaseModel):
    ok: bool
    run_id: str
    task_id: str | None = None
    execution_status: str
    plan_result: str | None = None
    validation_status: str | None = None
    details: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)


class ValidateMotionOutput(BaseModel):
    ok: bool
    run_id: str
    task_id: str | None = None
    source: str | None = None
    candidate_path_hash: str | None = None
    validation_status: str
    validation_method: str | None = None
    model_version: dict[str, Any]
    evidence_reference: list[Any] = Field(default_factory=list)
    error_or_incomplete_reason: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


def build_server(paths: WorkspacePaths | None = None) -> MCPServer:
    paths = paths or WorkspacePaths.from_env()
    registry = Registry(paths)
    run_manager = RunManager(paths, registry.adapters[0])
    mcp = MCPServer(
        name=SERVER_NAME,
        title="Robot Motion MCP (read-only + controlled execution)",
        description=(
            "Query and controlled-execution tools over ship subassembly welding "
            "motion-planning assets. Planning runs happen only inside isolated per-run "
            "worktree copies; upstream files are hash-verified untouched. No robot "
            "execution, no constraint relaxation."
        ),
        version=__version__,
    )

    @mcp.tool(
        name="list_tasks",
        description=(
            "List tasks that actually exist in the connected read-only sources "
            "(New_Design placement seeds, frozen research-audit tasks, labeled MOCK fixtures). "
            "No task ids are invented."
        ),
        structured_output=True,
    )
    def list_tasks(include_mock: bool = True) -> TaskListOutput:
        tasks = registry.list_tasks(include_mock=include_mock)
        return TaskListOutput(
            ok=True,
            server=SERVER_NAME,
            server_version=__version__,
            task_count=len(tasks),
            sources=[SourceInfo(**s) for s in registry.describe_sources()],
            tasks=[TaskSummary(**t) for t in tasks],
        )

    @mcp.tool(
        name="get_task_info",
        description=(
            "Read one task's configuration: workpieces, placement/allocation, robots, planner "
            "methods, collision-semantics flags (verbatim from config), start/end data sources "
            "and data completeness. task_id forms: 'ND-SEED-<seed>', frozen research task ids "
            "(e.g. 'T1_CM2B'), or 'MOCK-*' fixtures."
        ),
        structured_output=True,
    )
    def get_task_info(task_id: str) -> TaskInfoOutput:
        try:
            info, source = registry.get_task_info(task_id)
        except AdapterError as exc:
            raise ToolError(str(exc)) from exc
        warnings = list(info.get("warnings") or [])
        if info.get("source", {}).get("source_id") == "NEW_DESIGN_SNAPSHOT":
            warnings.append(
                "collision_semantics.current_parameter_config reports the snapshot's own flags; "
                "planning success there does not certify full 3D collision-free motion"
            )
        return TaskInfoOutput(
            ok=True,
            task_id=task_id,
            source_id=info.get("source", {}).get("source_id", "unknown"),
            mock=bool(info.get("mock", False)),
            kind=info.get("kind", "unknown"),
            info=info,
            warnings=warnings,
        )

    @mcp.tool(
        name="get_experiment_report",
        description=(
            "Read a frozen experiment/run report with honest status semantics: planner raw "
            "status + canonical state, candidate-path facts, independent-validation state "
            "(PASSED/FAILED/INCOMPLETE/NOT_VALIDATED/TOOL_ERROR), durations, error class, "
            "caveats and source-file references. run_id forms: '<experiment>/<record>' "
            "(e.g. 'rm_next_009_full_motion_generation_cost/T1_CM2B_P0', "
            "'third_workpiece_process_boundary/EXPERIMENT', '_audit/FINAL_QA'), 'ND-SEED-<seed>', "
            "'MOCK-*'. Timeouts are never reported as infeasibility."
        ),
        structured_output=True,
    )
    def get_experiment_report(run_id: str) -> ExperimentReportOutput:
        try:
            report, _source = registry.find_experiment_report(run_id)
        except AdapterError as exc:
            raise ToolError(str(exc)) from exc
        return ExperimentReportOutput(
            ok=True,
            run_id=run_id,
            mock=bool(report.get("mock", False)),
            report=report,
        )

    @mcp.tool(
        name="start_motion_plan",
        description=(
            "Start ONE real offline planning run for a New_Design seed task "
            "(task_id 'ND-SEED-<seed>') inside an isolated per-run worktree copy; the "
            "upstream project is never written (pre/post SHA-256 manifests attached). "
            "planner: whitelist " + str(list(PLANNER_WHITELIST)) + " (applied to the "
            "worktree config). time_budget_seconds: "
            f"{MIN_BUDGET_S:.0f}-{MAX_BUDGET_S:.0f} (default {DEFAULT_BUDGET_S:.0f}); "
            "on expiry the process tree is killed and status becomes TIMEOUT (never "
            "'no path exists'). request_id (optional, [\\w.-]{1,64}) makes retries "
            "idempotent. Returns immediately with a run_id; poll get_plan_status. "
            "Only one real run at a time."
        ),
        structured_output=True,
    )
    def start_motion_plan(
        task_id: str,
        planner: str = "Lazy_PRM",
        time_budget_seconds: float = DEFAULT_BUDGET_S,
        request_id: str | None = None,
    ) -> StartPlanOutput:
        if not isinstance(task_id, str) or not _TASK_ID_RE.match(task_id):
            raise ToolError(
                f"invalid task_id {task_id!r}; expected 'ND-SEED-<seed>' (digits only). "
                "Shell metacharacters and paths are not accepted."
            )
        try:
            result = run_manager.start(
                task_id=task_id,
                planner=planner,
                time_budget_seconds=time_budget_seconds,
                request_id=request_id,
            )
        except RunError as exc:
            raise ToolError(str(exc)) from exc
        return StartPlanOutput(
            ok=True,
            run_id=result["run_id"],
            accepted=result["accepted"],
            duplicate=result.get("duplicate", False),
            execution_status=result.get("execution_status", "UNKNOWN"),
            task_id=result.get("task_id", task_id),
            seed=result.get("seed"),
            planner=result.get("planner"),
            budget_s=result.get("budget_s"),
            message=result.get("message", ""),
        )

    @mcp.tool(
        name="get_plan_status",
        description=(
            "Query a real planning run (run_id from start_motion_plan) with three "
            "separate dimensions: execution_status (PENDING/RUNNING/COMPLETED/FAILED/"
            "TIMEOUT/UNKNOWN - COMPLETED means the tool finished, not that a path was "
            "found), plan_result (PATH_FOUND/NO_CANDIDATE_WITHIN_BUDGET/ERROR/"
            "NOT_AVAILABLE) and validation_status. Status survives service restarts "
            "via persistent records; unverifiable outcomes are reported as UNKNOWN, "
            "never silently converted."
        ),
        structured_output=True,
    )
    def get_plan_status(run_id: str) -> PlanStatusOutput:
        try:
            status = run_manager.status(run_id)
        except RunError as exc:
            raise ToolError(str(exc)) from exc
        validation = status.get("validation") or {}
        warnings = []
        if status.get("recovered"):
            warnings.append(
                "status was reconciled after a service restart; exit code unknown, "
                "recovered from the output artifact"
            )
        if status.get("execution_status") == "TIMEOUT":
            warnings.append("timeout is not a proof of infeasibility")
        integrity = status.get("upstream_integrity") or {}
        if integrity.get("verified") is False:
            warnings.append(
                "upstream integrity diff is non-empty: "
                + ", ".join(integrity.get("diff", {}).get("changed", [])[:5])
            )
        if integrity.get("verified") is None and status.get("execution_status") in {
            "COMPLETED",
            "FAILED",
            "TIMEOUT",
        }:
            warnings.append("upstream post-run integrity check did not complete")
        return PlanStatusOutput(
            ok=True,
            run_id=run_id,
            task_id=status.get("task_id"),
            execution_status=status.get("execution_status", "UNKNOWN"),
            plan_result=status.get("plan_result"),
            validation_status=validation.get("validation_status") or "NOT_RUN",
            details={k: v for k, v in status.items() if k != "validation"},
            warnings=warnings,
        )

    @mcp.tool(
        name="validate_motion",
        description=(
            "Report the independent-validation status bound to a run's candidate path. "
            "For NEW local runs this is NOT_RUN until a scene-compatible strict "
            "validator is registered - planning success is never treated as PASS, and "
            "historical verdicts are never transferred to new paths. For frozen "
            "research-audit records the recorded verdict is reported verbatim, bound to "
            "the recorded path sha256 and input contract. run_id forms: local 'R-*' "
            "runs and historical records like 'rm_next_015_fr69b_transfer_expansion/"
            "Q1_RRT_E1' or '.../REF_T1_CM2B_P0'."
        ),
        structured_output=True,
    )
    def validate_motion(run_id: str) -> ValidateMotionOutput:
        audit = registry.adapters[1]
        try:
            result = validate_run(run_id, run_manager, audit)
        except (ValidateError, RunError) as exc:
            raise ToolError(str(exc)) from exc
        return ValidateMotionOutput(
            ok=True,
            run_id=run_id,
            task_id=result.get("task_id"),
            source=result.get("source"),
            candidate_path_hash=result.get("candidate_path_hash"),
            validation_status=result.get("validation_status", "ERROR"),
            validation_method=result.get("validation_method"),
            model_version=result.get("model_version") or {},
            evidence_reference=result.get("evidence_reference") or [],
            error_or_incomplete_reason=result.get("error_or_incomplete_reason"),
            details=result.get("details") or {},
        )

    return mcp


def main() -> None:
    mcp = build_server()
    mcp.run()  # stdio transport by default


if __name__ == "__main__":
    main()
