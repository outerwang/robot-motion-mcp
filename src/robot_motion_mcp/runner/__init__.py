"""Controlled real-planner execution layer (Phase 2).

Components:
* ``integrity`` - SHA-256 manifests proving upstream trees are untouched
* ``worktree``  - per-run isolated copy of the upstream New_Design project
* ``manager``   - persistent run records, subprocess lifecycle, restart recovery
* ``validate``  - strictly bound validation status (historical evidence or explicit
  NOT_RUN; never upgrades unvalidated candidates)

The upstream project is NEVER written by this package: every run executes inside
``runs/<run_id>/worktree/`` and pre/post manifests guard the upstream tree.
"""

from .manager import (
    EXECUTION_STATES,
    PLAN_RESULT_STATES,
    RUN_VALIDATION_STATES,
    RunManager,
    RunNotFoundError,
)

__all__ = [
    "EXECUTION_STATES",
    "PLAN_RESULT_STATES",
    "RUN_VALIDATION_STATES",
    "RunManager",
    "RunNotFoundError",
]
