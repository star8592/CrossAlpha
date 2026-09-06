from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from crossalpha.settings import Settings  # noqa: E402
from crossalpha.state.v03_cycle import FINALITY_LAG_BLOCKS  # noqa: E402
from crossalpha.state.v03_preflight import run_v03_preflight  # noqa: E402
from crossalpha.state.v03_rpc import (  # noqa: E402
    AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK,
)

STATIC_FIELDS = (
    "protocol",
    "data_cost_usd",
    "split_data_plane",
    "archive_rpc_required",
    "borrow_log_source",
    "block_time_source",
    "finality_lag_blocks",
    "historical_log_scan_from_block",
    "historical_log_scan_to_block",
    "historical_log_scan_ok",
    "fixed_block_account_call_ok",
    "actionability",
    "risk_multiplier",
)

ALLOWED_RPC_SOURCES = {
    "EVM_RPC_URL",
    "BLOCKSCOUT_ETH_RPC_ZERO_COST_FALLBACK",
    "BLOCKREQ_ZERO_COST_FALLBACK",
    "PUBLICNODE_ZERO_COST_FALLBACK",
    "LLAMARPC_ZERO_COST_FALLBACK",
}


def _run_rust(binary: Path, timeout: float) -> dict[str, Any]:
    completed = subprocess.run(
        [str(binary), "--http-timeout", str(timeout)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Rust State V0.3 preflight failed: "
            + (completed.stderr.strip() or completed.stdout.strip())
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"invalid Rust preflight JSON: {exc}; stdout={completed.stdout[:500]!r}"
        ) from exc


def _iso(value: Any) -> datetime:
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _validate_report(report: dict[str, Any], label: str) -> list[str]:
    mismatches: list[str] = []
    latest = int(report.get("latest_block", -1))
    finalized = int(report.get("finalized_block", -1))
    expected_finalized = max(
        latest - FINALITY_LAG_BLOCKS,
        AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK,
    )
    if finalized != expected_finalized:
        mismatches.append(
            f"{label}.finalized_relation: latest={latest} finalized={finalized} "
            f"expected={expected_finalized}"
        )
    recent_from = int(report.get("recent_borrow_scan_from_block", -1))
    expected_recent_from = max(
        finalized - 127,
        AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK,
    )
    if recent_from != expected_recent_from:
        mismatches.append(
            f"{label}.recent_from: actual={recent_from} expected={expected_recent_from}"
        )
    if int(report.get("recent_borrow_scan_to_block", -1)) != finalized:
        mismatches.append(f"{label}.recent_to != finalized")
    if report.get("state_rpc_source") not in ALLOWED_RPC_SOURCES:
        mismatches.append(
            f"{label}.state_rpc_source unexpected={report.get('state_rpc_source')!r}"
        )
    if report.get("rpc_source") != report.get("state_rpc_source"):
        mismatches.append(f"{label}.rpc_source != state_rpc_source")
    if int(report.get("recent_borrow_log_count", -1)) < 0:
        mismatches.append(f"{label}.recent_borrow_log_count invalid")
    if int(report.get("historical_borrow_log_count", -1)) < 0:
        mismatches.append(f"{label}.historical_borrow_log_count invalid")
    try:
        block_time = _iso(report.get("finalized_block_time"))
        if block_time > datetime.now(timezone.utc):
            mismatches.append(f"{label}.finalized_block_time is in the future")
    except Exception as exc:  # noqa: BLE001
        mismatches.append(f"{label}.finalized_block_time invalid: {type(exc).__name__}")
    return mismatches


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run Python then native Rust State V0.3 preflight and compare stable contract "
            "fields plus live-chain structural invariants."
        )
    )
    parser.add_argument(
        "--rust-binary",
        type=Path,
        default=(
            REPO_ROOT / "target" / "debug" / "crossalpha-state-v03-preflight-rs"
        ),
    )
    parser.add_argument("--max-block-delta", type=int, default=8)
    args = parser.parse_args()

    if not args.rust_binary.exists():
        print(f"ok=false mismatches=1 error=Rust binary missing: {args.rust_binary}")
        return 1

    settings = Settings()
    python_report = asyncio.run(run_v03_preflight(settings))
    rust_report = _run_rust(args.rust_binary, settings.crossalpha_http_timeout)

    mismatches: list[str] = []
    for field in STATIC_FIELDS:
        if python_report.get(field) != rust_report.get(field):
            mismatches.append(
                f"$.{field}: python={python_report.get(field)!r} rust={rust_report.get(field)!r}"
            )

    mismatches.extend(_validate_report(python_report, "$.python"))
    mismatches.extend(_validate_report(rust_report, "$.rust"))

    latest_delta = abs(
        int(python_report["latest_block"]) - int(rust_report["latest_block"])
    )
    finalized_delta = abs(
        int(python_report["finalized_block"]) - int(rust_report["finalized_block"])
    )
    if latest_delta > args.max_block_delta:
        mismatches.append(
            f"$.latest_block delta={latest_delta} exceeds {args.max_block_delta}"
        )
    if finalized_delta > args.max_block_delta:
        mismatches.append(
            f"$.finalized_block delta={finalized_delta} exceeds {args.max_block_delta}"
        )

    if (
        python_report.get("historical_borrow_log_count")
        != rust_report.get("historical_borrow_log_count")
    ):
        mismatches.append(
            "$.historical_borrow_log_count: python={} rust={}".format(
                python_report.get("historical_borrow_log_count"),
                rust_report.get("historical_borrow_log_count"),
            )
        )

    if python_report["finalized_block"] == rust_report["finalized_block"]:
        if (
            python_report.get("recent_borrow_log_count")
            != rust_report.get("recent_borrow_log_count")
        ):
            mismatches.append(
                "$.recent_borrow_log_count: python={} rust={}".format(
                    python_report.get("recent_borrow_log_count"),
                    rust_report.get("recent_borrow_log_count"),
                )
            )

    print(
        "ok={} mismatches={} python_rpc={} rust_rpc={} latest_delta={} "
        "finalized_delta={} historical_logs={}".format(
            str(not mismatches).lower(),
            len(mismatches),
            python_report.get("state_rpc_source"),
            rust_report.get("state_rpc_source"),
            latest_delta,
            finalized_delta,
            python_report.get("historical_borrow_log_count"),
        )
    )
    for mismatch in mismatches[:100]:
        print(f"mismatch={mismatch}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
