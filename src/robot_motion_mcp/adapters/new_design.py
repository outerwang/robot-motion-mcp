"""Read-only adapter over the configured New_Design pipeline snapshot.

The source root is configured through ``ROBOT_MOTION_NEW_DESIGN_ROOT``.

This adapter NEVER executes the pipeline. It only reads existing JSON snapshots and
reports them verbatim, including the honest collision-semantics flags (the current
configuration ignores workpiece collisions and disables same-arm/cross-arm checks),
so a consumer cannot mistake a planning success for a validated collision-free motion.
"""

from __future__ import annotations

from pathlib import Path

from ..config import JsonCache, WorkspacePaths
from ..status import map_raw_status
from .base import RunNotFound, TaskAdapter, TaskNotFound

SOURCE_ID = "NEW_DESIGN_SNAPSHOT"

_TASK_PREFIX = "ND-SEED-"

# Parameter files whose presence the task-info completeness check reports on.
_REQUIRED_SNAPSHOT_FILES = (
    "Parameter_Config/Get_All_Roads.json",
    "Parameter_Config/Lazy_PRM_Config.json",
    "Data_Config/Random_XZL.json",
    "Data_Config/Allocation_Summary.json",
    "Data_Config/All_Roads.json",
    "Data_Config/Welding_Sequence.json",
    "Data_Config/Collection_of_Sampling_Points.json",
)


class NewDesignAdapter(TaskAdapter):
    source_id = SOURCE_ID
    description = (
        "New_Design 4-arm ship subassembly welding pipeline snapshots "
        "(placement seeds, allocation, Lazy_PRM/EIT* roads, TSP sequence, simulation)"
    )

    def __init__(self, paths: WorkspacePaths):
        super().__init__(paths)
        self._cache = JsonCache()
        self._root = paths.new_design_root

    # -- availability -------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._root.is_dir() and (self._root / "Data_Config" / "All_Roads.json").is_file()

    def _file(self, rel: str) -> Path:
        return self._root / rel

    def _load(self, rel: str):
        return self._cache.load(self._file(rel))

    # -- helpers ------------------------------------------------------------

    def _placement_seeds(self) -> list[int]:
        data = self._load("Data_Config/Random_XZL.json") or {}
        return sorted(int(p.get("seed")) for p in data.get("placements", []) if p.get("seed") is not None)

    def _summary(self):
        return self._load("Data_Config/Allocation_Summary.json")

    def _summary_entry(self, seed: int):
        summary = self._summary()
        if not summary:
            return None
        for image in summary.get("images", []):
            if int(image.get("seed", -1)) == seed:
                return image
        return None

    def _roads(self):
        return self._load("Data_Config/All_Roads.json")

    def _weld_parts(self) -> list[dict]:
        collection = self._load("Data_Config/Collection_of_Sampling_Points.json") or {}
        parts = []
        for part_id, welds in collection.items():
            parts.append({"part_id": part_id, "weld_count": len(welds)})
        parts.sort(key=lambda p: p["part_id"])
        return parts

    # -- task listing -------------------------------------------------------

    def list_tasks(self) -> list[dict]:
        if not self.available:
            return []
        seeds: set[int] = set()
        seeds.update(self._placement_seeds())
        summary = self._summary()
        for image in (summary or {}).get("images", []):
            if image.get("seed") is not None:
                seeds.add(int(image["seed"]))
        roads = self._roads()
        if roads and roads.get("seed") is not None:
            seeds.add(int(roads["seed"]))
        executed_seed = int(roads["seed"]) if roads and roads.get("seed") is not None else None
        tasks = []
        for seed in sorted(seeds):
            summary_entry = self._summary_entry(seed)
            is_executed = executed_seed is not None and seed == executed_seed
            tasks.append(
                {
                    "task_id": f"{_TASK_PREFIX}{seed}",
                    "source_id": self.source_id,
                    "mock": False,
                    "kind": "new_design_seed_task",
                    "summary": (
                        f"New_Design placement seed {seed}"
                        + ("; executed planning snapshot available (All_Roads.json)" if is_executed else "")
                        + ("; allocation summary available" if summary_entry else "")
                    ),
                }
            )
        return tasks

    # -- task info ----------------------------------------------------------

    def get_task_info(self, task_id: str) -> dict:
        if not task_id.startswith(_TASK_PREFIX):
            raise TaskNotFound(f"{self.source_id}: task_id {task_id!r} does not use the {_TASK_PREFIX}<seed> form")
        seed_part = task_id[len(_TASK_PREFIX) :]
        try:
            seed = int(seed_part)
        except ValueError as exc:
            raise TaskNotFound(f"{self.source_id}: seed must be an integer, got {seed_part!r}") from exc

        config = self._load("Parameter_Config/Get_All_Roads.json") or {}
        lazy_cfg = self._load("Parameter_Config/Lazy_PRM_Config.json") or {}
        roads = self._roads()
        roads_seed = int(roads["seed"]) if roads and roads.get("seed") is not None else None
        executed = roads is not None and roads_seed == seed

        warnings: list[str] = []
        if not executed:
            warnings.append(
                f"All_Roads.json holds the executed run for seed {roads_seed}, not seed {seed}; "
                "robot starts/paths below are omitted. No planning was run by this server."
            )

        summary_entry = self._summary_entry(seed)
        summary = self._summary()
        if summary and summary_entry is None:
            warnings.append(
                f"seed {seed} has no entry in the current Allocation_Summary.json batch "
                f"(best_seed={summary.get('best_seed')!r}); allocation data below is omitted"
            )

        # Known inter-file generation gap in the snapshot (surfaced, never hidden):
        if summary and roads_seed is not None and summary.get("best_seed") is not None:
            warnings.append(
                "snapshot note: Allocation_Summary.json (best_seed=%s) and All_Roads.json "
                "(seed=%s) come from different pipeline generations; do not mix numbers across them"
                % (summary.get("best_seed"), roads_seed)
            )

        info: dict = {
            "task_id": task_id,
            "kind": "new_design_seed_task",
            "source": {
                "source_id": self.source_id,
                "root": str(self._root),
                "read_only": True,
                "origin": "Configured New_Design pipeline snapshot",
            },
            "workpieces": {"part_count": len(self._weld_parts()), "parts": self._weld_parts()},
            "placement": self._placement_info(seed),
            "allocation": self._allocation_info(seed),
            "robots": self._robots_info(roads if executed else None),
            "planner": {
                "default_algorithm": config.get("algorithm"),
                "available_algorithms": ["EIT", "EIT_Optimize", "Lazy_PRM"],
                "lazy_prm_config": {
                    k: lazy_cfg.get(k)
                    for k in ("n_samples", "k_neighbors", "max_conn_dist", "lazy", "shortcut", "refine_on_fail", "seed")
                },
                "invoked_by_this_server": False,
            },
            "collision_semantics": self._collision_semantics(config, roads if executed else None),
            "start_end": self._start_end_info(roads if executed else None),
            "data_completeness": self._completeness(executed, roads_seed, seed),
            "warnings": warnings,
        }
        return info

    def _placement_info(self, seed: int) -> dict:
        data = self._load("Data_Config/Random_XZL.json") or {}
        for placement in data.get("placements", []):
            if int(placement.get("seed", -1)) == seed:
                poses = placement.get("poses", {})
                return {
                    "recorded": True,
                    "seed": seed,
                    "n_parts_placed": len(poses),
                    "parts": sorted(poses.keys()),
                    "excluded_blocks": placement.get("excluded_blocks", []),
                    "units_note": "pose x/y/yaw_deg/z in metres / degrees as defined by Random_XZL.json",
                }
        return {"recorded": False, "seed": seed}

    def _allocation_info(self, seed: int) -> dict:
        entry = self._summary_entry(seed)
        if entry is None:
            return {"recorded_for_seed": False, "seed": seed}
        robots = {}
        for rid, welds in (entry.get("robots") or {}).items():
            robots[rid] = {
                "weld_count": len(welds),
                "weld_ids": [w.get("weld_id") for w in welds],
            }
        return {
            "recorded_for_seed": True,
            "seed": seed,
            "makespan_min": entry.get("makespan"),
            "robots": robots,
            "unreachable_welds": entry.get("unreachable_welds", []),
            "walls_drawn": bool(entry.get("walls")),
        }

    @staticmethod
    def _robots_info(roads: dict | None) -> dict:
        if not roads:
            return {"count": 4, "joint_dim": None, "arm_ids": ["R1", "R2", "R3", "R4"], "executed_roads": None}
        robots_out = {}
        for rid, rdata in roads.get("robots", {}).items():
            paths = rdata.get("paths", {})
            n_pairs = sum(len(v) for v in paths.values())
            n_found = sum(
                1 for v in paths.values() for p in v.values() if p.get("found")
            )
            robots_out[rid] = {
                "weld_count": len(rdata.get("weld_ids", [])),
                "node_count": len(rdata.get("nodes", [])),
                "path_pairs": n_pairs,
                "paths_found": n_found,
                "start_source": rdata.get("start_source"),
                "start_q": rdata.get("start_q"),
            }
        return {
            "count": len(roads.get("robots", {})),
            "joint_dim": len(next(iter(roads.get("robots", {}).values()), {}).get("start_q", [])) or None,
            "arm_ids": sorted(roads.get("robots", {}).keys()),
            "executed_roads": robots_out,
        }

    @staticmethod
    def _collision_semantics(config: dict, roads: dict | None) -> dict:
        semantics = {
            "current_parameter_config": {
                "ignore_workpiece_collision": config.get("ignore_workpiece_collision"),
                "check_same_arm_self": config.get("check_same_arm_self"),
                "check_cross_arm": config.get("check_cross_arm"),
                "wall_analysis": config.get("wall_analysis"),
                "tool_offset": config.get("tool_offset"),
                "planning_node_offset": config.get("planning_node_offset"),
            },
            "executed_run_scene": None,
            "interpretation": (
                "Current configuration reports workpiece collisions as ignorable and disables "
                "same-arm self-collision and cross-arm checks (walls only). A planning success "
                "in this system does NOT certify a full 3D collision-free robot motion."
            ),
        }
        if roads:
            obstacles = roads.get("obstacles", {})
            semantics["executed_run_scene"] = {
                "seed": roads.get("seed"),
                "algorithm": roads.get("algorithm"),
                "tool_offset": roads.get("tool_offset"),
                "wall_count": len(obstacles.get("walls", {})),
                "workpiece_instance_count": len(obstacles.get("blocks", [])),
                "unsolved_welds": roads.get("unsolved_welds", []),
                "wall_analysis_red_parts": (roads.get("wall_analysis") or {}).get("red_parts"),
                "wall_analysis_blue_parts": (roads.get("wall_analysis") or {}).get("blue_parts"),
            }
        return semantics

    @staticmethod
    def _start_end_info(roads: dict | None) -> dict:
        if not roads:
            return {
                "task_level": "per-weld endpoints live in All_Roads.json welds[<id>].start/.end (local) and .start_world/.end_world (metres); robot start joints live in robots[<R>].start_q",
                "executed_road_nodes": None,
            }
        nodes = {}
        for rid, rdata in roads.get("robots", {}).items():
            nodes[rid] = [
                {"id": n.get("id"), "type": n.get("type")} for n in rdata.get("nodes", [])
            ]
        return {
            "task_level": "per-weld endpoints live in All_Roads.json welds[<id>].start/.end (local) and .start_world/.end_world (metres); robot start joints live in robots[<R>].start_q",
            "executed_road_nodes": nodes,
        }

    def _completeness(self, executed: bool, roads_seed, seed: int) -> dict:
        present = {rel: self._file(rel).is_file() for rel in _REQUIRED_SNAPSHOT_FILES}
        sim_summary = self._file(f"Output/Simulation/seed_{seed:03d}_summary.json")
        summary_entry = self._summary_entry(seed)
        return {
            "parameter_configs_present": present,
            "roads_executed_for_seed": executed,
            "roads_seed": roads_seed,
            "allocation_summary_entry_present": summary_entry is not None,
            "sequence_for_seed": bool(executed and self._load("Data_Config/Welding_Sequence.json")),
            "simulation_summary_file": str(sim_summary) if sim_summary.is_file() else None,
            "all_required_files_present": all(present.values()),
        }

    # -- experiment report --------------------------------------------------

    def find_experiment_report(self, run_id: str) -> dict | None:
        if not run_id.startswith(_TASK_PREFIX):
            return None
        seed_part = run_id[len(_TASK_PREFIX) :]
        try:
            seed = int(seed_part)
        except ValueError as exc:
            raise RunNotFound(f"{self.source_id}: seed must be an integer, got {seed_part!r}") from exc
        if not self.available:
            raise RunNotFound(f"{self.source_id}: snapshot root not available")
        if seed not in {int(t["task_id"][len(_TASK_PREFIX) :]) for t in self.list_tasks()}:
            raise RunNotFound(f"{self.source_id}: no snapshot data for seed {seed}")

        source_files: list[str] = []
        roads = self._roads()
        executed = bool(roads) and int(roads.get("seed", -1)) == seed

        pipeline: dict = {"roads": None, "sequence": None, "simulation": None}
        if executed:
            pipeline["roads"] = self._roads_summary(roads)
            source_files.append("Data_Config/All_Roads.json")
            sequence = self._load("Data_Config/Welding_Sequence.json")
            if sequence and int(sequence.get("seed", -1)) == seed:
                pipeline["sequence"] = {
                    rid: {"method": r.get("method"), "min_time_s": r.get("min_time_s")}
                    for rid, r in sequence.get("robots", {}).items()
                }
                source_files.append("Data_Config/Welding_Sequence.json")
        sim = self._load(f"Output/Simulation/seed_{seed:03d}_summary.json")
        if sim:
            pipeline["simulation"] = sim
            source_files.append(f"Output/Simulation/seed_{seed:03d}_summary.json")

        report = {
            "run_id": run_id,
            "report_type": "new_design_snapshot_run",
            "task_id": f"{_TASK_PREFIX}{seed}",
            "planner": {"default_algorithm": (self._load("Parameter_Config/Get_All_Roads.json") or {}).get("algorithm")},
            "model_version": {
                "identity": "New_Design 4-arm URDF snapshot (see 03_ARTIFACT_VERSION_FAMILY.md)",
                "note": "no per-run model hash exists in the New_Design snapshot files",
            },
            "planner_execution": {
                "raw_status": "completed" if executed else "no_executed_run_for_seed",
                "state": map_raw_status("completed" if executed else None).planner_state,
                "attempted": executed,
            },
            "executed_pipeline": pipeline,
            "independent_validation": None,
            "upstream_audit_evidence": self._audit_evidence(seed),
            "caveats": [
                "This is a snapshot report assembled from existing files; the server did not run the pipeline.",
                "The executed New_Design configuration does not certify full 3D collision-free motion "
                "(workpiece collisions ignored, self/cross-arm checks off).",
                "Timing values come from the pipeline's own time model, not from a real robot.",
            ],
            "source_files": source_files,
        }
        return report

    @staticmethod
    def _roads_summary(roads: dict) -> dict:
        per_robot = {}
        for rid, rdata in roads.get("robots", {}).items():
            paths = rdata.get("paths", {})
            per_robot[rid] = {
                "path_pairs": sum(len(v) for v in paths.values()),
                "paths_found": sum(1 for v in paths.values() for p in v.values() if p.get("found")),
            }
        return {
            "seed": roads.get("seed"),
            "algorithm": roads.get("algorithm"),
            "weld_count": len(roads.get("welds", {})),
            "unsolved_welds": roads.get("unsolved_welds", []),
            "per_robot": per_robot,
        }

    def _audit_evidence(self, seed: int) -> dict | None:
        """Attach configured research-audit replay evidence when it targets this seed."""
        audit_root = self.paths.audit_root
        if not audit_root.is_dir():
            return None
        evidence: dict = {"audit_root": str(audit_root), "items": []}
        witness_path = audit_root / "results" / "wall_collision_witness.json"
        dense_path = audit_root / "results" / "final_transfer_dense_check.json"
        if dense_path.is_file():
            dense = self._cache.load(dense_path) or {}
            if int(dense.get("seed", -1)) == seed:
                evidence["items"].append(
                    {
                        "evidence": "final_transfer_dense_check",
                        "path": self.rel_path(audit_root, dense_path),
                        "scope": dense.get("scope"),
                        "selected_transfer_paths": dense.get("selected_transfer_paths"),
                        "checked_states": dense.get("checked_states"),
                        "invalid_transfer_paths": dense.get("invalid_transfer_paths"),
                        "invalid_states": dense.get("invalid_states"),
                    }
                )
        if witness_path.is_file():
            witness = self._cache.load(witness_path) or {}
            evidence["items"].append(
                {
                    "evidence": "wall_collision_witness",
                    "path": self.rel_path(audit_root, witness_path),
                    "scope": witness.get("evidence_scope"),
                    "witness_count": len(witness.get("witnesses", [])),
                }
            )
        final_qa = self._cache.load(audit_root / "FINAL_QA.json")
        if final_qa:
            evidence["gates"] = {
                "execution_gate": final_qa.get("execution_gate"),
                "scientific_gate": final_qa.get("scientific_gate"),
                "joint_order_mismatch_count": final_qa.get("joint_order_mismatch_count"),
                "wall_counterexample_repeated": final_qa.get("wall_counterexample_repeated"),
            }
        timing = self._cache.load(audit_root / "results" / "native_timing_summary.json")
        if timing:
            evidence["native_timing_summary"] = {
                k: timing.get(k) for k in ("n", "mean_s", "median_s", "min_s", "max_s", "std_sample_s")
            }
        if not evidence["items"] and "gates" not in evidence:
            return None
        evidence["note"] = (
            "Evidence produced by the 2026-10-14 takeover audit against the snapshot of that date; "
            "this server does not re-verify file hashes."
        )
        return evidence
