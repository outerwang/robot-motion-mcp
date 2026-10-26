"""Task registry: merges the read-only adapters behind one dispatch surface."""

from __future__ import annotations

from .adapters.base import AdapterError, TaskAdapter
from .adapters.mock import MockAdapter
from .adapters.new_design import NewDesignAdapter
from .adapters.research_audit import ResearchAuditAdapter
from .config import WorkspacePaths


class Registry:
    def __init__(self, paths: WorkspacePaths):
        self.paths = paths
        self.adapters: list[TaskAdapter] = [
            NewDesignAdapter(paths),
            ResearchAuditAdapter(paths),
            MockAdapter(paths),
        ]

    # -- listing ------------------------------------------------------------

    def describe_sources(self) -> list[dict]:
        return [
            {
                "source_id": a.source_id,
                "available": a.available,
                "root": str(getattr(a, "_root", "")) or None,
                "read_only": a.read_only,
                "mock": a.source_id == "MOCK",
                "description": a.description,
            }
            for a in self.adapters
        ]

    def list_tasks(self, include_mock: bool = True) -> list[dict]:
        tasks: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for adapter in self.adapters:
            if adapter.source_id == "MOCK" and not include_mock:
                continue
            for task in adapter.list_tasks():
                key = (task["source_id"], task["task_id"])
                if key in seen:
                    continue
                seen.add(key)
                tasks.append(task)
        return tasks

    # -- dispatch -----------------------------------------------------------

    def get_task_info(self, task_id: str) -> tuple[dict, dict]:
        """Return (info, source_description) or raise TaskNotFound."""
        errors: list[str] = []
        for adapter in self.adapters:
            try:
                info = adapter.get_task_info(task_id)
            except AdapterError as exc:
                errors.append(f"{adapter.source_id}: {exc}")
                continue
            if info is not None:
                return info, self._source_of(adapter)
        hint = "; ".join(errors) if errors else "no adapter handled the id"
        examples = self._example_task_ids()
        raise AdapterError(
            f"unknown task_id {task_id!r}. Known prefixes: ND-SEED-<seed> (New_Design seeds), "
            f"frozen research task ids (e.g. T1_CM2B, T2_FR69B, Q1, RM-NEXT-017), MOCK-* "
            f"(protocol fixtures). Examples: {examples}. Detail: {hint}"
        )

    def find_experiment_report(self, run_id: str) -> tuple[dict, dict]:
        """Return (report, source_description) or raise RunNotFound."""
        errors: list[str] = []
        for adapter in self.adapters:
            try:
                report = adapter.find_experiment_report(run_id)
            except AdapterError as exc:
                errors.append(f"{adapter.source_id}: {exc}")
                continue
            if report is not None:
                return report, self._source_of(adapter)
        hint = "; ".join(errors) if errors else "no adapter handled the id"
        examples = self._example_run_ids()
        raise AdapterError(
            f"unknown run_id {run_id!r}. Forms: '<experiment>/<record>' (e.g. "
            f"rm_next_009_full_motion_generation_cost/T1_CM2B_P0, "
            f"third_workpiece_process_boundary/EXPERIMENT, _audit/FINAL_QA), 'ND-SEED-<seed>', "
            f"'MOCK-...'. Bare stems resolve only when unique. Examples: {examples}. Detail: {hint}"
        )

    # -- helpers ------------------------------------------------------------

    def _source_of(self, adapter: TaskAdapter) -> dict:
        return {
            "source_id": adapter.source_id,
            "available": adapter.available,
            "read_only": adapter.read_only,
        }

    def _example_task_ids(self) -> str:
        ids = [t["task_id"] for t in self.list_tasks()][:6]
        return ", ".join(ids)

    def _example_run_ids(self) -> str:
        index = self.adapters[1]._build_index()  # research audit owns run records
        mock = ["MOCK-DEMO-RUN-001"]
        nd = ["ND-SEED-46"]
        from_audit = sorted(index)[:3]
        return ", ".join(nd + from_audit + mock)
