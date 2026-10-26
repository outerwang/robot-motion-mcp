"""Read-only workspace roots resolution.

Defaults are derived from this file's location (the project folder lives inside the
research workspace). Every default can be overridden by environment variables so the
server can be pointed at other checkouts without code changes.

Nothing in this package ever writes to the configured roots.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

ENV_WORKSPACE = "ROBOT_MOTION_WORKSPACE"
ENV_NEW_DESIGN_ROOT = "ROBOT_MOTION_NEW_DESIGN_ROOT"
ENV_AUDIT_ROOT = "ROBOT_MOTION_AUDIT_ROOT"
ENV_RUNS_ROOT = "ROBOT_MOTION_RUNS_ROOT"
ENV_PLANNER_PYTHON = "ROBOT_MOTION_PLANNER_PYTHON"

# src/robot_motion_mcp/config.py -> parents[2] is the repository root when
# installed in editable mode.
_DEFAULT_WORKSPACE = Path(__file__).resolve().parents[2]

# By default, use the interpreter that launched this server. Set
# ROBOT_MOTION_PLANNER_PYTHON when the planner needs a separate environment.
_DEFAULT_PLANNER_PYTHON = Path(sys.executable)


def _root_from_env(env_name: str, default: Path) -> Path:
    value = os.environ.get(env_name)
    return Path(value) if value else default


@dataclass(frozen=True)
class WorkspacePaths:
    """Absolute locations of the planning inputs and local run records."""

    workspace: Path
    new_design_root: Path
    audit_root: Path
    runs_root: Path
    planner_python: Path

    @classmethod
    def from_env(cls) -> "WorkspacePaths":
        workspace = _root_from_env(ENV_WORKSPACE, _DEFAULT_WORKSPACE)
        return cls(
            workspace=workspace,
            new_design_root=_root_from_env(
                ENV_NEW_DESIGN_ROOT, workspace / "New_Design"
            ),
            audit_root=_root_from_env(ENV_AUDIT_ROOT, workspace / "research_audit"),
            runs_root=_root_from_env(
                ENV_RUNS_ROOT,
                Path(__file__).resolve().parents[2] / "runs",
            ),
            planner_python=_root_from_env(ENV_PLANNER_PYTHON, _DEFAULT_PLANNER_PYTHON),
        )

    def describe(self) -> list[dict]:
        return [
            {
                "name": "workspace",
                "path": str(self.workspace),
                "exists": self.workspace.is_dir(),
            },
            {
                "name": "new_design_root",
                "path": str(self.new_design_root),
                "exists": self.new_design_root.is_dir(),
            },
            {
                "name": "audit_root",
                "path": str(self.audit_root),
                "exists": self.audit_root.is_dir(),
            },
            {
                "name": "runs_root",
                "path": str(self.runs_root),
                "exists": self.runs_root.is_dir(),
            },
            {
                "name": "planner_python",
                "path": str(self.planner_python),
                "exists": self.planner_python.is_file(),
            },
        ]


@dataclass
class JsonCache:
    """Per-process cache for the (large) read-only JSON snapshots."""

    cache: dict = field(default_factory=dict)

    def load(self, path: Path):
        key = str(path)
        if key not in self.cache:
            if not path.is_file():
                self.cache[key] = None
            else:
                import json

                self.cache[key] = json.loads(path.read_text(encoding="utf-8"))
        return self.cache[key]
