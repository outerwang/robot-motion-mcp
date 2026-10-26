"""Per-run isolated working copy of the upstream New_Design project.

Design (Phase 2A decision): the upstream program resolves every input/output path
relative to ``PROJECT_ROOT`` (derived from the module file's own location), so the
only safe way to run it without touching upstream files is to execute a *copy* of
the project. Each run gets ``runs/<run_id>/worktree/``:

* real copy of code, configs, inputs and the seed's wall URDF (~90 MB, seconds);
* excluded: historical outputs, backups, debug workspaces, caches;
* the run's own ``Parameter_Config/Get_All_Roads.json`` inside the copy is then
  adjusted (seed / planner) - editing our copy is allowed, editing upstream is not.

Verified upstream write inventory (scripts/audit_side_effects.py): all writes use
PROJECT_ROOT-relative paths, so nothing can escape the worktree.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..config import WorkspacePaths
from .integrity import EXCLUDED_DIR_NAMES

# Top-level exclusions: historical outputs / archives / debug workspaces.
_EXCLUDED_TOP = {"_backup_archive_2026-08-23", "New_Design_Test", "work", "logs"}
# Output/ is excluded except the requested seed's walls folder (roads reads it).
_OUTPUT_KEEP = "Arrange_The_Working_Area"

ALGORITHM_WHITELIST = ("Lazy_PRM",)


class WorktreeError(Exception):
    pass


def build_worktree(
    paths: WorkspacePaths,
    run_dir: Path,
    seed: int,
    algorithm: str = "Lazy_PRM",
    log=None,
) -> Path:
    """Copy New_Design into run_dir/worktree and apply run-specific config overrides."""
    if algorithm not in ALGORITHM_WHITELIST:
        raise WorktreeError(
            f"planner {algorithm!r} is not whitelisted; available: {list(ALGORITHM_WHITELIST)} "
            "(selection is applied to the worktree copy of Parameter_Config/Get_All_Roads.json)"
        )
    src = paths.new_design_root
    worktree = run_dir / "worktree"
    if worktree.exists():
        raise WorktreeError(f"worktree already exists: {worktree}")

    def _log(msg: str) -> None:
        if log:
            log(msg)

    _log(f"building isolated worktree at {worktree} (copy of {src})")
    for entry in src.iterdir():
        name = entry.name
        if name in _EXCLUDED_TOP or name in EXCLUDED_DIR_NAMES or name.startswith("_"):
            continue
        if entry.is_dir() and name == "Output":
            _copy_walls_only(entry, worktree / "Output", seed, _log)
        elif entry.is_dir():
            shutil.copytree(
                entry,
                worktree / name,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        else:
            shutil.copy2(entry, worktree / name)

    _apply_run_config(worktree, seed, algorithm, _log)
    _remove_step_outputs(worktree, _log)
    _log("worktree ready")
    return worktree


def _remove_step_outputs(worktree: Path, log) -> None:
    """Delete files the roads step will (re)write, so a failed/timed-out run can
    never be confused with the copied historical output."""
    stale = worktree / "Data_Config" / "All_Roads.json"
    if stale.is_file():
        stale.unlink()
        log(
            "removed copied historical Data_Config/All_Roads.json from worktree "
            "(this run must produce its own output)"
        )


def _copy_walls_only(output_src: Path, output_dst: Path, seed: int, log) -> None:
    walls_src = output_src / _OUTPUT_KEEP / f"seed_{seed:02d}"
    if not walls_src.is_dir():
        raise WorktreeError(
            f"walls for seed {seed} not found upstream ({walls_src}); "
            "the allocate step must have run before roads; this server does not run allocate"
        )
    shutil.copytree(walls_src, output_dst / _OUTPUT_KEEP / f"seed_{seed:02d}")
    log(f"copied walls input: Output/{_OUTPUT_KEEP}/seed_{seed:02d}")


def _apply_run_config(worktree: Path, seed: int, algorithm: str, log) -> None:
    cfg_path = worktree / "Parameter_Config" / "Get_All_Roads.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    applied = {}
    if cfg.get("seed") != seed:
        cfg["seed"] = seed
        applied["seed"] = seed
    if cfg.get("algorithm") != algorithm:
        cfg["algorithm"] = algorithm
        applied["algorithm"] = algorithm
    if applied:
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"worktree config overrides applied: {applied} (worktree copy only)")
