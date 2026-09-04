from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from crossalpha.core.free_baselines import RISK_ASSETS, FreeBaselineConfig, _load_daily_panel
from crossalpha.core.free_robustness import _run_scenario, run_free_robustness_stage1


def _write_synthetic_returns(root: Path, periods: int = 500) -> tuple[str, str]:
    dates = pd.date_range("2024-01-01", periods=periods, freq="D", tz="UTC")
    rows: list[dict[str, object]] = []
    for asset_index, asset in enumerate(RISK_ASSETS):
        for i, date in enumerate(dates):
            value = 0.0003 + asset_index * 0.00003 + 0.002 * np.sin((i + asset_index) / 17.0)
            rows.append(
                {
                    "date": date,
                    "economic_asset": asset,
                    "source": "synthetic",
                    "symbol": asset,
                    "price": 100.0,
                    "return": value,
                }
            )
    for date in dates:
        rows.append(
            {
                "date": date,
                "economic_asset": "CASH",
                "source": "synthetic",
                "symbol": "CASH",
                "price": None,
                "return": 0.00005,
            }
        )

    start = dates[0].strftime("%Y-%m-%d")
    end = (dates[-1] + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    path = (
        root
        / "derived"
        / "core"
        / "free_v01"
        / f"start={start}"
        / f"end={end}"
        / "asset_returns.parquet"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(path, index=False)
    return start, end


def test_leave_one_asset_out_really_removes_the_asset(tmp_path: Path) -> None:
    start, end = _write_synthetic_returns(tmp_path, periods=420)
    daily, available = _load_daily_panel(tmp_path, start, end)
    weights, _ = _run_scenario(
        daily,
        available,
        FreeBaselineConfig(),
        excluded_asset="BTC",
    )
    assert float(weights["BTC"].abs().max()) == 0.0


def test_robustness_stage_writes_expected_attack_matrix(tmp_path: Path) -> None:
    start, end = _write_synthetic_returns(tmp_path, periods=500)
    result = run_free_robustness_stage1(tmp_path, start=start, end=end)

    assert result["stage"] == "ROBUSTNESS_STAGE_1"
    assert result["data_cost_usd"] == 0
    assert result["scenario_count"] == 26
    assert set(result["scenario_types"]) == {
        "baseline",
        "parameter_neighborhood",
        "execution_delay",
        "double_cost",
        "leave_one_asset_out",
    }
    assert set(result["focus_screen"]) == {
        "B3_ABSOLUTE_TREND_EQUAL_WEIGHT",
        "B4_ABSOLUTE_TREND_INVERSE_VOLATILITY",
    }

    scenario = pd.read_parquet(result["scenario_metrics"])
    assert scenario["scenario"].nunique() == 26
    assert len(scenario) == 26 * 8
    assert (
        scenario.loc[
            (scenario["scenario_type"] == "leave_one_asset_out")
            & (scenario["excluded_asset"] == "BTC")
        ].shape[0]
        == 8
    )

    remove_best = pd.read_parquet(result["remove_best_years"])
    assert not remove_best.empty
    assert set(remove_best["removed_count"].unique()).issubset({1, 2, 3})
