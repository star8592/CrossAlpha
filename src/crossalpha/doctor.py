from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def storage_report(data_root: Path, repo_root: Path | None = None) -> dict[str, Any]:
    data_root = data_root.expanduser().resolve()
    repo_root = (repo_root or Path.cwd()).expanduser().resolve()

    data_root.mkdir(parents=True, exist_ok=True)
    for name in ("raw", "canonical", "derived", "manifests", "research", "archive"):
        (data_root / name).mkdir(parents=True, exist_ok=True)

    usage = shutil.disk_usage(data_root)
    writable = os.access(data_root, os.W_OK)
    inside_repo = _is_relative_to(data_root, repo_root)

    warnings: list[str] = []
    if inside_repo:
        warnings.append("data root is inside the Git working tree")
    if not writable:
        warnings.append("data root is not writable")
    if usage.free / usage.total < 0.10:
        warnings.append("filesystem has less than 10% free space")

    return {
        "ok": writable and not inside_repo,
        "repo_root": str(repo_root),
        "data_root": str(data_root),
        "writable": writable,
        "inside_repo": inside_repo,
        "total_tb": round(usage.total / 10**12, 3),
        "used_tb": round(usage.used / 10**12, 3),
        "free_tb": round(usage.free / 10**12, 3),
        "free_percent": round(usage.free / usage.total * 100, 2),
        "warnings": warnings,
    }
