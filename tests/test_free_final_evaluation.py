from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crossalpha.core.free_baselines import ALL_ASSETS, RISK_ASSETS
from crossalpha.core.free_final_evaluation import (
    B1,
    B3,
    _benchmark_stress_comparison,
    _neutralize_year,
    _weight_concentration,
)


def test_neutralize_year_replaces_only_target_year_with_cash() -> None:
    dates = pd.to_datetime(["2025-12-31", "2026-01-01", "2026-01-02"], utc=True)
    frame = pd.DataFrame(
        {
            "date": dates,
            "gross_return": [0.01, 0.02, -0.01],
            "net_return": [0.009, 0.019, -0.011],
            "cash_return": [0.0001, 0.0002, 0.0002],
            "turnover": [0.2, 0.3, 0.4],
            "cost": [0.001, 0.001, 0.001],
        }
    )
    stressed = _neutralize_year(frame, 2026)
    assert stressed.loc[0, "net_return"] == pytest.approx(0.009)
    assert stressed.loc[1, "net_return"] == pytest.approx(0.0002)
    assert stressed.loc[2, "gross_return"] == pytest.approx(0.0002)
    assert stressed.loc[1:, "turnover"].eq(0.0).all()
    assert stressed.loc[1:, "cost"].eq(0.0).all()


def test_weight_concentration_reports_effective_asset_count() -> None:
    dates = pd.date_range("2026-01-01", periods=2, freq="D", tz="UTC")
    rows = []
    for date in dates:
        row = {"date": date, "strategy": B3}
        for asset in ALL_ASSETS:
            row[asset] = 0.0
        row["US_EQUITY"] = 0.25
        row["GOLD"] = 0.25
        row["BTC"] = 0.25
        row["WTI"] = 0.25
        rows.append(row)
    result = _weight_concentration(pd.DataFrame(rows), B3)
    assert result["average_risk_gross"] == pytest.approx(1.0)
    assert result["max_risk_gross"] == pytest.approx(1.0)
    assert result["average_effective_asset_count"] == pytest.approx(4.0)
    assert result["min_effective_asset_count"] == pytest.approx(4.0)


def test_benchmark_stress_window_matches_peak_to_trough_drawdown() -> None:
    dates = pd.date_range("2026-01-01", periods=7, freq="D", tz="UTC")
    b1_returns = [0.10, -0.10, -0.10, 0.05, 0.20, 0.00, 0.00]
    b3_returns = [0.02, -0.02, -0.01, 0.01, 0.03, 0.00, 0.00]
    rows = []
    for strategy, values in ((B1, b1_returns), (B3, b3_returns)):
        for date, value in zip(dates, values, strict=True):
            rows.append(
                {
                    "date": date,
                    "strategy": strategy,
                    "gross_return": value,
                    "turnover": 0.0,
                    "cost": 0.0,
                    "net_return": value,
                    "cash_return": 0.0,
                }
            )
    result = _benchmark_stress_comparison(pd.DataFrame(rows), top_n=1)
    assert len(result) == 1
    # Peak wealth is after +10%; trough is two subsequent -10% days.
    expected = (1.0 - 0.10) * (1.0 - 0.10) - 1.0
    assert result.loc[0, "benchmark_drawdown"] == pytest.approx(expected)
    assert result.loc[0, "B1_period_return"] == pytest.approx(expected)
    assert result.loc[0, "B3_period_return"] == pytest.approx((1.0 - 0.02) * (1.0 - 0.01) - 1.0)
    assert bool(result.loc[0, "B3_outperformed"])


def test_all_risk_assets_are_present_in_concentration_contract() -> None:
    assert set(RISK_ASSETS) == {
        "US_EQUITY",
        "US_GROWTH",
        "GOLD",
        "SILVER",
        "COPPER",
        "WTI",
        "BTC",
        "ETH",
    }
