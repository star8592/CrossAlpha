from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from crossalpha.observatory.live_health import observatory_live_health  # noqa: E402
from crossalpha.storage.indexes import manifest_lock  # noqa: E402


def _copy_tree(source: Path, destination: Path) -> None:
    if source.exists():
        shutil.copytree(source, destination, dirs_exist_ok=True)


def _freeze_state(data_root: Path, frozen_root: Path) -> None:
    source_manifests = data_root / "manifests"
    destination_manifests = frozen_root / "manifests"
    destination_manifests.mkdir(parents=True, exist_ok=True)
    with manifest_lock(data_root):
        audit = source_manifests / "raw_snapshots.jsonl"
        if audit.exists():
            shutil.copy2(audit, destination_manifests / "raw_snapshots.jsonl")
        _copy_tree(source_manifests / "series", destination_manifests / "series")


def _diff(expected: Any, actual: Any, path: str = "$", *, limit: int = 50) -> list[str]:
    mismatches: list[str] = []

    def walk(left: Any, right: Any, current: str) -> None:
        if len(mismatches) >= limit:
            return
        if isinstance(left, bool) or isinstance(right, bool):
            if left != right or type(left) is not type(right):
                mismatches.append(f"{current}: python={left!r} rust={right!r}")
            return
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            if abs(float(left) - float(right)) > 1e-9:
                mismatches.append(f"{current}: python={left!r} rust={right!r}")
            return
        if isinstance(left, dict) and isinstance(right, dict):
            left_keys = set(left)
            right_keys = set(right)
            for key in sorted(left_keys - right_keys):
                mismatches.append(f"{current}.{key}: missing from Rust")
                if len(mismatches) >= limit:
                    return
            for key in sorted(right_keys - left_keys):
                mismatches.append(f"{current}.{key}: unexpected in Rust")
                if len(mismatches) >= limit:
                    return
            for key in sorted(left_keys & right_keys):
                walk(left[key], right[key], f"{current}.{key}")
            return
        if isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                mismatches.append(
                    f"{current}: list length python={len(left)} rust={len(right)}"
                )
                return
            for index, (left_item, right_item) in enumerate(zip(left, right, strict=True)):
                walk(left_item, right_item, f"{current}[{index}]")
            return
        if left != right or type(left) is not type(right):
            mismatches.append(f"{current}: python={left!r} rust={right!r}")

    walk(expected, actual, path)
    return mismatches


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare Python and Rust O(1) Observatory live health on frozen state."
    )
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument(
        "--rust-binary",
        type=Path,
        default=REPO_ROOT / "target" / "debug" / "crossalpha-rs",
    )
    parser.add_argument("--stale-after", type=int, default=900)
    parser.add_argument("--no-verify-latest", action="store_true")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    verify_latest = not args.no_verify_latest

    with tempfile.TemporaryDirectory(prefix="crossalpha-live-health-parity-") as tmp:
        frozen_root = Path(tmp)
        _freeze_state(args.data_root, frozen_root)

        python_report = observatory_live_health(
            frozen_root,
            stale_after_seconds=args.stale_after,
            verify_latest=verify_latest,
            now=now,
        )

        command = [
            str(args.rust_binary),
            "observatory-live-health",
            str(frozen_root),
            "--stale-after",
            str(args.stale_after),
            "--now",
            now.isoformat(),
            "--no-write-report",
        ]
        if not verify_latest:
            command.append("--no-verify-latest")

        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if not completed.stdout.strip():
            print("ok=false mismatches=1")
            print(f"mismatch=Rust live health produced no JSON; stderr={completed.stderr.strip()}")
            return 1

        try:
            rust_report = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            print("ok=false mismatches=1")
            print(f"mismatch=invalid Rust JSON: {exc}")
            print(completed.stdout)
            return 1

    mismatches = _diff(python_report, rust_report)
    print(
        "ok={} records={} mismatches={} python_ok={} rust_ok={} mode={}".format(
            str(not mismatches).lower(),
            python_report.get("manifest_records"),
            len(mismatches),
            str(bool(python_report.get("ok"))).lower(),
            str(bool(rust_report.get("ok"))).lower(),
            python_report.get("mode"),
        )
    )
    for mismatch in mismatches:
        print(f"mismatch={mismatch}")

    if mismatches:
        if completed.stderr.strip():
            print(f"rust_stderr={completed.stderr.strip()}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
