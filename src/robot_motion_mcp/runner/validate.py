"""Strictly bound validation status for candidate paths (validate_motion backend).

v1 semantics (Phase 2 decision, documented in docs/PHASE2_TECHNICAL_AUDIT.md):

* For NEW local runs (RunManager): a completed planner execution only proves a
  candidate was produced. No independent strict validator is currently registered
  for the New_Design 4-arm scene (the frozen FCL/OMPL reference harnesses in
  _research_audit are bound to their own frozen input contracts and scenes), so
  the honest answer is NOT_RUN with the binding data a future validator needs.
  Historical reference verdicts are NEVER transferred onto new paths.

* For HISTORICAL research-audit records: the verdict recorded in the frozen
  evidence files is reported verbatim (PASS / INCOMPLETE / FAIL / NOT_RUN), bound
  to the recorded candidate path sha256 and input contract. The record speaks for
  itself; this module never recomputes or re-judges it.
"""

from __future__ import annotations

from pathlib import Path

from ..adapters.base import AdapterError
from ..adapters.research_audit import ResearchAuditAdapter
from ..runner.integrity import file_sha256
from ..runner.manager import RunManager, RunNotFoundError

# Historical record bindings: (experiment, record_stem) -> validation summary keys
_HIST_PASS_STATUSES = {"SUCCESS_REFERENCE_GRID", "FREE_REFERENCE_GRID", "Q1_REFERENCE_PASS"}


class ValidateError(Exception):
    pass


def validate_run(run_id: str, manager: RunManager, audit: ResearchAuditAdapter) -> dict:
    # 1) local runs
    try:
        status = manager.status(run_id)
        return _validate_local(status, manager)
    except RunNotFoundError:
        pass

    # 2) historical research-audit records
    try:
        report = audit.find_experiment_report(run_id)
    except AdapterError as exc:
        raise ValidateError(str(exc)) from exc
    if report is None:
        raise ValidateError(f"unknown run_id {run_id!r} in any run store")
    return _validate_historical(run_id, audit, report)


def _validate_local(status: dict, manager: RunManager) -> dict:
    run_id = status["run_id"]
    base = {
        "run_id": run_id,
        "task_id": status.get("task_id"),
        "mock": False,
        "source": "LOCAL_RUN",
        "candidate_path_hash": status.get("result_sha256"),
        "validation_method": None,
        "model_version": {
            "input_versions": status.get("input_versions"),
            "worktree_config_sha256": status.get("worktree_config_sha256"),
        },
        "evidence_reference": [status.get("result_file")] if status.get("result_file") else [],
        "details": {},
    }

    execution = status.get("execution_status")
    if execution != "COMPLETED":
        base.update(
            {
                "validation_status": "NOT_RUN",
                "error_or_incomplete_reason": (
                    f"execution_status is {execution}; a validation target (candidate "
                    "path) only exists after a completed planner run"
                ),
            }
        )
        return base
    if status.get("plan_result") != "PATH_FOUND":
        base.update(
            {
                "validation_status": "NOT_RUN",
                "error_or_incomplete_reason": (
                    f"plan_result is {status.get('plan_result')}; there is no candidate "
                    "path to validate. Timeout is not a proof of infeasibility."
                ),
            }
        )
        return base

    existing = status.get("validation")
    if existing:
        return existing

    not_run = dict(base)
    not_run.update(
        {
            "validation_status": "NOT_RUN",
            "error_or_incomplete_reason": (
                "No independent strict validator is registered for the New_Design 4-arm "
                "scene. The frozen FCL/OMPL reference harnesses in _research_audit bind to "
                "their own frozen input contracts (different scenes/models), so they cannot "
                "validate this new path. Planning success does NOT imply collision-free "
                "motion; the collision flags of this system (walls only) remain in effect."
            ),
            "details": {
                "next_step": "register and qualify a scene-compatible independent validator "
                "(new experiment per research governance) before any PASS can be claimed",
                "planner_system_collision_flags": (
                    "workpiece collisions ignored; same-arm and cross-arm checks disabled"
                ),
            },
        }
    )
    manager.set_validation(run_id, not_run)
    return not_run


def _validate_historical(run_id: str, audit: ResearchAuditAdapter, report: dict) -> dict:
    report_type = report.get("report_type")
    source_files = list(report.get("source_files") or [])
    resolved = source_files[0] if source_files else None
    file_sha = None
    if resolved:
        absolute = audit._root / resolved  # noqa: SLF001 - same package boundary
        if absolute.is_file():
            file_sha = file_sha256(absolute)

    base = {
        "run_id": run_id,
        "task_id": report.get("task_id"),
        "mock": False,
        "source": "HISTORICAL_RESEARCH_AUDIT",
        "candidate_path_hash": None,
        "validation_method": "historical frozen independent reference (verbatim from record)",
        "model_version": {"note": "bound to the record's own input contract; see source files"},
        "evidence_reference": source_files,
        "details": {},
    }

    def record_sha() -> str | None:
        raw = report.get("candidate_path") or {}
        if raw.get("path_sha256"):
            return raw["path_sha256"]
        facts = report.get("experiment_facts") or {}
        return facts.get("path_sha256")

    if report_type == "independent_validation_record":
        validation = report.get("validation") or {}
        passed = validation.get("passed")
        raw_status = validation.get("raw_status")
        if passed is True or raw_status in _HIST_PASS_STATUSES:
            status = "PASS"
        elif passed is False and raw_status == "UNVERIFIED_SUCCESS":
            status = "INCOMPLETE"
        else:
            status = "FAIL" if passed is False else "ERROR"
        base.update(
            {
                "validation_status": status,
                "candidate_path_hash": report.get("validated_candidate", {}).get(
                    "candidate_path_sha256"
                ),
                "error_or_incomplete_reason": validation.get("reason"),
                "details": {
                    "raw_status": raw_status,
                    "recorded_sha256_semantics": "sha256 recorded by the original experiment",
                    "scope": validation.get("scope"),
                    "artifact_file_sha256_now": file_sha,
                },
            }
        )
        return base

    if report_type == "candidate_path_artifact":
        facts = report.get("experiment_facts") or {}
        verdict = facts.get("Q1_reference_verdict") or facts.get("reference_status")
        if verdict in _HIST_PASS_STATUSES:
            status = "PASS"
        elif verdict:
            status = "INCOMPLETE"
        else:
            status = "NOT_RUN"
        base.update(
            {
                "validation_status": status,
                "candidate_path_hash": record_sha(),
                "error_or_incomplete_reason": None if status == "PASS" else f"reference verdict: {verdict}",
                "details": {
                    "recorded_verdict": verdict,
                    "recorded_sha256_semantics": "sha256 recorded by the original experiment",
                    "artifact_file_sha256_now": file_sha,
                },
            }
        )
        return base

    if report_type == "candidate_planner_run":
        independent = report.get("independent_validation") or {}
        raw_status = independent.get("raw_status")
        passed = independent.get("passed")
        if passed is True or (raw_status or "") in _HIST_PASS_STATUSES:
            status = "PASS"
        elif passed is False:
            status = "INCOMPLETE" if raw_status == "UNVERIFIED_SUCCESS" else "FAIL"
        else:
            status = "NOT_RUN"
        base.update(
            {
                "validation_status": status,
                "candidate_path_hash": (report.get("candidate_path") or {}).get("path_sha256"),
                "error_or_incomplete_reason": None if status == "PASS" else f"reference record: {raw_status}",
                "details": {"reference_record_run_id": independent.get("record_run_id")},
            }
        )
        return base

    # experiment summaries / audit root evidence: report recorded verdict fields
    summary_fields = (report.get("independent_validation") or {}).get("summary_fields") or {}
    pass_keys = [
        k
        for k, v in summary_fields.items()
        if v is True or (isinstance(v, str) and v in _HIST_PASS_STATUSES)
    ]
    negative = summary_fields.get("full_transfer_reference")
    if pass_keys:
        status = "PASS"
        reason = f"recorded passing fields: {pass_keys} (scope limited to the record)"
    elif negative == "NO_COMPLETE_CANDIDATE":
        status = "NOT_RUN"
        reason = "record states no complete candidate existed to validate"
    else:
        status = "NOT_RUN"
        reason = "record contains no full independent validation verdict"
    base.update(
        {
            "validation_status": status,
            "candidate_path_hash": record_sha(),
            "error_or_incomplete_reason": reason,
            "details": {"summary_fields": summary_fields},
        }
    )
    return base
