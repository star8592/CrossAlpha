from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from pandas.testing import assert_series_equal

from crossalpha.observatory.features.hyperliquid import (
    build_hyperliquid_market_state,
    compute_hyperliquid_market_state,
)


def _canonical_frame(count: int = 30, *, start: datetime | None = None) -> pd.DataFrame:
    start = start or datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(count):
        observed = start + timedelta(hours=i)
        mark = 100.0 + i
        oracle = mark - (0.05 + i * 0.002)
        rows.append(
            {
                "observed_at": observed,
                "known_at": observed,
                "asset": "BTC",
                "mark_price": mark,
                "oracle_price": oracle,
                "mid_price": mark,
                "prev_day_price": 95.0 + i * 0.1,
                "premium": 0.00001 * i,
                "funding_rate": 0.000001 * i,
                "open_interest": 10.0 + i * 0.2,
                "day_notional_volume": 1_000_000.0 + i * 10_000.0,
                "impact_bid": mark - 0.1,
                "impact_ask": mark + 0.1,
                "raw_sha256": f"hash-{i}",
                "raw_path": f"/raw/{i}.json.gz",
            }
        )
    return pd.DataFrame(rows)


def test_market_state_financial_features_and_rolling_history() -> None:
    result = compute_hyperliquid_market_state(_canonical_frame())
    first = result.iloc[0]
    latest = result.iloc[-1]

    expected_basis = (100.0 / 99.95 - 1.0) * 10_000.0
    assert abs(first["mark_oracle_basis_bps"] - expected_basis) < 1e-9
    assert abs(first["impact_spread_bps"] - 20.0) < 1e-9
    assert first["open_interest_notional"] == 1_000.0
    assert pd.isna(first["open_interest_change_pct"])
    assert latest["rolling_observations_24h"] >= 24
    assert pd.notna(latest["funding_z_24h"])
    assert pd.notna(latest["basis_z_24h"])


def test_future_observation_does_not_change_past_features() -> None:
    base = _canonical_frame(29)
    before = compute_hyperliquid_market_state(base)
    future = _canonical_frame(30)
    after = compute_hyperliquid_market_state(future).iloc[:29].reset_index(drop=True)

    for column in (
        "mark_oracle_basis_bps",
        "impact_spread_bps",
        "open_interest_change_pct",
        "funding_z_24h",
        "basis_z_24h",
        "oi_change_z_24h",
    ):
        assert_series_equal(before[column].reset_index(drop=True), after[column], check_names=False)


def test_daily_market_state_materialization_is_rebuildable(tmp_path: Path) -> None:
    source_root = (
        tmp_path
        / "canonical"
        / "hyperliquid"
        / "asset_contexts"
        / "year=2026"
        / "month=09"
        / "day=04"
    )
    source_root.mkdir(parents=True)

    frame = _canonical_frame(2, start=datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc))
    frame.iloc[[0]].to_parquet(source_root / "a.parquet", index=False)
    frame.iloc[[1]].to_parquet(source_root / "b.parquet", index=False)

    first = build_hyperliquid_market_state(tmp_path)
    second = build_hyperliquid_market_state(tmp_path)
    out = (
        tmp_path
        / "derived"
        / "hyperliquid"
        / "market_state"
        / "year=2026"
        / "month=09"
        / "day=04"
        / "market_state.parquet"
    )

    assert first["source_files"] == 2
    assert first["written_days"] == 1
    assert first["rows_written"] == 2
    assert second["written_days"] == 1
    assert out.exists()
    materialized = pd.read_parquet(out)
    assert len(materialized) == 2
    assert "mark_oracle_basis_bps" in materialized.columns
