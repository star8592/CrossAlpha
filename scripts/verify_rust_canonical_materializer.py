from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from crossalpha.observatory.canonical.hyperliquid import canonicalize_hyperliquid  # noqa: E402
from crossalpha.observatory.canonical.stablecoins import canonicalize_stablecoins  # noqa: E402
from crossalpha.storage.indexes import manifest_lock  # noqa: E402
from verify_rust_canonical_parser_parity import _diff, _frame_records  # noqa: E402


def _freeze_recent_daily_manifests(data_root: Path, frozen_root: Path, days: int) -> list[Path]:
    daily_root = data_root / "manifests" / "daily"
    with manifest_lock(data_root):
        paths = sorted(daily_root.glob("year=*/month=*/day=*/raw_snapshots.jsonl"))
        selected = paths[-days:]
        if not selected:
            raise RuntimeError(f"daily manifests missing under: {daily_root}")
        frozen_paths: list[Path] = []
        for source in selected:
            relative = source.relative_to(data_root)
            destination = frozen_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            frozen_paths.append(destination)
    return frozen_paths


def _schema_signature(path: Path) -> list[tuple[str, str, bool]]:
    schema = pq.read_schema(path)
    return [(field.name, str(field.type), field.nullable) for field in schema]


def _sort_rows(frame: pd.DataFrame, path: Path) -> list[dict[str, Any]]:
    rows = _frame_records(frame)
    value = str(path)
    if "hyperliquid/asset_contexts" in value:
        return sorted(rows, key=lambda row: str(row.get("asset")))
    if "stablecoin_assets" in value:
        return sorted(rows, key=lambda row: str(row.get("stablecoin_id")))
    if "stablecoin_chain_supply" in value:
        return sorted(
            rows,
            key=lambda row: (str(row.get("stablecoin_id")), str(row.get("chain"))),
        )
    return rows


def _compare_parquet(expected: Path, actual: Path, prefix: str) -> list[str]:
    mismatches: list[str] = []
    if not actual.exists():
        return [f"{prefix}: Rust file missing: {actual}"]

    expected_schema = _schema_signature(expected)
    actual_schema = _schema_signature(actual)
    if expected_schema != actual_schema:
        mismatches.append(
            f"{prefix}.schema: python={expected_schema!r} rust={actual_schema!r}"
        )

    expected_frame = pd.read_parquet(expected)
    actual_frame = pd.read_parquet(actual)
    if list(expected_frame.columns) != list(actual_frame.columns):
        mismatches.append(
            f"{prefix}.columns: python={list(expected_frame.columns)!r} "
            f"rust={list(actual_frame.columns)!r}"
        )

    mismatches.extend(
        _diff(
            _sort_rows(expected_frame, expected),
            _sort_rows(actual_frame, actual),
            f"{prefix}.rows",
            limit=max(1, 50 - len(mismatches)),
        )
    )
    return mismatches[:50]


def _relative_files(root: Path) -> list[Path]:
    canonical = root / "canonical"
    if not canonical.exists():
        return []
    return sorted(path.relative_to(canonical) for path in canonical.glob("**/*.parquet"))


def _run_rust(binary: Path, frozen_root: Path, rust_root: Path, days: int) -> dict[str, Any]:
    completed = subprocess.run(
        [
            str(binary),
            "canonical-materialize",
            str(frozen_root),
            "--output-root",
            str(rust_root),
            "--recent-days",
            str(days),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Rust canonical-materialize failed: "
            + (completed.stderr.strip() or completed.stdout.strip())
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"invalid Rust materializer JSON: {exc}; stdout={completed.stdout[:500]!r}"
        ) from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Freeze recent daily manifests, materialize canonical Parquet with Python and Rust "
            "into isolated roots, and compare paths/schema/values."
        )
    )
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--recent-days", type=int, default=2)
    parser.add_argument(
        "--rust-binary",
        type=Path,
        default=REPO_ROOT / "target" / "debug" / "crossalpha-rs",
    )
    args = parser.parse_args()

    if args.recent_days < 1:
        print("ok=false mismatches=1 error=--recent-days must be >= 1")
        return 1
    if not args.rust_binary.exists():
        print(f"ok=false mismatches=1 error=Rust binary missing: {args.rust_binary}")
        return 1

    help_result = subprocess.run(
        [str(args.rust_binary), "--help"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if "canonical-materialize" not in help_result.stdout:
        print(
            "ok=false mismatches=1 error=Rust binary is stale and lacks canonical-materialize; "
            "run cargo build -p crossalpha-cli"
        )
        return 1

    with tempfile.TemporaryDirectory(prefix="crossalpha-canonical-materializer-") as tmp:
        root = Path(tmp)
        frozen_root = root / "frozen"
        rust_root = root / "rust"
        frozen_paths = _freeze_recent_daily_manifests(
            args.data_root, frozen_root, args.recent_days
        )

        python_hyper = canonicalize_hyperliquid(
            frozen_root, recent_days=args.recent_days
        )
        python_stable = canonicalize_stablecoins(
            frozen_root, recent_days=args.recent_days
        )
        rust_report = _run_rust(
            args.rust_binary, frozen_root, rust_root, args.recent_days
        )

        python_files = _relative_files(frozen_root)
        rust_files = _relative_files(rust_root)
        mismatches: list[str] = []
        if python_files != rust_files:
            missing = sorted(set(python_files) - set(rust_files))
            extra = sorted(set(rust_files) - set(python_files))
            if missing:
                mismatches.append(f"$.files.missing_from_rust={missing!r}")
            if extra:
                mismatches.append(f"$.files.extra_in_rust={extra!r}")

        for relative in python_files:
            if len(mismatches) >= 50:
                break
            expected = frozen_root / "canonical" / relative
            actual = rust_root / "canonical" / relative
            mismatches.extend(
                _compare_parquet(
                    expected,
                    actual,
                    f"$.files.{relative.as_posix()}",
                )[: 50 - len(mismatches)]
            )

        python_written = int(python_hyper["written"]) + int(python_stable["written"]) * 2
        rust_hyper = rust_report.get("hyperliquid", {})
        rust_stable = rust_report.get("stablecoins", {})
        rust_written = int(rust_hyper.get("written", -1)) + int(
            rust_stable.get("written", -1)
        ) * 2
        if rust_written != python_written:
            mismatches.append(
                f"$.written_files: python={python_written} rust={rust_written}"
            )

        print(
            "ok={} mismatches={} recent_days={} frozen_daily={} parquet_files={} "
            "hyperliquid_snapshots={} stablecoin_snapshots={}".format(
                str(not mismatches).lower(),
                len(mismatches),
                args.recent_days,
                len(frozen_paths),
                len(python_files),
                python_hyper["snapshots"],
                python_stable["snapshots"],
            )
        )
        for mismatch in mismatches[:50]:
            print(f"mismatch={mismatch}")
        return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
