from __future__ import annotations

import json
import math
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

from crossalpha.state.v02 import compute_state_v02
from crossalpha.state.v02_config import strict_v02_config_consistency_report

REPO_ROOT = Path(__file__).resolve().parents[1]
RUST_STATE = REPO_ROOT / "target" / "debug" / "crossalpha-state-rs"
RUST_KERNEL = REPO_ROOT / "target" / "debug" / "crossalpha-state-v02-kernel-rs"
CONFIG = REPO_ROOT / "config" / "state_v02.yaml"


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
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return json.loads(completed.stdout)


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, str):
        try:
            stamp = pd.Timestamp(value)
            if stamp.tzinfo is not None and ("T" in value or " " in value):
                return stamp.tz_convert("UTC").isoformat()
        except Exception:
            pass
        return value
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _diff(left: Any, right: Any, path: str = "$") -> list[str]:
    if isinstance(left, dict) and isinstance(right, dict):
        errors: list[str] = []
        if set(left) != set(right):
            errors.append(
                f"{path}: key mismatch python_only={sorted(set(left)-set(right))} "
                f"rust_only={sorted(set(right)-set(left))}"
            )
        for key in sorted(set(left) & set(right)):
            errors.extend(_diff(left[key], right[key], f"{path}.{key}"))
        return errors
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return [f"{path}: len mismatch python={len(left)} rust={len(right)}"]
        errors: list[str] = []
        for index, (a, b) in enumerate(zip(left, right)):
            errors.extend(_diff(a, b, f"{path}[{index}]"))
        return errors
    if isinstance(left, (int, float)) and not isinstance(left, bool) and isinstance(right, (int, float)) and not isinstance(right, bool):
        a = float(left)
        b = float(right)
        if math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-10):
            return []
        return [f"{path}: numeric mismatch python={a} rust={b}"]
    if left != right:
        return [f"{path}: mismatch python={left!r} rust={right!r}"]
    return []


def _fixture() -> tuple[dict[str, Any], dict[str, pd.DataFrame]]:
    current = pd.Timestamp("2026-09-06T12:00:00Z")
    generated = pd.Timestamp("2026-09-06T12:05:00Z")
    prior = current - pd.Timedelta(hours=24)
    lag = current - pd.Timedelta(hours=168)

    aave = [
        {"observed_at": prior.isoformat(), "known_at": prior.isoformat(), "symbol": "WETH", "market_name": "Ethereum Core", "borrow_apy_pct": 4.0, "available_liquidity_usd": 30_000_000.0, "borrow_cap_reached": False, "is_frozen": False, "is_paused": False},
        {"observed_at": prior.isoformat(), "known_at": prior.isoformat(), "symbol": "WBTC", "market_name": "Ethereum Core", "borrow_apy_pct": 3.0, "available_liquidity_usd": 20_000_000.0, "borrow_cap_reached": False, "is_frozen": False, "is_paused": False},
        {"observed_at": prior.isoformat(), "known_at": prior.isoformat(), "symbol": "USDC", "market_name": "Ethereum Core", "borrow_apy_pct": 5.0, "available_liquidity_usd": 50_000_000.0, "borrow_cap_reached": False, "is_frozen": False, "is_paused": False},
        {"observed_at": current.isoformat(), "known_at": current.isoformat(), "symbol": "WETH", "market_name": "Ethereum Core", "borrow_apy_pct": 8.0, "available_liquidity_usd": 8_000_000.0, "borrow_cap_reached": False, "is_frozen": False, "is_paused": False},
        {"observed_at": current.isoformat(), "known_at": current.isoformat(), "symbol": "WBTC", "market_name": "Ethereum Core", "borrow_apy_pct": 6.0, "available_liquidity_usd": 18_000_000.0, "borrow_cap_reached": False, "is_frozen": False, "is_paused": False},
        {"observed_at": current.isoformat(), "known_at": current.isoformat(), "symbol": "USDC", "market_name": "Ethereum Core", "borrow_apy_pct": 7.0, "available_liquidity_usd": 40_000_000.0, "borrow_cap_reached": False, "is_frozen": False, "is_paused": False},
    ]
    system = [
        {"observed_at": lag.isoformat(), "known_at": lag.isoformat(), "usd_market_value_usd": 100_000_000_000.0, "chain_coverage_ratio": 0.995, "chain_abs_residual_ratio": 0.005},
        {"observed_at": current.isoformat(), "known_at": current.isoformat(), "usd_market_value_usd": 98_000_000_000.0, "chain_coverage_ratio": 0.995, "chain_abs_residual_ratio": 0.005},
    ]
    chain_state = [
        {"observed_at": lag.isoformat(), "known_at": lag.isoformat(), "chain": "Ethereum", "market_value_usd": 70_000_000_000.0},
        {"observed_at": lag.isoformat(), "known_at": lag.isoformat(), "chain": "Tron", "market_value_usd": 30_000_000_000.0},
        {"observed_at": current.isoformat(), "known_at": current.isoformat(), "chain": "Ethereum", "market_value_usd": 65_000_000_000.0},
        {"observed_at": current.isoformat(), "known_at": current.isoformat(), "chain": "Tron", "market_value_usd": 33_000_000_000.0},
    ]
    basis = [
        {"observed_at": current.isoformat(), "known_at": current.isoformat(), "asset": "BTC", "basis_z_24h": 1.5},
        {"observed_at": current.isoformat(), "known_at": current.isoformat(), "asset": "ETH", "basis_z_24h": -0.5},
    ]
    composition = [
        {"observed_at": current.isoformat(), "known_at": current.isoformat(), "stablecoin_id": "usdt", "chain": "Ethereum", "market_value_usd": 25_000_000_000.0},
        {"observed_at": current.isoformat(), "known_at": current.isoformat(), "stablecoin_id": "usdc", "chain": "Ethereum", "market_value_usd": 40_000_000_000.0},
        {"observed_at": current.isoformat(), "known_at": current.isoformat(), "stablecoin_id": "usdt", "chain": "Tron", "market_value_usd": 32_000_000_000.0},
        {"observed_at": current.isoformat(), "known_at": current.isoformat(), "stablecoin_id": "usdc", "chain": "Tron", "market_value_usd": 1_000_000_000.0},
    ]
    liquidations = [
        {"event_time": (current - pd.Timedelta(hours=3)).isoformat(), "observed_at": current.isoformat(), "known_at": current.isoformat(), "transaction_hash": "0xaaa", "log_index": 1},
        {"event_time": (current - pd.Timedelta(days=2)).isoformat(), "observed_at": current.isoformat(), "known_at": current.isoformat(), "transaction_hash": "0xbbb", "log_index": 2},
    ]
    rust = {
        "as_of": current.isoformat(),
        "generated_at": generated.isoformat(),
        "inputs": {
            "aave_markets": aave,
            "stablecoin_system": system,
            "stablecoin_chain_state": chain_state,
            "hyperliquid_market_state": basis,
            "stablecoin_chain_composition": composition,
            "aave_liquidations": liquidations,
        },
    }
    frames = {
        "aave": pd.DataFrame(aave),
        "system": pd.DataFrame(system),
        "chain_state": pd.DataFrame(chain_state),
        "basis": pd.DataFrame(basis),
        "composition": pd.DataFrame(composition),
        "liquidations": pd.DataFrame(liquidations),
    }
    return rust, frames


def main() -> int:
    errors: list[str] = []
    python_config = strict_v02_config_consistency_report(CONFIG)
    rust_config = _run_json([str(RUST_STATE), "v02", "config-check", "--config", str(CONFIG)])
    errors.extend(_diff(_normalize(python_config), _normalize(rust_config), "$.config"))

    fixture, frames = _fixture()
    with tempfile.TemporaryDirectory(prefix="crossalpha-v02-parity-") as tmp:
        path = Path(tmp) / "fixture.json"
        path.write_text(json.dumps(fixture, ensure_ascii=False, indent=2), encoding="utf-8")
        rust = _run_json([str(RUST_KERNEL), "--input", str(path)])
    python = compute_state_v02(
        frames["aave"],
        frames["system"],
        frames["chain_state"],
        frames["basis"],
        frames["composition"],
        frames["liquidations"],
        as_of=fixture["as_of"],
        generated_at=fixture["generated_at"],
    )
    errors.extend(_diff(_normalize(python), _normalize(rust), "$.state"))
    print(f"ok={str(not errors).lower()} mismatches={len(errors)}")
    for error in errors[:100]:
        print(f"mismatch={error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
