"""Base contract for read-only task/run adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..config import WorkspacePaths


class AdapterError(Exception):
    """Base class for adapter domain errors (never leaks raw tracebacks to MCP)."""


class TaskNotFound(AdapterError):
    pass


class RunNotFound(AdapterError):
    pass


class AmbiguousRunId(AdapterError):
    pass


class TaskAdapter(ABC):
    """A read-only view over one upstream asset root."""

    source_id: str = "abstract"
    description: str = ""
    read_only: bool = True

    def __init__(self, paths: WorkspacePaths):
        self.paths = paths

    @property
    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def list_tasks(self) -> list[dict]:
        """Return task summaries: {task_id, source_id, mock, kind, summary}."""

    @abstractmethod
    def get_task_info(self, task_id: str) -> dict:
        """Return the full task info payload for one task (raises TaskNotFound)."""

    @abstractmethod
    def find_experiment_report(self, run_id: str) -> dict | None:
        """Return a report payload for run_id, or None if this adapter does not own it.

        Raises AmbiguousRunId when a bare id matches several records.
        """

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def rel_path(root: Path, target: Path) -> str:
        try:
            return target.relative_to(root).as_posix()
        except ValueError:
            return str(target)
