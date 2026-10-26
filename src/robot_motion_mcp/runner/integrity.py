"""SHA-256 integrity manifests for directory trees (upstream protection evidence)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

_CHUNK = 1 << 20

# Directories never copied into worktrees and never expected to change upstream.
EXCLUDED_DIR_NAMES = {"__pycache__", ".idea", ".vscode"}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def tree_manifest(root: Path) -> dict:
    """Hash every file under root (excluded dirs skipped). Returns {relpath: sha256}."""
    if not root.is_dir():
        raise FileNotFoundError(f"not a directory: {root}")
    manifest: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIR_NAMES]
        for name in filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            manifest[rel] = file_sha256(path)
    return manifest


def diff_manifests(before: dict, after: dict) -> dict:
    """Compare two manifests; returns changed/added/removed relative paths."""
    before_keys = set(before)
    after_keys = set(after)
    return {
        "changed": sorted(k for k in before_keys & after_keys if before[k] != after[k]),
        "added": sorted(after_keys - before_keys),
        "removed": sorted(before_keys - after_keys),
    }
