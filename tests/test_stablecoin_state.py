from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from crossalpha.catalog import build_catalog
from crossalpha.observatory.features.stablecoins import (
    build_stablecoin_state,
    compute_stablecoin_system_state,
)
from crossalpha.observatory.query import latest_stablecoin_state


def _frames(observed_at: datetime, *, usdc_chain_supply: float = 50.0) -> tuple[pd.DataFrame, pd.DataFrame]:
    assets = pd.DataFrame(
        [
            {
                "observed_at": observed_at,
                "known_at": observed_at,
                "stablecoin_id": "usdt",
                "symbol": "USDT",
                "peg_type": "peggedUSD",
                "price_usd": 1.0,
                "circulating_native": 100.0,
                "delta_1d_native": 3.0,
                "delta_7d_native": 7.0,
                "delta_30d_native": 20.0,
                "market_value_usd": 100.0,
                "peg_deviation_bps": 0.0,
                "raw_sha256": "a",
            },
            {
                "observed_at": observed_at,
                "known_at": observed_at,
                "stablecoin_id": "usdc",
                "symbol": "USDC",
                "peg_type": "peggedUSD",
                "price_usd": 0.999,
                "circulating_native": 50.0,
                "delta_1d_native": 2.0,
                "delta_7d_native": 4.0,
                "delta_30d_native": 9.0,
                "market_value_usd": 49.95,
                "peg_deviation_bps": -10.0,
                "raw_sha256": "b",
            },
            {
                "observed_at": observed_at,
                "known_at": observed_at,
                "stablecoin_id": "eurc",
                "symbol": "EURC",
                "peg_type": "peggedEUR",
                "price_usd": 1.17,
                "circulating_native": 10.0,
                "delta_1d_native": 1.0,
                "delta_7d_native": 1.0,
                "delta_30d_native": 1.0,
                "market_value_usd": 11.7,
                "peg_deviation_bps": None,
                "raw_sha256": "c",
            },
        ]
    )
    chains = pd.DataFrame(
        [
            {
                "observed_at": observed_at,
                "known_at": observed_at,
                "stablecoin_id": "usdt",
                "symbol": "USDT",
                "peg_type": "peggedUSD",
                "chain": "Ethereum",
                "circulating_native": 60.0,
                "market_value_usd": 60.0,
                "raw_sha256": "a",
            },
            {
                "observed_at": observed_at,
                "known_at": observed_at,
                "stablecoin_id": "usdt",
                "symbol": "USDT",
                "peg_type": "peggedUSD",
                "chain": "Tron",
                "circulating_native": 40.0,
                "market_value_usd": 40.0,
                "raw_sha256": "a",
            },
            {
                "observed_at": observed_at,
                "known_at": observed_at,
                "stablecoin_id": "usdc",
                "symbol": "USDC",
                "peg_type": "peggedUSD",
                "chain": "Ethereum",
                "circulating_native": usdc_chain_supply,
                "market_value_usd": usdc_chain_supply * 0.999,
                "raw_sha256": "b",
            },
            {
                "observed_at": observed_at,
                "known_at": observed_at,
                "stablecoin_id": "eurc",
                "symbol": "EURC",
                "peg_type": "peggedEUR",
                "chain": "Ethereum",
                "circulating_native": 10.0,
                "market_value_usd": 11.7,
                "raw_sha256": "c",
            },
        ]
    )
    return assets, chains


def test_stablecoin_state_keeps_usd_accounting_and_conservation_explicit() -> None:
    now = datetime(2026, 9, 4, 13, 0, tzinfo=timezone.utc)
    assets, chains = _frames(now)
    system, chain_state = compute_stablecoin_system_state(assets, chains)

    row = system.iloc[0]
    assert row["usd_stablecoin_count"] == 2
    assert row["usd_supply_native"] == pytest.approx(150.0)
    assert row["usd_market_value_usd"] == pytest.approx(149.95)
    assert row["usd_delta_1d_native"] == pytest.approx(5.0)
    assert row["chain_coverage_ratio"] == pytest.approx(1.0)
    assert row["chain_abs_residual_ratio"] == pytest.approx(0.0)
    assert row["usdt_share"] == pytest.approx(100.0 / 149.95)
    assert set(chain_state["chain"]) == {"Ethereum", "Tron"}
    ethereum = chain_state.loc[chain_state["chain"] == "Ethereum"].iloc[0]
    assert ethereum["market_value_usd"] == pytest.approx(109.95)


def test_stablecoin_state_reports_chain_coverage_residual_without_hiding_it() -> None:
    now = datetime(2026, 9, 4, 13, 0, tzinfo=timezone.utc)
    assets, chains = _frames(now, usdc_chain_supply=40.0)
    system, _ = compute_stablecoin_system_state(assets, chains)

    row = system.iloc[0]
    assert row["chain_coverage_ratio"] == pytest.approx(140.0 / 150.0)
    assert row["chain_residual_native"] == pytest.approx(-10.0)
    assert row["chain_abs_residual_native"] == pytest.approx(10.0)
    assert row["chain_abs_residual_ratio"] == pytest.approx(10.0 / 150.0)


def test_stablecoin_materialization_and_query_return_latest_snapshot(tmp_path: Path) -> None:
    t0 = datetime(2026, 9, 4, 13, 0, tzinfo=timezone.utc)
    day_dir_assets = tmp_path / "canonical" / "defillama" / "stablecoin_assets" / "year=2026" / "month=09" / "day=04"
    day_dir_chains = tmp_path / "canonical" / "defillama" / "stablecoin_chain_supply" / "year=2026" / "month=09" / "day=04"
    day_dir_assets.mkdir(parents=True)
    day_dir_chains.mkdir(parents=True)

    for index, observed in enumerate((t0, t0 + timedelta(minutes=5))):
        assets, chains = _frames(observed)
        assets.to_parquet(day_dir_assets / f"snapshot_{index}.parquet", index=False)
        chains.to_parquet(day_dir_chains / f"snapshot_{index}.parquet", index=False)

    result = build_stablecoin_state(tmp_path, recent_only=True)
    assert result["written_days"] == 1
    assert result["rows_written"] == 2

    build_catalog(tmp_path)
    latest = latest_stablecoin_state(tmp_path, top_chains=2)
    assert latest["system"] is not None
    assert latest["system"]["observed_at"] == t0 + timedelta(minutes=5)
    assert len(latest["top_chains"]) == 2
    assert latest["top_chains"][0]["chain"] == "Ethereum"
