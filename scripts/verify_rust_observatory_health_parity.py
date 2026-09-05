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

from crossalpha.observatory.health import observatory_health  # noqa: E402
from crossalpha.storage.indexes import manifest_lock  # noqa: E402


def _freeze_audit_ledger(data_root: Path, frozen_root: Path) -> None:
    source = data_root / "manifests" / "raw_snapshots.jsonl"
    if not source.exists():
        raise FileNotFoundError(f"audit manifest missing: {source}")
    destination = frozen_root / "manifests" / "raw_snapshots.jsonl"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with manifest_lock(data_root):
        shutil.copy2(source, destination)


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
        description="Compare Python and Rust Observatory health on one frozen audit ledger."
    )
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument(
        "--rust-binary",
        type=Path,
        default=REPO_ROOT / "target" / "debug" / "crossalpha-rs",
    )
    parser.add_argument("--expected-interval", type=int, default=300)
    parser.add_argument("--stale-after", type=int, default=900)
    parser.add_argument("--no-verify-latest", action="store_true")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    verify_latest = not args.no_verify_latest

    with tempfile.TemporaryDirectory(prefix="crossalpha-health-parity-") as tmp:
        frozen_root = Path(tmp)
        _freeze_audit_ledger(args.data_root, frozen_root)

        python_report = observatory_health(
            frozen_root,
            expected_interval_seconds=args.expected_interval,
            stale_after_seconds=args.stale_after,
            verify_latest=verify_latest,
            now=now,
        )

        command = [
            str(args.rust_binary),
            "observatory-health",
            str(frozen_root),
            "--expected-interval",
            str(args.expected_interval),
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
            print(f"mismatch=Rust health produced no JSON; stderr={completed.stderr.strip()}")
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
        "ok={} records={} mismatches={} python_ok={} rust_ok={}".format(
            str(not mismatches).lower(),
            python_report.get("manifest_records"),
            len(mismatches),
            str(bool(python_report.get("ok"))).lower(),
            str(bool(rust_report.get("ok"))).lower(),
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
