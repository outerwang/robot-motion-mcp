"""Explicitly labeled mock adapter for protocol-level tests ONLY.

Nothing here represents a real task, a real planner run or real evidence. Every
payload carries ``mock=true`` and ``data_origin="MOCK_PROTOCOL_TEST_ONLY"`` so a
consumer can never confuse it with the real adapters. Real experiment results are
NEVER fabricated here.
"""

from __future__ import annotations

from .base import RunNotFound, TaskAdapter, TaskNotFound

SOURCE_ID = "MOCK"

_MOCK_TASKS: dict[str, dict] = {
    "MOCK-ALPHA-01": {
        "kind": "mock_seed_task",
        "workpieces": {"part_count": 2, "parts": [{"part_id": "MOCK-PART-A", "weld_count": 3}]},
        "robots": {"count": 1, "arm_ids": ["MR1"]},
        "planner": {"default_algorithm": "mock-straight-line", "invoked_by_this_server": False},
        "collision_semantics": {
            "note": "mock flags; no geometric meaning",
            "ignore_workpiece_collision": False,
            "check_same_arm_self": True,
            "check_cross_arm": True,
        },
    },
    "MOCK-BETA-02": {
        "kind": "mock_seed_task",
        "workpieces": {"part_count": 1, "parts": [{"part_id": "MOCK-PART-B", "weld_count": 1}]},
        "robots": {"count": 2, "arm_ids": ["MR1", "MR2"]},
        "planner": {"default_algorithm": "mock-straight-line", "invoked_by_this_server": False},
        "collision_semantics": {"note": "mock flags; no geometric meaning"},
    },
}

_MOCK_REPORTS: dict[str, dict] = {
    "MOCK-DEMO-RUN-001": {
        "task_id": "MOCK-ALPHA-01",
        "planner": {"method": "mock-linear-interpolation", "note": "NOT a real planner"},
        "planner_execution": {"raw_status": "MOCK_COMPLETED", "state": "COMPLETED", "attempted": True},
        "candidate_path": {"found": True, "path_sha256": "0" * 64, "point_count": 5},
        "independent_validation": {"raw_status": "MOCK_PASSED", "passed": True, "state": "PASSED"},
        "durations": {"mock_wall_s": 0.001},
    },
}


class MockAdapter(TaskAdapter):
    source_id = SOURCE_ID
    description = "protocol-test fixtures, explicitly labeled; never real planning data"
    read_only = True

    def __init__(self, paths):
        super().__init__(paths)  # paths unused; mock has no external roots

    @property
    def available(self) -> bool:
        return True

    def list_tasks(self) -> list[dict]:
        return [
            {
                "task_id": task_id,
                "source_id": self.source_id,
                "mock": True,
                "kind": info["kind"],
                "summary": "MOCK fixture for MCP protocol tests only; not a real task",
            }
            for task_id, info in sorted(_MOCK_TASKS.items())
        ]

    def get_task_info(self, task_id: str) -> dict:
        if task_id not in _MOCK_TASKS:
            raise TaskNotFound(f"{self.source_id}: unknown mock task {task_id!r}")
        info = dict(_MOCK_TASKS[task_id])
        info.update(
            {
                "task_id": task_id,
                "source": {
                    "source_id": self.source_id,
                    "root": None,
                    "read_only": True,
                    "origin": "this server's test fixtures",
                },
                "mock": True,
                "data_origin": "MOCK_PROTOCOL_TEST_ONLY",
                "mock_notice": "This task is a test fixture. It does not exist in any upstream system.",
            }
        )
        return info

    def find_experiment_report(self, run_id: str) -> dict | None:
        if run_id not in _MOCK_REPORTS:
            if run_id.startswith("MOCK-"):
                raise RunNotFound(f"{self.source_id}: unknown mock run {run_id!r}")
            return None
        report = dict(_MOCK_REPORTS[run_id])
        report.update(
            {
                "run_id": run_id,
                "report_type": "mock_protocol_fixture",
                "mock": True,
                "data_origin": "MOCK_PROTOCOL_TEST_ONLY",
                "source": {"source_id": self.source_id, "root": None, "read_only": True},
                "source_files": [],
                "mock_notice": (
                    "All values in this report are static test fixtures for MCP protocol "
                    "testing. They are not measurements and not planning results."
                ),
                "caveats": [
                    "mock report; never cite as experimental evidence",
                ],
            }
        )
        return report
