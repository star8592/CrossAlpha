from __future__ import annotations

import json
import math
from dataclasses import asdict, replace
from itertools import combinations
from pathlib import Path
from statistics import NormalDist
from typing import Any

import numpy as np
import pandas as pd

from crossalpha.core.free_baselines import (
    ALL_ASSETS,
    FreeBaselineConfig,
    _load_daily_panel,
    _metrics,
    _safe_slug,
)
from crossalpha.core.free_robustness import FOCUS_STRATEGIES, _run_scenario


PARAMETER_GRID = tuple(
    (trend, vol)
    for trend in (270, 315, 365, 420, 455)
    for vol in (42, 63, 126)
)
BASELINE_LABEL = "trend_365_vol_63"
BOOTSTRAP_BLOCKS = (21, 63)
CSCV_SLICES = 8
WALK_FORWARD_TRAIN_YEARS = 4
SCREEN_BOOTSTRAP_PROB = 0.95
SCREEN_DSR_PROB = 0.95
SCREEN_PBO_MAX = 0.25
SCREEN_POSITIVE_WF_FOLDS = 0.60


def _output_root(data_root: Path, start: str, end: str) -> Path:
    return (
        data_root / "research" / "free_v01" / "robustness_stage2"
        / f"start={_safe_slug(start)}" / f"end={_safe_slug(end)}"
    )


def _candidate_family(
    daily: pd.DataFrame,
    available: pd.DataFrame,
) -> tuple[
    dict[str, FreeBaselineConfig],
    dict[str, pd.DataFrame],
    dict[str, pd.DataFrame],
]:
    configs: dict[str, FreeBaselineConfig] = {}
    weights: dict[str, pd.DataFrame] = {}
    returns: dict[str, pd.DataFrame] = {}
    baseline = FreeBaselineConfig()
    for trend, vol in PARAMETER_GRID:
        label = f"trend_{trend}_vol_{vol}"
        config = replace(baseline, trend_window_days=trend, vol_window_days=vol)
        scenario_weights, scenario_returns = _run_scenario(daily, available, config)
        configs[label] = config
        weights[label] = scenario_weights
        returns[label] = scenario_returns
    return configs, weights, returns


def _strategy_part(frame: pd.DataFrame, strategy: str) -> pd.DataFrame:
    part = frame.loc[frame["strategy"] == strategy].copy()
    part["date"] = pd.to_datetime(part["date"], utc=True)
    return part.sort_values("date").reset_index(drop=True)


def _excess(part: pd.DataFrame) -> pd.Series:
    return (
        pd.to_numeric(part["net_return"], errors="coerce").fillna(0.0)
        - pd.to_numeric(part["cash_return"], errors="coerce").fillna(0.0)
    )


def _daily_sharpe(values: pd.Series | np.ndarray) -> float:
    array = np.asarray(values, dtype=float)
    array = array[np.isfinite(array)]
    if len(array) < 2:
        return float("nan")
    std = float(np.std(array, ddof=1))
    return float(np.mean(array) / std) if std > 0 else float("nan")


def _annualized_sharpe(values: pd.Series | np.ndarray, annualization_days: int = 365) -> float:
    value = _daily_sharpe(values)
    return value * math.sqrt(annualization_days) if math.isfinite(value) else float("nan")


def _circular_block_bootstrap(
    values: pd.Series,
    *,
    block_size: int,
    replications: int,
    seed: int,
    annualization_days: int,
) -> dict[str, float | int]:
    array = np.asarray(values, dtype=float)
    array = array[np.isfinite(array)]
    n = len(array)
    if n < max(30, block_size):
        raise ValueError("insufficient observations for block bootstrap")
    if replications < 100:
        raise ValueError("bootstrap replications must be at least 100")
    rng = np.random.default_rng(seed)
    block_count = int(math.ceil(n / block_size))
    offsets = np.arange(block_size)
    out = np.empty(replications, dtype=float)
    for i in range(replications):
        starts = rng.integers(0, n, size=block_count)
        idx = ((starts[:, None] + offsets[None, :]) % n).reshape(-1)[:n]
        out[i] = _annualized_sharpe(array[idx], annualization_days)
    finite = out[np.isfinite(out)]
    if finite.size == 0:
        raise ValueError("bootstrap produced no finite Sharpe estimates")
    return {
        "block_size_days": block_size,
        "replications": replications,
        "observed_sharpe": _annualized_sharpe(array, annualization_days),
        "median_bootstrap_sharpe": float(np.median(finite)),
        "p025_sharpe": float(np.quantile(finite, 0.025)),
        "p975_sharpe": float(np.quantile(finite, 0.975)),
        "prob_sharpe_gt_0": float(np.mean(finite > 0.0)),
    }


def _expected_max_sharpe(sharpes: np.ndarray) -> float:
    finite = sharpes[np.isfinite(sharpes)]
    n = len(finite)
    if n <= 1:
        return 0.0
    sigma = float(np.std(finite, ddof=1))
    if sigma <= 0:
        return 0.0
    gamma = 0.5772156649015329
    normal = NormalDist()
    return sigma * (
        (1.0 - gamma) * normal.inv_cdf(1.0 - 1.0 / n)
        + gamma * normal.inv_cdf(1.0 - 1.0 / (n * math.e))
    )


def _deflated_sharpe(
    baseline_excess: pd.Series,
    candidate_daily_sharpes: np.ndarray,
) -> dict[str, float | int]:
    values = pd.Series(np.asarray(baseline_excess, dtype=float)).replace(
        [np.inf, -np.inf], np.nan
    ).dropna()
    t = int(len(values))
    if t < 30:
        raise ValueError("insufficient observations for DSR")
    sr = _daily_sharpe(values)
    sr0 = _expected_max_sharpe(candidate_daily_sharpes)
    skew = float(values.skew())
    kurtosis = float(values.kurt()) + 3.0
    denominator_sq = 1.0 - skew * sr + ((kurtosis - 1.0) / 4.0) * sr**2
    z = (sr - sr0) * math.sqrt(max(t - 1, 1)) / math.sqrt(max(denominator_sq, 1e-12))
    return {
        "observations": t,
        "trial_count": int(np.isfinite(candidate_daily_sharpes).sum()),
        "observed_daily_sharpe": sr,
        "observed_annualized_sharpe": sr * math.sqrt(365.0),
        "expected_max_daily_sharpe": sr0,
        "skew": skew,
        "kurtosis": kurtosis,
        "dsr_probability": float(NormalDist().cdf(z)),
    }


def _candidate_excess_matrix(
    candidate_returns: dict[str, pd.DataFrame],
    strategy: str,
    *,
    analysis_start: pd.Timestamp,
) -> pd.DataFrame:
    columns: dict[str, pd.Series] = {}
    for label, frame in candidate_returns.items():
        part = _strategy_part(frame, strategy)
        columns[label] = pd.Series(_excess(part).to_numpy(), index=part["date"], name=label)
    return pd.DataFrame(columns).sort_index().loc[analysis_start:].fillna(0.0)


def _cscv_pbo(
    matrix: pd.DataFrame,
    *,
    slices: int = CSCV_SLICES,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if slices % 2 or slices < 4:
        raise ValueError("CSCV slices must be even and at least 4")
    if len(matrix) < slices * 30:
        raise ValueError("insufficient observations for CSCV")
    blocks = [np.asarray(x, dtype=int) for x in np.array_split(np.arange(len(matrix)), slices)]
    labels = list(matrix.columns)
    values = matrix.to_numpy(dtype=float)
    rows: list[dict[str, Any]] = []
    for split_id, is_blocks in enumerate(combinations(range(slices), slices // 2)):
        is_set = set(is_blocks)
        oos_blocks = tuple(i for i in range(slices) if i not in is_set)
        is_idx = np.concatenate([blocks[i] for i in is_blocks])
        oos_idx = np.concatenate([blocks[i] for i in oos_blocks])
        is_scores = np.array([_daily_sharpe(values[is_idx, j]) for j in range(len(labels))])
        selected = int(np.argmax(np.where(np.isfinite(is_scores), is_scores, -np.inf)))
        oos_scores = np.array([_daily_sharpe(values[oos_idx, j]) for j in range(len(labels))])
        selected_score = oos_scores[selected]
        finite = np.isfinite(oos_scores)
        if not finite[selected] or int(finite.sum()) < 2:
            percentile = 0.5
        else:
            scores = oos_scores[finite]
            less = float(np.sum(scores < selected_score))
            equal = float(np.sum(np.isclose(scores, selected_score, rtol=1e-12, atol=1e-12)))
            percentile = (less + 0.5 * equal) / float(len(scores))
        percentile = min(max(percentile, 1e-6), 1.0 - 1e-6)
        logit = math.log(percentile / (1.0 - percentile))
        rows.append({
            "split_id": split_id,
            "is_blocks": ",".join(map(str, is_blocks)),
            "oos_blocks": ",".join(map(str, oos_blocks)),
            "selected_candidate": labels[selected],
            "is_daily_sharpe": float(is_scores[selected]),
            "oos_daily_sharpe": float(selected_score),
            "oos_rank_percentile": percentile,
            "logit_rank": logit,
            "overfit": bool(logit < 0.0),
        })
    frame = pd.DataFrame(rows)
    return frame, {
        "slices": slices,
        "split_count": int(len(frame)),
        "candidate_count": int(len(labels)),
        "pbo": float(frame["overfit"].mean()),
        "median_logit_rank": float(frame["logit_rank"].median()),
        "median_oos_daily_sharpe": float(frame["oos_daily_sharpe"].median()),
    }


def _returns_from_weights(
    daily: pd.DataFrame,
    weights: pd.DataFrame,
    *,
    strategy: str,
    cost_bps: float = 5.0,
) -> pd.DataFrame:
    part = weights.copy()
    part["date"] = pd.to_datetime(part["date"], utc=True)
    part = part.sort_values("date").drop_duplicates("date", keep="last").set_index("date")
    w = part.loc[:, list(ALL_ASSETS)].astype(float)
    market = daily.reindex(w.index).loc[:, list(ALL_ASSETS)].fillna(0.0)
    gross = (w * market).sum(axis=1)
    previous = w.shift(1)
    if len(w):
        initial = pd.Series(0.0, index=ALL_ASSETS, dtype=float)
        initial.loc["CASH"] = 1.0
        previous.iloc[0] = initial
    turnover = (w - previous).abs().sum(axis=1).fillna(0.0) * 0.5
    cost = turnover * (cost_bps / 10_000.0)
    return pd.DataFrame({
        "date": w.index,
        "strategy": strategy,
        "gross_return": gross.to_numpy(),
        "turnover": turnover.to_numpy(),
        "cost": cost.to_numpy(),
        "net_return": (gross - cost).to_numpy(),
        "cash_return": market["CASH"].to_numpy(),
    })


def _walk_forward(
    daily: pd.DataFrame,
    candidate_weights: dict[str, pd.DataFrame],
    candidate_returns: dict[str, pd.DataFrame],
    strategy: str,
    *,
    start: str,
    end: str,
    train_years: int = WALK_FORWARD_TRAIN_YEARS,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC")
    first_test_year = max(start_ts.year + train_years + 1, 2015)
    return_parts = {k: _strategy_part(v, strategy) for k, v in candidate_returns.items()}
    weight_parts = {k: _strategy_part(v, strategy) for k, v in candidate_weights.items()}
    selections: list[dict[str, Any]] = []
    selected_weights: list[pd.DataFrame] = []
    frozen_weights: list[pd.DataFrame] = []

    for year in range(first_test_year, end_ts.year + 1):
        test_start = pd.Timestamp(f"{year}-01-01", tz="UTC")
        test_end = min(pd.Timestamp(f"{year + 1}-01-01", tz="UTC"), end_ts)
        if test_start >= end_ts or (test_end - test_start).days < 90:
            continue
        train_start = test_start - pd.DateOffset(years=train_years)
        train_scores: dict[str, float] = {}
        for label, part in return_parts.items():
            train = part.loc[(part["date"] >= train_start) & (part["date"] < test_start)]
            train_scores[label] = _annualized_sharpe(_excess(train)) if len(train) >= 180 else float("nan")
        finite = {k: v for k, v in train_scores.items() if math.isfinite(v)}
        if not finite:
            continue
        selected = max(finite, key=finite.get)
        chosen = weight_parts[selected].loc[
            (weight_parts[selected]["date"] >= test_start)
            & (weight_parts[selected]["date"] < test_end)
        ].copy()
        frozen = weight_parts[BASELINE_LABEL].loc[
            (weight_parts[BASELINE_LABEL]["date"] >= test_start)
            & (weight_parts[BASELINE_LABEL]["date"] < test_end)
        ].copy()
        if len(chosen) < 90 or len(frozen) < 90:
            continue
        selected_weights.append(chosen)
        frozen_weights.append(frozen)
        selections.append({
            "strategy": strategy,
            "test_year": year,
            "train_start": train_start,
            "train_end": test_start,
            "test_start": test_start,
            "test_end": test_end,
            "selected_candidate": selected,
            "selected_train_sharpe": finite[selected],
        })

    if not selections:
        raise ValueError(f"walk-forward produced no folds for {strategy}")
    selected_w = pd.concat(selected_weights, ignore_index=True)
    frozen_w = pd.concat(frozen_weights, ignore_index=True)
    selected_returns = _returns_from_weights(daily, selected_w, strategy=strategy)
    frozen_returns = _returns_from_weights(daily, frozen_w, strategy=strategy)

    fold_rows: list[dict[str, Any]] = []
    for item in selections:
        selected_test = selected_returns.loc[
            (selected_returns["date"] >= item["test_start"])
            & (selected_returns["date"] < item["test_end"])
        ]
        frozen_test = frozen_returns.loc[
            (frozen_returns["date"] >= item["test_start"])
            & (frozen_returns["date"] < item["test_end"])
        ]
        selected_metrics = _metrics(selected_test, 365)
        frozen_metrics = _metrics(frozen_test, 365)
        fold_rows.append({
            **{k: (v.isoformat() if isinstance(v, pd.Timestamp) else v) for k, v in item.items()},
            "selected_oos_sharpe": selected_metrics["sharpe_excess_cash"],
            "selected_oos_cagr": selected_metrics["cagr"],
            "selected_oos_max_drawdown": selected_metrics["max_drawdown"],
            "frozen_oos_sharpe": frozen_metrics["sharpe_excess_cash"],
            "frozen_oos_cagr": frozen_metrics["cagr"],
            "frozen_oos_max_drawdown": frozen_metrics["max_drawdown"],
        })
    folds = pd.DataFrame(fold_rows)
    positive = pd.to_numeric(folds["selected_oos_sharpe"], errors="coerce").dropna()
    return folds, {
        "strategy": strategy,
        "train_years": train_years,
        "fold_count": int(len(folds)),
        "positive_selected_oos_fold_share": float((positive > 0).mean()) if not positive.empty else 0.0,
        "selected_parameter_oos": _metrics(selected_returns, 365),
        "frozen_parameter_oos": _metrics(frozen_returns, 365),
        "switch_turnover_and_cost_included": True,
    }


def run_free_robustness_stage2(
    data_root: Path,
    *,
    start: str,
    end: str,
    bootstrap_replications: int = 2000,
    seed: int = 8592,
) -> dict[str, Any]:
    daily, available = _load_daily_panel(data_root, start, end)
    configs, candidate_weights, candidate_returns = _candidate_family(daily, available)
    analysis_start = daily.index.min() + pd.Timedelta(days=max(t for t, _ in PARAMETER_GRID))
    bootstrap_rows: list[dict[str, Any]] = []
    dsr_rows: list[dict[str, Any]] = []
    pbo_rows: list[pd.DataFrame] = []
    pbo_summary_rows: list[dict[str, Any]] = []
    wf_rows: list[pd.DataFrame] = []
    wf_summary: dict[str, Any] = {}
    screens: dict[str, Any] = {}

    for strategy_index, strategy in enumerate(FOCUS_STRATEGIES):
        matrix = _candidate_excess_matrix(candidate_returns, strategy, analysis_start=analysis_start)
        baseline_excess = matrix[BASELINE_LABEL]
        for block in BOOTSTRAP_BLOCKS:
            bootstrap_rows.append({
                "strategy": strategy,
                **_circular_block_bootstrap(
                    baseline_excess,
                    block_size=block,
                    replications=bootstrap_replications,
                    seed=seed + strategy_index * 100 + block,
                    annualization_days=365,
                ),
            })
        candidate_daily_sharpes = np.array(
            [_daily_sharpe(matrix[c].to_numpy()) for c in matrix.columns], dtype=float
        )
        dsr = _deflated_sharpe(baseline_excess, candidate_daily_sharpes)
        dsr_rows.append({"strategy": strategy, **dsr})
        splits, pbo = _cscv_pbo(matrix)
        splits.insert(0, "strategy", strategy)
        pbo_rows.append(splits)
        pbo_summary_rows.append({"strategy": strategy, **pbo})
        folds, wf = _walk_forward(
            daily,
            candidate_weights,
            candidate_returns,
            strategy,
            start=start,
            end=end,
        )
        wf_rows.append(folds)
        wf_summary[strategy] = wf

        boot63 = next(
            x for x in bootstrap_rows
            if x["strategy"] == strategy and x["block_size_days"] == 63
        )
        selected_oos = wf["selected_parameter_oos"]["sharpe_excess_cash"]
        survives = bool(
            boot63["prob_sharpe_gt_0"] >= SCREEN_BOOTSTRAP_PROB
            and dsr["dsr_probability"] >= SCREEN_DSR_PROB
            and pbo["pbo"] <= SCREEN_PBO_MAX
            and selected_oos is not None
            and selected_oos > 0
            and wf["positive_selected_oos_fold_share"] >= SCREEN_POSITIVE_WF_FOLDS
        )
        screens[strategy] = {
            "survives_stage2": survives,
            "bootstrap_63d_prob_sharpe_gt_0": boot63["prob_sharpe_gt_0"],
            "dsr_probability": dsr["dsr_probability"],
            "pbo": pbo["pbo"],
            "walk_forward_selected_oos_sharpe": selected_oos,
            "walk_forward_positive_fold_share": wf["positive_selected_oos_fold_share"],
        }

    bootstrap = pd.DataFrame(bootstrap_rows)
    dsr_frame = pd.DataFrame(dsr_rows)
    pbo_splits = pd.concat(pbo_rows, ignore_index=True)
    pbo_summary = pd.DataFrame(pbo_summary_rows)
    walk_forward_folds = pd.concat(wf_rows, ignore_index=True)
    output_root = _output_root(data_root, start, end)
    output_root.mkdir(parents=True, exist_ok=True)
    paths = {
        "bootstrap": output_root / "block_bootstrap.parquet",
        "dsr": output_root / "deflated_sharpe.parquet",
        "pbo_splits": output_root / "pbo_splits.parquet",
        "pbo_summary": output_root / "pbo_summary.parquet",
        "walk_forward_folds": output_root / "walk_forward_folds.parquet",
    }
    bootstrap.to_parquet(paths["bootstrap"], index=False)
    dsr_frame.to_parquet(paths["dsr"], index=False)
    pbo_splits.to_parquet(paths["pbo_splits"], index=False)
    pbo_summary.to_parquet(paths["pbo_summary"], index=False)
    walk_forward_folds.to_parquet(paths["walk_forward_folds"], index=False)

    summary_path = output_root / "summary.json"
    summary = {
        "protocol": "CROSSALPHA_FREE_V0_1",
        "stage": "ROBUSTNESS_STAGE_2_STATISTICAL_FALSIFICATION",
        "data_cost_usd": 0,
        "start": start,
        "end": end,
        "analysis_start": analysis_start.isoformat(),
        "parameter_family_count": len(PARAMETER_GRID),
        "parameter_grid": [
            {"trend_window_days": trend, "vol_window_days": vol}
            for trend, vol in PARAMETER_GRID
        ],
        "baseline_config": asdict(configs[BASELINE_LABEL]),
        "bootstrap_replications": bootstrap_replications,
        "bootstrap_blocks_days": list(BOOTSTRAP_BLOCKS),
        "cscv_slices": CSCV_SLICES,
        "walk_forward_train_years": WALK_FORWARD_TRAIN_YEARS,
        "screen_thresholds": {
            "bootstrap_63d_prob_sharpe_gt_0_min": SCREEN_BOOTSTRAP_PROB,
            "dsr_probability_min": SCREEN_DSR_PROB,
            "pbo_max": SCREEN_PBO_MAX,
            "positive_walk_forward_fold_share_min": SCREEN_POSITIVE_WF_FOLDS,
            "walk_forward_selected_oos_sharpe_must_be_positive": True,
        },
        "focus_screen": screens,
        "walk_forward": wf_summary,
        "interpretation": (
            "Stage 2 is a pre-registered falsification screen. DSR is an explicit "
            "Bailey/Lopez de Prado-style approximation over the frozen 15-member parameter family; "
            "CSCV/PBO uses contiguous slices. Walk-forward charges turnover when the selected "
            "parameter set changes. Passing is not final proof of alpha."
        ),
        **{name: str(path) for name, path in paths.items()},
    }
    tmp = summary_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(summary_path)
    return {**summary, "summary": str(summary_path)}
