from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from crossalpha.state.v04 import compute_market_mechanics  # noqa: E402
from crossalpha.state.v04_config import strict_v04_config_report  # noqa: E402
from crossalpha.state.v04_provider import parse_venue_snapshot  # noqa: E402

KNOWN_AT = "2026-01-02T12:00:00+00:00"
GENERATED_AT = "2026-01-02T12:00:05+00:00"


def _ms(text: str) -> int:
    return int(pd.Timestamp(text).timestamp() * 1000)


def _fixtures() -> list[dict[str, Any]]:
    recent = _ms("2026-01-02T11:59:50+00:00")
    funding_latest = _ms("2026-01-02T08:00:00+00:00")
    funding_previous = funding_latest - 8 * 60 * 60 * 1000
    rows: list[dict[str, Any]] = []
    for asset, base in (("BTC", 100_000.0), ("ETH", 4_000.0)):
        symbol = f"{asset}USDT"
        rows.append(
            {
                "venue": "binance",
                "asset": asset,
                "spot_symbol": symbol,
                "perp_symbol": symbol,
                "spot": {"bidPrice": str(base - 1), "askPrice": str(base + 1)},
                "perp_depth": {
                    "bids": [[str(base + 9), "1"]],
                    "asks": [[str(base + 11), "1"]],
                    "E": recent,
                    "T": recent,
                },
                "premium": {
                    "markPrice": str(base + 10),
                    "indexPrice": str(base),
                    "time": recent,
                },
                "open_interest": {"openInterest": "10", "time": recent},
                "funding_history": [
                    {"fundingRate": "0.00010", "fundingTime": funding_previous},
                    {"fundingRate": "0.00012", "fundingTime": funding_latest},
                ],
            }
        )
        spot_id = f"{asset}-USDT"
        swap_id = f"{asset}-USDT-SWAP"
        rows.append(
            {
                "venue": "okx",
                "asset": asset,
                "spot_symbol": spot_id,
                "perp_symbol": swap_id,
                "spot": {"code": "0", "data": [{"bidPx": str(base - 2), "askPx": str(base + 2), "ts": str(recent)}]},
                "perp": {"code": "0", "data": [{"bidPx": str(base + 6), "askPx": str(base + 10), "ts": str(recent)}]},
                "funding_history": {
                    "code": "0",
                    "data": [
                        {"realizedRate": "0.00008", "fundingTime": str(funding_previous)},
                        {"realizedRate": "0.00010", "fundingTime": str(funding_latest)},
                    ],
                },
                "open_interest": {"code": "0", "data": [{"oiUsd": "25000000", "ts": str(recent)}]},
            }
        )
        rows.append(
            {
                "venue": "bybit",
                "asset": asset,
                "spot_symbol": symbol,
                "perp_symbol": symbol,
                "spot": {
                    "retCode": 0,
                    "time": recent,
                    "result": {"list": [{"bid1Price": str(base - 3), "ask1Price": str(base + 3)}]},
                },
                "perp": {
                    "retCode": 0,
                    "time": recent,
                    "result": {
                        "list": [{
                            "bid1Price": str(base + 4),
                            "ask1Price": str(base + 12),
                            "markPrice": str(base + 8),
                            "indexPrice": str(base),
                            "openInterestValue": "20000000",
                        }]
                    },
                },
                "funding_history": {
                    "retCode": 0,
                    "result": {
                        "list": [
                            {"fundingRate": "0.00009", "fundingRateTimestamp": str(funding_previous)},
                            {"fundingRate": "0.00011", "fundingRateTimestamp": str(funding_latest)},
                        ]
                    },
                },
            }
        )
    return rows


def _run_json(command: list[str]) -> Any:
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed rc={completed.returncode}: {' '.join(command)}\n"
            + (completed.stderr.strip() or completed.stdout.strip())
        )
    return json.loads(completed.stdout)


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        if parsed.tzinfo is None:
            return value
        return parsed.astimezone(timezone.utc).isoformat()
    return value


def _diff(expected: Any, actual: Any, path: str = "$", limit: int = 100) -> list[str]:
    mismatches: list[str] = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        if set(expected) != set(actual):
            mismatches.append(
                f"{path}.keys: python={sorted(expected)!r} rust={sorted(actual)!r}"
            )
        for key in sorted(set(expected) & set(actual)):
            if len(mismatches) >= limit:
                break
            mismatches.extend(_diff(expected[key], actual[key], f"{path}.{key}", limit - len(mismatches)))
        return mismatches
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return [f"{path}.len: python={len(expected)} rust={len(actual)}"]
        for index, (left, right) in enumerate(zip(expected, actual)):
            if len(mismatches) >= limit:
                break
            mismatches.extend(_diff(left, right, f"{path}[{index}]", limit - len(mismatches)))
        return mismatches
    if isinstance(expected, (int, float)) and not isinstance(expected, bool) and isinstance(actual, (int, float)) and not isinstance(actual, bool):
        left = float(expected)
        right = float(actual)
        if math.isnan(left) and math.isnan(right):
            return []
        if not math.isclose(left, right, rel_tol=1e-10, abs_tol=1e-10):
            return [f"{path}: python={expected!r} rust={actual!r}"]
        return []
    if expected != actual:
        return [f"{path}: python={expected!r} rust={actual!r}"]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare deterministic State V0.4 config, venue normalization and mechanics between Python and Rust."
    )
    parser.add_argument(
        "--config-binary",
        type=Path,
        default=REPO_ROOT / "target" / "release" / "crossalpha-state-v04-config-check-rs",
    )
    parser.add_argument(
        "--parse-binary",
        type=Path,
        default=REPO_ROOT / "target" / "release" / "crossalpha-state-v04-parse-rs",
    )
    parser.add_argument(
        "--mechanics-binary",
        type=Path,
        default=REPO_ROOT / "target" / "release" / "crossalpha-state-v04-mechanics-rs",
    )
    args = parser.parse_args()
    missing = [str(path) for path in (args.config_binary, args.parse_binary, args.mechanics_binary) if not path.exists()]
    if missing:
        print(f"ok=false mismatches=1 error=Rust binaries missing: {missing}")
        return 1

    mismatches: list[str] = []
    python_config = strict_v04_config_report(REPO_ROOT / "config" / "state_v04.yaml")
    rust_config = _run_json([str(args.config_binary), "config/state_v04.yaml"])
    mismatches.extend(_diff(_normalize(python_config), _normalize(rust_config), "$.config"))

    python_rows: list[dict[str, Any]] = []
    rust_rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="crossalpha-v04-parity-") as tmp:
        root = Path(tmp)
        for index, payload in enumerate(_fixtures()):
            path = root / f"slot-{index}.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            python_row = parse_venue_snapshot(payload, known_at=KNOWN_AT)
            rust_row = _run_json([str(args.parse_binary), str(path), "--known-at", KNOWN_AT])
            python_rows.append(python_row)
            rust_rows.append(rust_row)
            mismatches.extend(
                _diff(
                    _normalize(python_row),
                    _normalize(rust_row),
                    f"$.normalized[{payload['asset']}:{payload['venue']}]",
                    max(0, 100 - len(mismatches)),
                )
            )

        rust_rows_path = root / "rust-rows.json"
        rust_rows_path.write_text(json.dumps(rust_rows), encoding="utf-8")
        python_mechanics = compute_market_mechanics(
            pd.DataFrame(python_rows), generated_at=GENERATED_AT
        )
        rust_mechanics = _run_json(
            [
                str(args.mechanics_binary),
                str(rust_rows_path),
                "--generated-at",
                GENERATED_AT,
                "--maximum-age-seconds",
                "90",
            ]
        )
        mismatches.extend(
            _diff(
                _normalize(python_mechanics),
                _normalize(rust_mechanics),
                "$.mechanics",
                max(0, 100 - len(mismatches)),
            )
        )

    print(
        f"ok={str(not mismatches).lower()} mismatches={len(mismatches)} slots={len(python_rows)} "
        f"config_ok={str(bool(python_config.get('ok'))).lower()}"
    )
    for mismatch in mismatches[:100]:
        print(f"mismatch={mismatch}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
