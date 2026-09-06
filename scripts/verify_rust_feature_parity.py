from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from crossalpha.observatory.canonical.hyperliquid import (  # noqa: E402
    parse_meta_and_asset_contexts,
)
from crossalpha.observatory.canonical.stablecoins import (  # noqa: E402
    parse_stablecoin_snapshot,
)
from crossalpha.observatory.features.hyperliquid import (  # noqa: E402
    compute_hyperliquid_market_state,
)
from crossalpha.observatory.features.stablecoins import (  # noqa: E402
    compute_stablecoin_system_state,
)
from crossalpha.storage.indexes import load_recent_daily_manifests  # noqa: E402
from verify_rust_canonical_materializer import (  # noqa: E402
    _freeze_recent_daily_manifests,
)
from verify_rust_canonical_parser_parity import (  # noqa: E402
    _diff,
    _frame_records,
    _load_envelope,
    _normalize,
)


def _choose_assets(records: list[Any]) -> list[str]:
    hyper = [
        record
        for record in records
        if record.source_id == "hyperliquid"
        and record.observation_type == "metaAndAssetCtxs"
    ]
    if not hyper:
        raise RuntimeError("no frozen Hyperliquid metaAndAssetCtxs records")
    latest = max(hyper, key=lambda record: record.observed_at)
    frame = parse_meta_and_asset_contexts(_load_envelope(latest), latest)
    available = [str(value) for value in frame["asset"].dropna().tolist()]
    selected: list[str] = []
    for candidate in ("BTC", "ETH", "SOL", "HYPE"):
        if candidate in available and candidate not in selected:
            selected.append(candidate)
    for candidate in sorted(available):
        if candidate not in selected:
            selected.append(candidate)
        if len(selected) >= 4:
            break
    if not selected:
        raise RuntimeError("Hyperliquid frozen snapshot contains no assets")
    return selected[:4]


def _python_hyperliquid(records: list[Any], assets: list[str]) -> list[dict[str, Any]]:
    frames: list[pd.DataFrame] = []
    for record in sorted(records, key=lambda item: item.observed_at):
        if record.source_id != "hyperliquid" or record.observation_type != "metaAndAssetCtxs":
            continue
        frame = parse_meta_and_asset_contexts(_load_envelope(record), record)
        frames.append(frame.loc[frame["asset"].isin(assets)].copy())
    if not frames:
        raise RuntimeError("no Hyperliquid frames selected for feature parity")
    input_frame = pd.concat(frames, ignore_index=True)
    result = compute_hyperliquid_market_state(input_frame)
    rows = _frame_records(result)
    return sorted(
        rows,
        key=lambda row: (str(row.get("observed_at")), str(row.get("asset"))),
    )


def _python_stablecoins(
    records: list[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    assets: list[pd.DataFrame] = []
    chains: list[pd.DataFrame] = []
    for record in sorted(records, key=lambda item: item.observed_at):
        if record.source_id != "defillama" or record.observation_type != "stablecoins_snapshot":
            continue
        asset_frame, chain_frame = parse_stablecoin_snapshot(_load_envelope(record), record)
        assets.append(asset_frame)
        chains.append(chain_frame)
    if not assets or not chains:
        raise RuntimeError("no stablecoin frames selected for feature parity")
    asset_input = pd.concat(assets, ignore_index=True)
    chain_input = pd.concat(chains, ignore_index=True)
    system, chain_state = compute_stablecoin_system_state(asset_input, chain_input)
    system_rows = sorted(
        _frame_records(system), key=lambda row: str(row.get("observed_at"))
    )
    chain_rows = sorted(
        _frame_records(chain_state),
        key=lambda row: (str(row.get("observed_at")), str(row.get("chain"))),
    )
    return system_rows, chain_rows


def _run_rust(
    binary: Path,
    frozen_root: Path,
    output_json: Path,
    source: str,
    days: int,
    assets: list[str] | None = None,
) -> dict[str, Any]:
    command = [
        str(binary),
        "feature-preview",
        str(frozen_root),
        "--source",
        source,
        "--recent-days",
        str(days),
        "--output-json",
        str(output_json),
    ]
    for asset in assets or []:
        command.extend(["--asset", asset])
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Rust feature-preview failed for {source}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return _normalize(json.loads(output_json.read_text(encoding="utf-8")))


def _sort_rust(report: dict[str, Any]) -> None:
    if report.get("source") == "hyperliquid":
        report["rows"] = sorted(
            report.get("rows", []),
            key=lambda row: (str(row.get("observed_at")), str(row.get("asset"))),
        )
        return
    report["system"] = sorted(
        report.get("system", []), key=lambda row: str(row.get("observed_at"))
    )
    report["chains"] = sorted(
        report.get("chains", []),
        key=lambda row: (str(row.get("observed_at")), str(row.get("chain"))),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare Python and Rust causal feature kernels on the same frozen recent "
            "manifest partitions."
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
    if "feature-preview" not in help_result.stdout:
        print(
            "ok=false mismatches=1 error=Rust binary is stale and lacks feature-preview; "
            "run cargo build -p crossalpha-cli"
        )
        return 1

    with tempfile.TemporaryDirectory(prefix="crossalpha-feature-parity-") as tmp:
        root = Path(tmp)
        frozen_root = root / "frozen"
        _freeze_recent_daily_manifests(args.data_root, frozen_root, args.recent_days)
        records, errors = load_recent_daily_manifests(
            frozen_root, days=args.recent_days
        )
        if errors:
            raise RuntimeError(f"frozen daily manifests contain errors: {errors}")

        assets = _choose_assets(records)
        expected_hyper = {
            "source": "hyperliquid",
            "recent_days": args.recent_days,
            "assets": assets,
            "rows": _python_hyperliquid(records, assets),
        }
        system, chain_state = _python_stablecoins(records)
        expected_stable = {
            "source": "stablecoins",
            "recent_days": args.recent_days,
            "system": system,
            "chains": chain_state,
        }

        actual_hyper = _run_rust(
            args.rust_binary,
            frozen_root,
            root / "rust_hyper.json",
            "hyperliquid",
            args.recent_days,
            assets,
        )
        actual_stable = _run_rust(
            args.rust_binary,
            frozen_root,
            root / "rust_stable.json",
            "stablecoins",
            args.recent_days,
        )
        _sort_rust(actual_hyper)
        _sort_rust(actual_stable)

        mismatches = _diff(
            _normalize(expected_hyper), actual_hyper, "$.hyperliquid", limit=50
        )
        if len(mismatches) < 50:
            mismatches.extend(
                _diff(
                    _normalize(expected_stable),
                    actual_stable,
                    "$.stablecoins",
                    limit=50 - len(mismatches),
                )
            )

        zscore_rows = sum(
            1
            for row in expected_hyper["rows"]
            if row.get("funding_z_24h") is not None
        )
        print(
            "ok={} mismatches={} recent_days={} assets={} hyperliquid_rows={} "
            "rolling_ready_rows={} stablecoin_system_rows={} stablecoin_chain_rows={}".format(
                str(not mismatches).lower(),
                len(mismatches),
                args.recent_days,
                ",".join(assets),
                len(expected_hyper["rows"]),
                zscore_rows,
                len(system),
                len(chain_state),
            )
        )
        for mismatch in mismatches:
            print(f"mismatch={mismatch}")
        return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
