from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crossalpha.core.free_baselines import ALL_ASSETS
from crossalpha.core.free_robustness_stage2 import (
    BASELINE_LABEL,
    _circular_block_bootstrap,
    _cscv_pbo,
    _deflated_sharpe,
    _returns_from_weights,
    _walk_forward,
)


def test_block_bootstrap_is_deterministic_and_preserves_positive_edge() -> None:
    rng = np.random.default_rng(5)
    values = pd.Series(rng.normal(0.0006, 0.01, 1200))
    left = _circular_block_bootstrap(
        values,
        block_size=21,
        replications=200,
        seed=8592,
        annualization_days=365,
    )
    right = _circular_block_bootstrap(
        values,
        block_size=21,
        replications=200,
        seed=8592,
        annualization_days=365,
    )
    assert left == right
    assert left["prob_sharpe_gt_0"] > 0.5
    assert left["p025_sharpe"] <= left["median_bootstrap_sharpe"] <= left["p975_sharpe"]


def test_deflated_sharpe_accounts_for_trial_family() -> None:
    rng = np.random.default_rng(9)
    excess = pd.Series(rng.normal(0.0010, 0.01, 1500))
    candidate_daily_sharpes = np.array([0.01, 0.015, 0.02, 0.025, 0.03])
    result = _deflated_sharpe(excess, candidate_daily_sharpes)
    assert result["trial_count"] == 5
    assert 0.0 <= result["dsr_probability"] <= 1.0
    assert result["observed_daily_sharpe"] > result["expected_max_daily_sharpe"]
    assert result["dsr_probability"] > 0.5


def test_cscv_uses_all_70_half_splits_for_eight_slices() -> None:
    rng = np.random.default_rng(11)
    n = 800
    matrix = pd.DataFrame(
        {
            "stable": rng.normal(0.0008, 0.01, n),
            "weak1": rng.normal(0.0001, 0.01, n),
            "weak2": rng.normal(0.0, 0.01, n),
            "weak3": rng.normal(-0.0001, 0.01, n),
            "weak4": rng.normal(0.0002, 0.012, n),
        }
    )
    splits, summary = _cscv_pbo(matrix, slices=8)
    assert len(splits) == 70
    assert summary["split_count"] == 70
    assert summary["candidate_count"] == 5
    assert 0.0 <= summary["pbo"] <= 1.0


def test_returns_from_weights_charges_initial_and_switch_turnover() -> None:
    dates = pd.date_range("2026-01-01", periods=3, freq="D", tz="UTC")
    daily = pd.DataFrame(0.0, index=dates, columns=ALL_ASSETS)
    daily["US_EQUITY"] = [0.01, 0.00, 0.00]
    weights = pd.DataFrame(
        [
            {"date": dates[0], "US_EQUITY": 1.0, "CASH": 0.0},
            {"date": dates[1], "US_EQUITY": 0.0, "CASH": 1.0},
            {"date": dates[2], "US_EQUITY": 0.0, "CASH": 1.0},
        ]
    )
    for asset in ALL_ASSETS:
        if asset not in weights:
            weights[asset] = 0.0
    result = _returns_from_weights(daily, weights, strategy="B3", cost_bps=5.0)
    assert result.loc[0, "turnover"] == pytest.approx(1.0)
    assert result.loc[1, "turnover"] == pytest.approx(1.0)
    assert result.loc[2, "turnover"] == pytest.approx(0.0)
    assert result.loc[0, "cost"] == pytest.approx(0.0005)
    assert result.loc[1, "cost"] == pytest.approx(0.0005)


def _synthetic_candidate(
    dates: pd.DatetimeIndex,
    *,
    label_return: float,
    equity_weight: float,
    strategy: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    wave = 0.0002 * np.sin(np.arange(len(dates)) / 17.0)
    net = label_return + wave
    returns = pd.DataFrame(
        {
            "date": dates,
            "strategy": strategy,
            "gross_return": net,
            "turnover": 0.0,
            "cost": 0.0,
            "net_return": net,
            "cash_return": 0.0,
        }
    )
    weights = pd.DataFrame({"date": dates, "strategy": strategy})
    for asset in ALL_ASSETS:
        weights[asset] = 0.0
    weights["US_EQUITY"] = equity_weight
    weights["CASH"] = 1.0 - equity_weight
    return weights, returns


def test_walk_forward_returns_explicitly_include_parameter_switch_costs() -> None:
    dates = pd.date_range("2010-01-01", "2020-12-31", freq="D", tz="UTC")
    daily = pd.DataFrame(0.0, index=dates, columns=ALL_ASSETS)
    daily["US_EQUITY"] = 0.001 + 0.0002 * np.sin(np.arange(len(dates)) / 19.0)
    strategy = "B3_ABSOLUTE_TREND_EQUAL_WEIGHT"

    baseline_w, baseline_r = _synthetic_candidate(
        dates, label_return=0.0004, equity_weight=0.4, strategy=strategy
    )
    alt_w, alt_r = _synthetic_candidate(
        dates, label_return=0.0008, equity_weight=0.8, strategy=strategy
    )
    weights = {BASELINE_LABEL: baseline_w, "trend_270_vol_42": alt_w}
    returns = {BASELINE_LABEL: baseline_r, "trend_270_vol_42": alt_r}

    folds, summary = _walk_forward(
        daily,
        weights,
        returns,
        strategy,
        start="2010-01-01",
        end="2021-01-01",
        train_years=4,
    )
    assert not folds.empty
    assert summary["switch_turnover_and_cost_included"] is True
    assert summary["fold_count"] == len(folds)
    assert summary["selected_parameter_oos"]["total_cost"] > 0.0
