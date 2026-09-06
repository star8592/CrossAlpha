from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from crossalpha.state.v03 import CensusPolicy, compute_borrower_census  # noqa: E402
from crossalpha.state.v04 import compute_market_mechanics  # noqa: E402
from crossalpha.state.v04_config import strict_v04_config_report  # noqa: E402
from crossalpha.state.v04_provider import parse_venue_snapshot  # noqa: E402
from verify_rust_canonical_parser_parity import _diff, _normalize  # noqa: E402


def _run(binary: Path, args: list[str]) -> Any:
    result = subprocess.run(
        [str(binary), *args], cwd=REPO_ROOT, text=True, capture_output=True, check=False
    )
    if result.returncode not in (0, 2):
        raise RuntimeError(
            f"{binary.name} failed rc={result.returncode}: {result.stderr.strip()}"
        )
    return json.loads(result.stdout)


def _v03_fixture() -> dict[str, Any]:
    captured = "2026-09-06T00:00:00+00:00"
    rows = [
        {
            "address": "0x0000000000000000000000000000000000000001",
            "success": True,
            "error": None,
            "total_collateral_usd": 150.0,
            "total_debt_usd": 100.0,
            "available_borrows_usd": 0.0,
            "current_liquidation_threshold_pct": 80.0,
            "ltv_pct": 75.0,
            "health_factor": 0.99,
        },
        {
            "address": "0x0000000000000000000000000000000000000002",
            "success": True,
            "error": None,
            "total_collateral_usd": 350.0,
            "total_debt_usd": 200.0,
            "available_borrows_usd": 10.0,
            "current_liquidation_threshold_pct": 82.0,
            "ltv_pct": 77.0,
            "health_factor": 1.10,
        },
        {
            "address": "0x0000000000000000000000000000000000000003",
            "success": True,
            "error": None,
            "total_collateral_usd": 700.0,
            "total_debt_usd": 300.0,
            "available_borrows_usd": 100.0,
            "current_liquidation_threshold_pct": 80.0,
            "ltv_pct": 70.0,
            "health_factor": 1.60,
        },
    ]
    return {
        "rows": rows,
        "total_candidate_addresses": 3,
        "bootstrap_complete": True,
        "block_number": 20_000_000,
        "captured_at": captured,
    }


def _python_v03(fixture: dict[str, Any]) -> dict[str, Any]:
    frame = pd.DataFrame(fixture["rows"])
    return compute_borrower_census(
        frame,
        total_candidate_addresses=fixture["total_candidate_addresses"],
        bootstrap_complete=fixture["bootstrap_complete"],
        block_number=fixture["block_number"],
        captured_at=fixture["captured_at"],
        policy=CensusPolicy(),
    )


def _venue_payloads() -> list[dict[str, Any]]:
    t0 = 1_725_580_800_000
    t1 = t0 + 8 * 3_600_000
    payloads: list[dict[str, Any]] = []
    for asset, base in (("BTC", 60_000.0), ("ETH", 2_500.0)):
        symbol = f"{asset}USDT"
        payloads.append(
            {
                "venue": "binance",
                "asset": asset,
                "spot_symbol": symbol,
                "perp_symbol": symbol,
                "spot": {"bidPrice": str(base - 1), "askPrice": str(base + 1)},
                "perp_depth": {
                    "bids": [[str(base + 9), "1"]],
                    "asks": [[str(base + 11), "1"]],
                    "E": t1,
                },
                "premium": {"markPrice": str(base + 10), "indexPrice": str(base), "time": t1},
                "open_interest": {"openInterest": "100", "time": t1},
                "funding_history": [
                    {"fundingRate": "0.0001", "fundingTime": t0},
                    {"fundingRate": "0.0002", "fundingTime": t1},
                ],
                "perp": None,
                "collection_error": None,
            }
        )
        payloads.append(
            {
                "venue": "okx",
                "asset": asset,
                "spot_symbol": f"{asset}-USDT",
                "perp_symbol": f"{asset}-USDT-SWAP",
                "spot": {"code": "0", "data": [{"bidPx": str(base - 2), "askPx": str(base + 2), "ts": str(t1)}]},
                "perp": {"code": "0", "data": [{"bidPx": str(base + 7), "askPx": str(base + 13), "ts": str(t1)}]},
                "funding_history": {"code": "0", "data": [
                    {"realizedRate": "0.00015", "fundingTime": str(t0)},
                    {"realizedRate": "0.00025", "fundingTime": str(t1)},
                ]},
                "open_interest": {"code": "0", "data": [{"oiUsd": "5000000", "ts": str(t1)}]},
                "perp_depth": None,
                "premium": None,
                "collection_error": None,
            }
        )
        payloads.append(
            {
                "venue": "bybit",
                "asset": asset,
                "spot_symbol": symbol,
                "perp_symbol": symbol,
                "spot": {"retCode": 0, "time": t1, "result": {"list": [{"bid1Price": str(base - 3), "ask1Price": str(base + 3)}]}},
                "perp": {"retCode": 0, "time": t1, "result": {"list": [{
                    "bid1Price": str(base + 5),
                    "ask1Price": str(base + 15),
                    "markPrice": str(base + 10),
                    "indexPrice": str(base),
                    "openInterestValue": "6000000",
                }]}},
                "funding_history": {"retCode": 0, "result": {"list": [
                    {"fundingRate": "0.00012", "fundingRateTimestamp": str(t0)},
                    {"fundingRate": "0.00022", "fundingRateTimestamp": str(t1)},
                ]}},
                "perp_depth": None,
                "premium": None,
                "open_interest": None,
                "collection_error": None,
            }
        )
    return payloads


def main() -> int:
    bin_dir = REPO_ROOT / "target" / "debug"
    bins = {
        "v03_census": bin_dir / "crossalpha-state-v03-census-rs",
        "v04_config": bin_dir / "crossalpha-state-v04-config-check-rs",
        "v04_parse": bin_dir / "crossalpha-state-v04-parse-rs",
        "v04_mechanics": bin_dir / "crossalpha-state-v04-mechanics-rs",
    }
    missing = [str(path) for path in bins.values() if not path.exists()]
    if missing:
        print(f"ok=false mismatches=1 error=missing Rust binaries: {missing}")
        return 1

    mismatches: list[str] = []
    with tempfile.TemporaryDirectory(prefix="crossalpha-state-kernel-") as tmp:
        root = Path(tmp)
        v03_fixture = _v03_fixture()
        v03_path = root / "v03.json"
        v03_path.write_text(json.dumps(v03_fixture), encoding="utf-8")
        python_v03 = _normalize(_python_v03(v03_fixture))
        rust_v03 = _normalize(_run(bins["v03_census"], [str(v03_path)]))
        mismatches.extend(_diff(python_v03, rust_v03, "$.v03_census", limit=100))

        config = REPO_ROOT / "config" / "state_v04.yaml"
        python_config = _normalize(strict_v04_config_report(config))
        rust_config = _normalize(_run(bins["v04_config"], [str(config)]))
        mismatches.extend(_diff(python_config, rust_config, "$.v04_config", limit=100))

        known_at = datetime(2026, 9, 6, 8, 0, 10, tzinfo=timezone.utc)
        python_rows: list[dict[str, Any]] = []
        rust_rows: list[dict[str, Any]] = []
        for index, payload in enumerate(_venue_payloads()):
            path = root / f"venue-{index}.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            python_rows.append(parse_venue_snapshot(payload, known_at=known_at))
            rust_rows.append(
                _run(
                    bins["v04_parse"],
                    [str(path), "--known-at", known_at.isoformat()],
                )
            )
        mismatches.extend(
            _diff(
                _normalize(python_rows),
                _normalize(rust_rows),
                "$.v04_normalization",
                limit=100,
            )
        )

        rows_path = root / "rows.json"
        rows_path.write_text(json.dumps(rust_rows), encoding="utf-8")
        frame = pd.DataFrame(python_rows)
        python_mechanics = _normalize(
            compute_market_mechanics(frame, generated_at=known_at)
        )
        rust_mechanics = _normalize(
            _run(
                bins["v04_mechanics"],
                [
                    str(rows_path),
                    "--generated-at",
                    known_at.isoformat(),
                    "--maximum-age-seconds",
                    "90",
                ],
            )
        )
        mismatches.extend(
            _diff(
                python_mechanics,
                rust_mechanics,
                "$.v04_mechanics",
                limit=100,
            )
        )

    print(f"ok={str(not mismatches).lower()} mismatches={len(mismatches)} v03_census=1 v04_config=1 v04_slots=6 v04_mechanics=1")
    for mismatch in mismatches[:100]:
        print(f"mismatch={mismatch}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
