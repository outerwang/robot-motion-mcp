"""Read-only adapter over configured frozen research-audit run records.

Source root is configured through ``ROBOT_MOTION_AUDIT_ROOT``. Every report field is
traced to a real file; nothing is synthesized. Statuses are translated through
:mod:`..status` while the raw value is always preserved.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import JsonCache, WorkspacePaths
from ..status import map_raw_status
from .base import AmbiguousRunId, RunNotFound, TaskAdapter, TaskNotFound

SOURCE_ID = "RESEARCH_AUDIT"

# (experiment_dir, subdir_or_None, glob) for per-run record files.
_RUN_GLOBS: tuple[tuple[str, str | None, str], ...] = (
    ("rm_next_009_full_motion_generation_cost", "results", "*.json"),
    ("rm_next_011_rrt_validation_baseline_and_manufacturing_reuse", "results", "*.json"),
    ("rm_next_011_rrt_validation_baseline_and_manufacturing_reuse", "resume_segment/results", "*.json"),
    ("rm_next_015_fr69b_transfer_expansion", "paths", "*.json"),
    ("rm_next_012_014_multiweld_planning_study", "results", "*.json"),
    ("fr69b_full_transfer_feasibility", "results", "*.json"),
    ("validated_baseline", "runs", "*/receipt.json"),
)

# (experiment_dir, relative_file) treated as experiment-level summaries.
_EXPERIMENT_FILES: tuple[tuple[str, str], ...] = (
    ("rm_next_015_fr69b_transfer_expansion", "results/FINAL_FACTS.json"),
    ("rm_next_016_persistent_lazyprm_multiquery_pilot", "results/FINAL_FACTS.json"),
    ("rm_next_017_official_ompl_strict_checker_adapter", "results/FINAL_FACTS.json"),
    ("fr69b_full_transfer_feasibility", "FINAL_STATUS.json"),
    ("cross_workpiece_reuse_gate_20261016", "FINAL_STATUS.json"),
    ("third_workpiece_process_boundary", "FINAL_STATUS.json"),
)

_ROOT_FILES: tuple[str, ...] = (
    "FINAL_QA.json",
    "results/wall_collision_witness.json",
    "results/final_transfer_dense_check.json",
    "results/native_timing_summary.json",
)


def _trim(value, depth: int = 0):
    """Keep structure but bound payload size; every trimmed spot stays explicit."""
    if isinstance(value, dict):
        if depth >= 4:
            return "{...}"
        return {k: _trim(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        if depth >= 4:
            return "[...]"
        if len(value) > 6:
            return [_trim(v, depth + 1) for v in value[:2]] + [f"...{len(value) - 2} more"]
        return [_trim(v, depth + 1) for v in value]
    if isinstance(value, str) and len(value) > 300:
        return value[:297] + "..."
    return value


class ResearchAuditAdapter(TaskAdapter):
    source_id = SOURCE_ID
    description = (
        "Frozen planning and validation run records from the configured audit root: "
        "experiments, FR69B/FR76A/CM2B task evidence, takeover-audit results"
    )

    def __init__(self, paths: WorkspacePaths):
        super().__init__(paths)
        self._root = paths.audit_root
        self._cache = JsonCache()
        self._index: dict[str, Path] | None = None  # run_id -> file
        self._bare_index: dict[str, list[str]] | None = None

    # -- availability -------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._root.is_dir() and (self._root / "FINAL_QA.json").is_file()

    def _build_index(self) -> dict[str, Path]:
        if self._index is not None:
            return self._index
        index: dict[str, Path] = {}
        for experiment, subdir, pattern in _RUN_GLOBS:
            base = self._root / experiment / subdir
            for path in sorted(base.glob(pattern)) if base.is_dir() else []:
                run_id = f"{experiment}/{path.stem}"
                if run_id in index:
                    # same stem from different subdirectories (e.g. resumed reruns):
                    # qualify with the parent directory instead of silently overwriting
                    run_id = f"{experiment}/{path.parent.name}.{path.stem}"
                index[run_id] = path
        for experiment, rel in _EXPERIMENT_FILES:
            path = self._root / experiment / rel
            if path.is_file():
                index[f"{experiment}/EXPERIMENT"] = path
        for rel in _ROOT_FILES:
            path = self._root / rel
            if path.is_file():
                index[f"_audit/{path.stem}"] = path
        self._index = index
        # stem -> run_ids at FILE level: a bare stem is ambiguous whenever two
        # record files share it, even after subdirectory qualification
        self._bare_index = {}
        for run_id, path in index.items():
            self._bare_index.setdefault(path.stem, []).append(run_id)
        return index

    # -- task listing -------------------------------------------------------

    def list_tasks(self) -> list[dict]:
        if not self.available:
            return []
        index = self._build_index()
        tasks: dict[str, dict] = {}
        for run_id, path in index.items():
            data = self._cache.load(path)
            if not isinstance(data, dict):
                continue
            task_id = data.get("task")
            if not task_id or isinstance(task_id, (int, float)):
                continue
            entry = tasks.setdefault(
                str(task_id),
                {
                    "task_id": str(task_id),
                    "source_id": self.source_id,
                    "mock": False,
                    "kind": "research_audit_frozen_task",
                    "summary": "frozen research-audit task (see get_task_info for run records)",
                    "runs": [],
                },
            )
            entry["runs"].append(run_id)
        out = []
        for task_id in sorted(tasks):
            entry = tasks[task_id]
            entry["summary"] = (
                f"frozen research-audit task with {len(entry['runs'])} run record(s); "
                f"run_ids under {entry['runs'][0].split('/')[0]}/"
            )
            entry.pop("runs")
            out.append(entry)
        return out

    # -- task info ----------------------------------------------------------

    def get_task_info(self, task_id: str) -> dict:
        index = self._build_index()
        matching_runs: list[tuple[str, Path, dict]] = []
        for run_id, path in index.items():
            data = self._cache.load(path)
            if isinstance(data, dict) and str(data.get("task", "")) == task_id:
                matching_runs.append((run_id, path, data))
        if not matching_runs:
            raise TaskNotFound(f"{self.source_id}: no frozen task {task_id!r}")

        part = None
        input_contract = None
        runs = []
        validation_refs = []
        for run_id, path, data in sorted(matching_runs):
            semantics = map_raw_status(data.get("status"))
            binding = data.get("runtime_scene_binding") or {}
            if part is None and binding.get("part"):
                part = binding.get("part")
            if input_contract is None and data.get("input_contract_sha256"):
                input_contract = data.get("input_contract_sha256")
            is_ref = str(data.get("method", "")) == "REFERENCE" or run_id.split("/")[-1].startswith("REF_")
            if is_ref:
                validation_refs.append(
                    {
                        "run_id": run_id,
                        "candidate_ref": data.get("candidate_id"),
                        "raw_status": data.get("status"),
                        "passed": data.get("passed"),
                        "validation_state": semantics.validation_state,
                    }
                )
            else:
                runs.append(
                    {
                        "run_id": run_id,
                        "method": data.get("method"),
                        "raw_status": data.get("status"),
                        "planner_state": semantics.planner_state,
                        "candidate_found": data.get("path_sha256") is not None,
                        "error_class": semantics.error_class,
                    }
                )
        return {
            "task_id": task_id,
            "kind": "research_audit_frozen_task",
            "source": {
                "source_id": self.source_id,
                "root": str(self._root),
                "read_only": True,
                "origin": "Configured frozen research-audit records",
            },
            "part_identity": part,
            "model_version": {
                "input_contract_sha256": input_contract,
                "note": "model identity per experiment; see e.g. 03_ARTIFACT_VERSION_FAMILY.md",
            },
            "planner": {
                "methods_seen_in_records": sorted({r["method"] for r in runs if r.get("method")}),
                "invoked_by_this_server": False,
            },
            "candidate_runs": runs,
            "independent_validation_records": validation_refs,
            "endpoint_definition_sources": [
                "rm_next_009_full_motion_generation_cost/config/FROZEN_TASKS.json",
                "rm_next_012_014_multiweld_planning_study/01_FROZEN_MULTI_WELD_TASKS.csv",
                "rm_next_015_fr69b_transfer_expansion/04_FR69B_QUALIFIED_TASK_MATRIX.csv",
            ],
            "warnings": [
                "Endpoint/qualification details live in the referenced CSV/JSON files; "
                "this server reports what the run records state, no more.",
            ],
        }

    # -- experiment report --------------------------------------------------

    def find_experiment_report(self, run_id: str) -> dict | None:
        index = self._build_index()
        resolved: str | None = None
        if run_id in index:
            resolved = run_id
        elif "/" not in run_id:
            candidates = self._bare_index.get(run_id, [])
            if len(candidates) == 1:
                resolved = candidates[0]
            elif len(candidates) > 1:
                raise AmbiguousRunId(
                    f"run_id {run_id!r} is ambiguous; use one of: {sorted(candidates)}"
                )
        if resolved is None:
            return None

        path = index[resolved]
        data = self._cache.load(path)
        if data is None:
            raise RunNotFound(f"{self.source_id}: record file unreadable: {path}")
        rel = self.rel_path(self._root, path)
        experiment = resolved.split("/", 1)[0]
        stem = resolved.split("/", 1)[1]

        if experiment == "_audit":
            return self._root_report(stem, data, rel)
        if stem == "EXPERIMENT":
            return self._experiment_report(experiment, data, rel)
        if str(data.get("method", "")) == "REFERENCE" or stem.startswith("REF_"):
            return self._validation_record_report(resolved, experiment, data, rel)
        if path.parent.name == "runs" and path.name == "receipt.json":
            return self._receipt_report(resolved, data, rel)
        if data.get("q_path") is not None:
            return self._path_artifact_report(resolved, experiment, data, rel)
        return self._candidate_run_report(resolved, experiment, data, rel, index)

    # -- report builders ----------------------------------------------------

    def _base(self, run_id: str, report_type: str, source_files: list[str]) -> dict:
        return {
            "run_id": run_id,
            "report_type": report_type,
            "mock": False,
            "source": {
                "source_id": self.source_id,
                "root": str(self._root),
                "read_only": True,
            },
            "source_files": source_files,
        }

    def _candidate_run_report(self, run_id, experiment, data, rel, index) -> dict:
        semantics = map_raw_status(data.get("status"))
        ref_entry = None
        for other_id, other_path in index.items():
            if other_id.split("/", 1)[0] != experiment:
                continue
            other = self._cache.load(other_path)
            if isinstance(other, dict) and other.get("candidate_id") == data.get("id"):
                ref_entry = {
                    "record_run_id": other_id,
                    "raw_status": other.get("status"),
                    "passed": other.get("passed"),
                    "validation_state": map_raw_status(other.get("status")).validation_state,
                }
                break
        report = self._base(run_id, "candidate_planner_run", [rel])
        report.update(
            {
                "task_id": data.get("task"),
                "planner": {
                    "method": data.get("method"),
                    "seed": data.get("seed"),
                    "search_invocations": data.get("path_search_invocations"),
                },
                "model_version": {
                    "input_contract_sha256": data.get("input_contract_sha256"),
                    "runtime_scene_sha256": data.get("runtime_scene_sha256"),
                    "static_qualification": _trim(data.get("static_qualification")),
                    "part_identity": (data.get("runtime_scene_binding") or {}).get("part"),
                },
                "planner_execution": {
                    "raw_status": data.get("status"),
                    "planner_state": semantics.planner_state,
                    "attempted": semantics.attempted,
                    "candidate_found": semantics.candidate_found,
                    "error_class": semantics.error_class,
                    "durations": {
                        "child_wall_s_before_final_write": data.get("child_wall_s_before_final_write"),
                        "state_s_inclusive": data.get("state_s_inclusive"),
                        "first_raw_path_at_s": data.get("first_raw_path_at_s"),
                        "candidate_validated_at_s": data.get("candidate_validated_at_s"),
                    },
                    "counts": {
                        "state_calls": data.get("state_calls"),
                        "state_status_counts": data.get("state_status_counts"),
                        "endpoint_checks": len(data.get("endpoint_checks") or []),
                    },
                },
                "candidate_path": {
                    "found": bool(data.get("path_sha256") or data.get("path_file")),
                    "path_sha256": data.get("path_sha256"),
                    "path_points": data.get("path_points"),
                    "normalized_joint_length": data.get("normalized_joint_length"),
                    "path_file": data.get("path_file"),
                    "continuous_certificate": data.get("continuous_certificate"),
                    "production_safety": data.get("production_safety"),
                },
                "independent_validation": ref_entry
                or {
                    "record_run_id": None,
                    "raw_status": None,
                    "passed": None,
                    "validation_state": map_raw_status(None).validation_state,
                },
                "caveats": list(semantics.caveats)
                + [
                    "production_safety=false and continuous_certificate=false are taken verbatim from the record",
                    "the server did not run any planner; this is a read-only replay of a frozen record",
                ],
                "raw_record": _trim(data),
            }
        )
        return report

    def _validation_record_report(self, run_id, experiment, data, rel) -> dict:
        semantics = map_raw_status(data.get("status"))
        report = self._base(run_id, "independent_validation_record", [rel])
        report.update(
            {
                "task_id": data.get("task"),
                "validated_candidate": {
                    "record_run_id": f"{experiment}/{data.get('candidate_id')}",
                    "candidate_path_sha256": data.get("candidate_path_sha256"),
                },
                "validation": {
                    "raw_status": data.get("status"),
                    "passed": data.get("passed"),
                    "validation_state": semantics.validation_state,
                    "scope": data.get("reference_independence"),
                    "reference_states": data.get("reference_states"),
                    "segments": _trim(data.get("segments")),
                    "reason": data.get("reason"),
                    "budget_phase": data.get("budget_phase"),
                },
                "error_class": semantics.error_class,
                "caveats": list(semantics.caveats)
                + [
                    "reference_independence.full_independent_geometric_truth=false means the "
                    "record itself does not claim full independent geometric ground truth",
                ],
                "raw_record": _trim(data),
            }
        )
        return report

    def _path_artifact_report(self, run_id, experiment, data, rel) -> dict:
        facts = self._cache.load(self._root / experiment / "results" / "FINAL_FACTS.json") or {}
        q_path = data.get("q_path") or []
        report = self._base(run_id, "candidate_path_artifact", [rel])
        report.update(
            {
                "task_id": data.get("task"),
                "model_version": {"input_contract_sha256": data.get("input_contract_sha256")},
                "candidate_path": {
                    "found": bool(q_path),
                    "point_count": len(q_path),
                    "joint_dim": len(q_path[0]) if q_path else None,
                    "full_candidate_validation": data.get("full_candidate_validation"),
                    "continuous_certificate": data.get("continuous_certificate"),
                },
                "experiment_facts": {
                    k: facts.get(k)
                    for k in (
                        "Q1_candidate_status",
                        "Q1_verdict",
                        "reference_status",
                        "reference_pass",
                        "Q1_reference_verdict",
                        "path_sha256",
                        "fine_required_states",
                        "candidate_and_reference_cold_s",
                    )
                    if k in facts
                },
                "caveats": [
                    "full q_path values stay in the artifact file; this report only counts them",
                    "full_candidate_validation refers to the candidate-phase validation semantics of the experiment",
                ],
            }
        )
        return report

    def _experiment_report(self, experiment, data, rel) -> dict:
        status_like = {
            k: data.get(k)
            for k in ("status", "execution_status", "research_decision", "execution", "qa_passed")
            if k in data
        }
        primary_raw = data.get("status") or data.get("execution_status") or data.get("research_decision")
        semantics = map_raw_status(primary_raw) if isinstance(primary_raw, str) else map_raw_status(None)
        report = self._base(f"{experiment}/EXPERIMENT", "experiment_summary", [rel])
        report.update(
            {
                "task_id": data.get("task") or experiment,
                "status_fields": status_like,
                "planner_execution": {
                    "raw_status": primary_raw,
                    "planner_state": semantics.planner_state,
                    "attempted": semantics.attempted,
                    "error_class": semantics.error_class,
                },
                "independent_validation": {
                    "summary_fields": {
                        k: data.get(k)
                        for k in (
                            "reference_passed_paths",
                            "reference_states",
                            "all_eight_flows_reference_passed",
                            "full_transfer_reference",
                            "retreat_prefix_reference",
                            "endpoint_gate",
                            "A_LazyPRM",
                            "A_RRTConnect",
                            "B_planners",
                            "Q1_reference_verdict",
                            "reference_status",
                            "reference_pass",
                        )
                        if k in data
                    }
                },
                "decision_fields": {
                    k: data.get(k)
                    for k in (
                        "research_decision",
                        "new_algorithm_development",
                        "production_safety",
                        "continuous_certificate",
                        "qa_passed",
                        "second_numerical_workpiece_case_established",
                        "qualified_scenario",
                        "multiquery_pilot_verdict",
                        "mature_PRM_verdict",
                        "new_algorithm_verdict",
                        "next_single_task",
                    )
                    if k in data
                },
                "caveats": list(semantics.caveats)
                + [
                    "experiment summaries speak for whole experiments; per-run records have their own run_ids",
                ],
                "raw_record": _trim(data),
            }
        )
        return report

    def _receipt_report(self, run_id, data, rel) -> dict:
        semantics = map_raw_status(data.get("status"))
        report = self._base(run_id, "baseline_run_receipt", [rel])
        report.update(
            {
                "task_id": data.get("task") or data.get("note"),
                "planner_execution": {
                    "raw_status": data.get("status"),
                    "planner_state": semantics.planner_state,
                    "attempted": semantics.attempted,
                    "error_class": semantics.error_class,
                    "durations": {"wall_s": data.get("wall_s")},
                },
                "caveats": ["execution-level receipt; carries no geometry claim"],
                "raw_record": _trim(data),
            }
        )
        return report

    def _root_report(self, stem, data, rel) -> dict:
        report = self._base(f"_audit/{stem}", "audit_root_evidence", [rel])
        if stem == "FINAL_QA":
            report.update(
                {
                    "report_type": "audit_quality_gates",
                    "gates": {
                        "execution_gate": data.get("execution_gate"),
                        "scientific_gate": data.get("scientific_gate"),
                    },
                    "planner_execution": {
                        "raw_status": data.get("scientific_gate"),
                        "planner_state": "UNKNOWN",
                        "attempted": False,
                        "error_class": None,
                    },
                    "caveats": [
                        "scientific_gate FAIL records the wall-collision counterexample and joint time "
                        "model mismatch found by the takeover audit; reproduction alone is not safety",
                    ],
                }
            )
        elif stem == "wall_collision_witness":
            report.update(
                {
                    "report_type": "collision_counterexample",
                    "validation": {
                        "scope": data.get("evidence_scope"),
                        "witness_count": len(data.get("witnesses", [])),
                    },
                    "caveats": ["counterexample evidence: repeated native checker collisions on a saved return path"],
                }
            )
        elif stem == "final_transfer_dense_check":
            report.update(
                {
                    "report_type": "dense_recheck",
                    "validation": {
                        "scope": data.get("scope"),
                        "seed": data.get("seed"),
                        "selected_transfer_paths": data.get("selected_transfer_paths"),
                        "checked_states": data.get("checked_states"),
                        "invalid_transfer_paths": data.get("invalid_transfer_paths"),
                        "invalid_states": data.get("invalid_states"),
                    },
                    "caveats": [
                        "dense recheck with the configured wall-only checker; not a continuous certificate",
                    ],
                }
            )
        else:
            report.update({"raw_record": _trim(data)})
        return report
