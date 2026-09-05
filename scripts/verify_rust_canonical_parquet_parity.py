from __future__ import annotations

import argparse
import json
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

from crossalpha.observatory.canonical.hyperliquid import (  # noqa: E402
    parse_meta_and_asset_contexts,
)
from crossalpha.observatory.canonical.stablecoins import (  # noqa: E402
    parse_stablecoin_snapshot,
)
from verify_rust_canonical_parser_parity import (  # noqa: E402
    _diff,
    _frame_records,
    _freeze_latest,
    _load_envelope,
)


def _schema_signature(path: Path) -> list[tuple[str, str, bool]]:
    schema = pq.read_schema(path)
    return [(field.name, str(field.type), field.nullable) for field in schema]


def _sorted_records(frame: pd.DataFrame, kind: str) -> list[dict[str, Any]]:
    rows = _frame_records(frame)
    if kind == "hyperliquid":
        return sorted(rows, key=lambda row: str(row.get("asset")))
    if kind == "assets":
        return sorted(rows, key=lambda row: str(row.get("stablecoin_id")))
    return sorted(
        rows,
        key=lambda row: (str(row.get("stablecoin_id")), str(row.get("chain"))),
    )


def _run_rust(
    binary: Path,
    frozen_root: Path,
    output_dir: Path,
    source: str,
) -> dict[str, Any]:
    completed = subprocess.run(
        [
            str(binary),
            "canonical-parquet-preview",
            str(frozen_root),
            "--output-dir",
            str(output_dir),
            "--source",
            source,
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Rust canonical-parquet-preview failed for {source}: "
            f"{completed.stderr.strip()}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"invalid Rust parquet preview JSON for {source}: {exc}; "
            f"stdout={completed.stdout[:500]!r}"
        ) from exc


def _compare_file(
    python_path: Path,
    rust_path: Path,
    kind: str,
    mismatch_prefix: str,
) -> list[str]:
    mismatches: list[str] = []
    if not rust_path.exists():
        return [f"{mismatch_prefix}: Rust parquet missing: {rust_path}"]

    python_frame = pd.read_parquet(python_path)
    rust_frame = pd.read_parquet(rust_path)

    python_columns = list(python_frame.columns)
    rust_columns = list(rust_frame.columns)
    if python_columns != rust_columns:
        mismatches.append(
            f"{mismatch_prefix}.columns: python={python_columns!r} rust={rust_columns!r}"
        )

    python_schema = _schema_signature(python_path)
    rust_schema = _schema_signature(rust_path)
    if python_schema != rust_schema:
        mismatches.append(
            f"{mismatch_prefix}.schema: python={python_schema!r} rust={rust_schema!r}"
        )

    python_rows = _sorted_records(python_frame, kind)
    rust_rows = _sorted_records(rust_frame, kind)
    mismatches.extend(
        _diff(
            python_rows,
            rust_rows,
            f"{mismatch_prefix}.rows",
            limit=max(1, 50 - len(mismatches)),
        )
    )
    return mismatches[:50]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare Python pandas/pyarrow canonical Parquet with native Rust "
            "Arrow/Parquet output on frozen real snapshots."
        )
    )
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument(
        "--rust-binary",
        type=Path,
        default=REPO_ROOT / "target" / "debug" / "crossalpha-rs",
    )
    args = parser.parse_args()

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
    if "canonical-parquet-preview" not in help_result.stdout:
        print(
            "ok=false mismatches=1 error=Rust binary is stale and lacks "
            "canonical-parquet-preview; run cargo build -p crossalpha-cli"
        )
        return 1

    with tempfile.TemporaryDirectory(prefix="crossalpha-parquet-parity-") as tmp:
        root = Path(tmp)
        frozen_root = root / "frozen"
        python_dir = root / "python"
        rust_dir = root / "rust"
        python_dir.mkdir(parents=True)
        rust_dir.mkdir(parents=True)

        frozen = _freeze_latest(args.data_root, frozen_root)

        hyper_record = frozen["hyperliquid"]
        hyper_frame = parse_meta_and_asset_contexts(
            _load_envelope(hyper_record), hyper_record
        )
        python_hyper = python_dir / "hyperliquid.parquet"
        hyper_frame.to_parquet(python_hyper, index=False)

        stable_record = frozen["stablecoins"]
        asset_frame, chain_frame = parse_stablecoin_snapshot(
            _load_envelope(stable_record), stable_record
        )
        python_assets = python_dir / "stablecoin_assets.parquet"
        python_chains = python_dir / "stablecoin_chain_supply.parquet"
        asset_frame.to_parquet(python_assets, index=False)
        chain_frame.to_parquet(python_chains, index=False)

        hyper_report = _run_rust(
            args.rust_binary, frozen_root, rust_dir, "hyperliquid"
        )
        stable_report = _run_rust(
            args.rust_binary, frozen_root, rust_dir, "stablecoins"
        )

        mismatches: list[str] = []
        if hyper_report.get("raw_sha256") != hyper_record.sha256:
            mismatches.append("$.hyperliquid.raw_sha256: selected snapshot differs")
        if stable_report.get("raw_sha256") != stable_record.sha256:
            mismatches.append("$.stablecoins.raw_sha256: selected snapshot differs")

        comparisons = (
            (
                python_hyper,
                rust_dir / "hyperliquid.parquet",
                "hyperliquid",
                "$.hyperliquid",
            ),
            (
                python_assets,
                rust_dir / "stablecoin_assets.parquet",
                "assets",
                "$.stablecoin_assets",
            ),
            (
                python_chains,
                rust_dir / "stablecoin_chain_supply.parquet",
                "chains",
                "$.stablecoin_chains",
            ),
        )
        for python_path, rust_path, kind, prefix in comparisons:
            if len(mismatches) >= 50:
                break
            mismatches.extend(
                _compare_file(python_path, rust_path, kind, prefix)[
                    : 50 - len(mismatches)
                ]
            )

        print(
            "ok={} mismatches={} hyperliquid_rows={} stablecoin_assets={} "
            "stablecoin_chains={} schema_files=3".format(
                str(not mismatches).lower(),
                len(mismatches),
                len(hyper_frame),
                len(asset_frame),
                len(chain_frame),
            )
        )
        for mismatch in mismatches:
            print(f"mismatch={mismatch}")
        return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
