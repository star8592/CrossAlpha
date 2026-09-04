from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from crossalpha.core.free_baselines import (
    ALL_ASSETS,
    RISK_ASSETS,
    SLEEVES,
    STRATEGIES,
    FreeBaselineConfig,
    _apply_constraints,
    _hrp_weights,
    _scale_to_target_vol,
    run_free_baselines,
)


def _returns_path(root: Path, start: str, end: str) -> Path:
    return (
        root
        / "derived"
        / "core"
        / "free_v01"
        / f"start={start}"
        / f"end={end}"
        / "asset_returns.parquet"
    )


def _write_synthetic_returns(
    root: Path,
    *,
    start: str = "2024-01-01",
    periods: int = 520,
    future_shock: float = 0.0,
) -> tuple[str, str]:
    start_ts = pd.Timestamp(start, tz="UTC")
    dates = pd.date_range(start_ts, periods=periods, freq="D")
    end_ts = dates[-1] + pd.Timedelta(days=1)
    rows: list[dict[str, object]] = []

    base_returns = {
        "US_EQUITY": 0.0005,
        "US_GROWTH": 0.0006,
        "GOLD": 0.0003,
        "SILVER": 0.0002,
        "COPPER": 0.0001,
        "WTI": -0.0001,
        "BTC": 0.0008,
        "ETH": 0.0007,
    }
    for asset_index, asset in enumerate(RISK_ASSETS):
        for i, date in enumerate(dates):
            wave = 0.002 * np.sin((i + asset_index) / 13.0)
            value = base_returns[asset] + wave
            if i >= 470 and asset == "BTC":
                value += future_shock
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

    start_value = start_ts.strftime("%Y-%m-%d")
    end_value = end_ts.strftime("%Y-%m-%d")
    path = _returns_path(root, start_value, end_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(path, index=False)
    return start_value, end_value


def test_constraints_never_break_caps_or_budget() -> None:
    config = FreeBaselineConfig()
    raw = pd.Series(1.0, index=RISK_ASSETS)
    weights = _apply_constraints(raw, config)

    assert weights.sum() == pytest.approx(1.0)
    assert (weights.loc[list(RISK_ASSETS)] >= 0).all()
    assert (weights.loc[list(RISK_ASSETS)] <= config.single_asset_max + 1e-12).all()
    for assets, cap in SLEEVES.values():
        assert weights.loc[list(assets)].sum() <= cap + 1e-12
    assert weights["CASH"] >= 0


def test_copper_and_oil_share_the_preregistered_35pct_sleeve() -> None:
    config = FreeBaselineConfig()
    raw = pd.Series(0.0, index=RISK_ASSETS)
    raw["COPPER"] = 1.0
    raw["WTI"] = 1.0
    weights = _apply_constraints(raw, config)

    assert weights["COPPER"] <= 0.25 + 1e-12
    assert weights["WTI"] <= 0.25 + 1e-12
    assert weights[["COPPER", "WTI"]].sum() == pytest.approx(0.35)
    assert weights["CASH"] == pytest.approx(0.65)


def test_target_vol_only_derisks_and_never_leverages() -> None:
    config = FreeBaselineConfig(target_vol=0.10, vol_window_days=63)
    weights = pd.Series(0.0, index=ALL_ASSETS)
    weights["BTC"] = 0.25
    weights["ETH"] = 0.10
    weights["CASH"] = 0.65
    dates = pd.date_range("2026-01-01", periods=63, freq="D", tz="UTC")
    history = pd.DataFrame(
        {
            "BTC": np.where(np.arange(63) % 2 == 0, 0.08, -0.08),
            "ETH": np.where(np.arange(63) % 2 == 0, -0.07, 0.07),
        },
        index=dates,
    )

    scaled = _scale_to_target_vol(weights, history, config)
    assert scaled["BTC"] <= weights["BTC"]
    assert scaled["ETH"] <= weights["ETH"]
    assert scaled["CASH"] >= weights["CASH"]
    assert scaled.sum() == pytest.approx(1.0)
    assert scaled.loc[list(RISK_ASSETS)].sum() <= weights.loc[list(RISK_ASSETS)].sum() + 1e-12


def test_hrp_weights_are_long_only_and_normalized() -> None:
    rng = np.random.default_rng(7)
    history = pd.DataFrame(
        rng.normal(0.0, 0.01, size=(200, 4)),
        columns=["US_EQUITY", "GOLD", "BTC", "WTI"],
    )
    weights = _hrp_weights(history)
    assert weights.sum() == pytest.approx(1.0)
    assert (weights >= 0).all()
    assert set(weights.index) == set(history.columns)


def test_baseline_run_writes_all_strategies_and_respects_budget(tmp_path: Path) -> None:
    start, end = _write_synthetic_returns(tmp_path)
    result = run_free_baselines(tmp_path, start=start, end=end)

    assert result["stage"] == "DESCRIPTIVE_BASELINES_ONLY"
    assert set(result["strategies"]) == set(STRATEGIES)
    weights = pd.read_parquet(result["weights"])
    sums = weights.loc[:, list(ALL_ASSETS)].sum(axis=1)
    assert np.allclose(sums.to_numpy(), 1.0)
    assert (weights.loc[:, list(RISK_ASSETS)] >= -1e-12).all().all()
    assert (weights.loc[:, list(RISK_ASSETS)] <= 0.25 + 1e-12).all().all()
    assert (weights[["COPPER", "WTI"]].sum(axis=1) <= 0.35 + 1e-12).all()


def test_future_shock_cannot_rewrite_past_weights(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    start, end = _write_synthetic_returns(left, future_shock=0.0)
    _write_synthetic_returns(right, future_shock=0.20)

    result_left = run_free_baselines(left, start=start, end=end)
    result_right = run_free_baselines(right, start=start, end=end)
    weights_left = pd.read_parquet(result_left["weights"])
    weights_right = pd.read_parquet(result_right["weights"])

    cutoff = pd.Timestamp(start, tz="UTC") + pd.Timedelta(days=450)
    left_past = weights_left.loc[pd.to_datetime(weights_left["date"], utc=True) < cutoff].reset_index(drop=True)
    right_past = weights_right.loc[pd.to_datetime(weights_right["date"], utc=True) < cutoff].reset_index(drop=True)
    pd.testing.assert_frame_equal(left_past, right_past)
