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
        data_root
        / "research"
        / "free_v01"
        / "robustness_stage2"
        / f"start={_safe_slug(start)}"
        / f"end={_safe_slug(end)}"
    )


def _candidate_family(
    daily: pd.DataFrame,
    available: pd.DataFrame,
) -> tuple[dict[str, FreeBaselineConfig], dict[str, pd.DataFrame]]:
    configs: dict[str, FreeBaselineConfig] = {}
    returns: dict[str, pd.DataFrame] = {}
    baseline = FreeBaselineConfig()
    for trend, vol in PARAMETER_GRID:
        label = f"trend_{trend}_vol_{vol}"
        config = replace(baseline, trend_window_days=trend, vol_window_days=vol)
        _, result = _run_scenario(daily, available, config)
        configs[label] = config
        returns[label] = result
    return configs, returns


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
    if std <= 0:
        return float("nan")
    return float(np.mean(array) / std)


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
    out = np.empty(replications, dtype=float)
    offsets = np.arange(block_size)
    for i in range(replications):
        starts = rng.integers(0, n, size=block_count)
        indices = ((starts[:, None] + offsets[None, :]) % n).reshape(-1)[:n]
        out[i] = _annualized_sharpe(array[indices], annualization_days)

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
    first = normal.inv_cdf(1.0 - 1.0 / n)
    second = normal.inv_cdf(1.0 - 1.0 / (n * math.e))
    return sigma * ((1.0 - gamma) * first + gamma * second)


def _deflated_sharpe(
    baseline_excess: pd.Series,
    candidate_daily_sharpes: np.ndarray,
) -> dict[str, float | int]:
    values = pd.Series(np.asarray(baseline_excess, dtype=float)).replace([np.inf, -np.inf], np.nan).dropna()
    t = int(len(values))
    if t < 30:
        raise ValueError("insufficient observations for DSR")
    sr = _daily_sharpe(values)
    sr0 = _expected_max_sharpe(candidate_daily_sharpes)
    skew = float(values.skew())
    kurtosis = float(values.kurt()) + 3.0
    denominator_sq = 1.0 - skew * sr + ((kurtosis - 1.0) / 4.0) * (sr**2)
    denominator = math.sqrt(max(denominator_sq, 1e-12))
    z = (sr - sr0) * math.sqrt(max(t - 1, 1)) / denominator
    probability = NormalDist().cdf(z)
    return {
        "observations": t,
        "trial_count": int(np.isfinite(candidate_daily_sharpes).sum()),
        "observed_daily_sharpe": sr,
        "observed_annualized_sharpe": sr * math.sqrt(365.0),
        "expected_max_daily_sharpe": sr0,
        "skew": skew,
        "kurtosis": kurtosis,
        "dsr_probability": float(probability),
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
        series = pd.Series(_excess(part).to_numpy(), index=part["date"], name=label)
        columns[label] = series
    matrix = pd.DataFrame(columns).sort_index()
    matrix = matrix.loc[matrix.index >= analysis_start].fillna(0.0)
    return matrix


def _cscv_pbo(matrix: pd.DataFrame, *, slices: int = CSCV_SLICES) -> tuple[pd.DataFrame, dict[str, Any]]:
    if slices % 2 != 0 or slices < 4:
        raise ValueError("CSCV slices must be even and at least 4")
    if len(matrix) < slices * 30:
        raise ValueError("insufficient observations for CSCV")

    blocks = [np.asarray(block, dtype=int) for block in np.array_split(np.arange(len(matrix)), slices)]
    half = slices // 2
    labels = list(matrix.columns)
    values = matrix.to_numpy(dtype=float)
    rows: list[dict[str, Any]] = []

    for split_id, is_blocks in enumerate(combinations(range(slices), half)):
        is_set = set(is_blocks)
        oos_blocks = tuple(i for i in range(slices) if i not in is_set)
        is_idx = np.concatenate([blocks[i] for i in is_blocks])
        oos_idx = np.concatenate([blocks[i] for i in oos_blocks])
        is_scores = np.array([_daily_sharpe(values[is_idx, j]) for j in range(len(labels))])
        safe_is = np.where(np.isfinite(is_scores), is_scores, -np.inf)
        selected = int(np.argmax(safe_is))
        oos_scores = np.array([_daily_sharpe(values[oos_idx, j]) for j in range(len(labels))])
        selected_score = oos_scores[selected]
        finite = np.isfinite(oos_scores)
        if not finite[selected] or int(finite.sum()) < 2:
            percentile = 0.5
        else:
            finite_scores = oos_scores[finite]
            less = float(np.sum(finite_scores < selected_score))
            equal = float(np.sum(np.isclose(finite_scores, selected_score, rtol=1e-12, atol=1e-12)))
            percentile = (less + 0.5 * equal) / float(len(finite_scores))
        percentile = min(max(percentile, 1e-6), 1.0 - 1e-6)
        logit = math.log(percentile / (1.0 - percentile))
        rows.append(
            {
                "split_id": split_id,
                "is_blocks": ",".join(map(str, is_blocks)),
                "oos_blocks": ",".join(map(str, oos_blocks)),
                "selected_candidate": labels[selected],
                "is_daily_sharpe": float(is_scores[selected]),
                "oos_daily_sharpe": float(selected_score),
                "oos_rank_percentile": percentile,
                "logit_rank": logit,
                "overfit": bool(logit < 0.0),
            }
        )

    frame = pd.DataFrame(rows)
    summary = {
        "slices": slices,
        "split_count": int(len(frame)),
        "candidate_count": int(len(labels)),
        "pbo": float(frame["overfit"].mean()),
        "median_logit_rank": float(frame["logit_rank"].median()),
        "median_oos_daily_sharpe": float(frame["oos_daily_sharpe"].median()),
    }
    return frame, summary


def _window_metrics(part: pd.DataFrame, annualization_days: int = 365) -> dict[str, Any]:
    if len(part) < 30:
        return {
            "days": int(len(part)),
            "cagr": None,
            "annualized_volatility": None,
            "sharpe_excess_cash": None,
            "max_drawdown": None,
        }
    result = _metrics(part, annualization_days)
    return {
        "days": result["days"],
        "cagr": result["cagr"],
        "annualized_volatility": result["annualized_volatility"],
        "sharpe_excess_cash": result["sharpe_excess_cash"],
        "max_drawdown": result["max_drawdown"],
    }


def _walk_forward(
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
    fold_rows: list[dict[str, Any]] = []
    selected_segments: list[pd.DataFrame] = []
    frozen_segments: list[pd.DataFrame] = []

    parts = {label: _strategy_part(frame, strategy) for label, frame in candidate_returns.items()}
    for year in range(first_test_year, end_ts.year + 1):
        test_start = pd.Timestamp(f"{year}-01-01", tz="UTC")
        test_end = min(pd.Timestamp(f"{year + 1}-01-01", tz="UTC"), end_ts)
        if test_start >= end_ts or (test_end - test_start).days < 90:
            continue
        train_start = test_start - pd.DateOffset(years=train_years)
        train_scores: dict[str, float] = {}
        for label, part in parts.items():
            train = part.loc[(part["date"] >= train_start) & (part["date"] < test_start)]
            train_scores[label] = _annualized_sharpe(_excess(train)) if len(train) >= 180 else float("nan")
        finite = {k: v for k, v in train_scores.items() if math.isfinite(v)}
        if not finite:
            continue
        selected = max(finite, key=finite.get)
        selected_test = parts[selected].loc[
            (parts[selected]["date"] >= test_start) & (parts[selected]["date"] < test_end)
        ].copy()
        frozen_test = parts[BASELINE_LABEL].loc[
            (parts[BASELINE_LABEL]["date"] >= test_start)
            & (parts[BASELINE_LABEL]["date"] < test_end)
        ].copy()
        if len(selected_test) < 90 or len(frozen_test) < 90:
            continue
        selected_segments.append(selected_test)
        frozen_segments.append(frozen_test)
        selected_metrics = _window_metrics(selected_test)
        frozen_metrics = _window_metrics(frozen_test)
        fold_rows.append(
            {
                "strategy": strategy,
                "test_year": year,
                "train_start": train_start.isoformat(),
                "train_end": test_start.isoformat(),
                "test_start": test_start.isoformat(),
                "test_end": test_end.isoformat(),
                "selected_candidate": selected,
                "selected_train_sharpe": finite[selected],
                "selected_oos_sharpe": selected_metrics["sharpe_excess_cash"],
                "selected_oos_cagr": selected_metrics["cagr"],
                "selected_oos_max_drawdown": selected_metrics["max_drawdown"],
                "frozen_oos_sharpe": frozen_metrics["sharpe_excess_cash"],
                "frozen_oos_cagr": frozen_metrics["cagr"],
                "frozen_oos_max_drawdown": frozen_metrics["max_drawdown"],
            }
        )

    folds = pd.DataFrame(fold_rows)
    if not selected_segments:
        raise ValueError(f"walk-forward produced no folds for {strategy}")
    selected_all = pd.concat(selected_segments, ignore_index=True)
    frozen_all = pd.concat(frozen_segments, ignore_index=True)
    selected_metrics = _metrics(selected_all, 365)
    frozen_metrics = _metrics(frozen_all, 365)
    positive = pd.to_numeric(folds["selected_oos_sharpe"], errors="coerce").dropna()
    summary = {
        "strategy": strategy,
        "train_years": train_years,
        "fold_count": int(len(folds)),
        "positive_selected_oos_fold_share": float((positive > 0).mean()) if not positive.empty else 0.0,
        "selected_parameter_oos": selected_metrics,
        "frozen_parameter_oos": frozen_metrics,
    }
    return folds, summary


def run_free_robustness_stage2(
    data_root: Path,
    *,
    start: str,
    end: str,
    bootstrap_replications: int = 2000,
    seed: int = 8592,
) -> dict[str, Any]:
    daily, available = _load_daily_panel(data_root, start, end)
    configs, candidate_returns = _candidate_family(daily, available)
    baseline_returns = candidate_returns[BASELINE_LABEL]
    analysis_start = daily.index.min() + pd.Timedelta(days=max(t for t, _ in PARAMETER_GRID))

    bootstrap_rows: list[dict[str, Any]] = []
    dsr_rows: list[dict[str, Any]] = []
    pbo_rows: list[pd.DataFrame] = []
    pbo_summary_rows: list[dict[str, Any]] = []
    wf_rows: list[pd.DataFrame] = []
    wf_summary: dict[str, Any] = {}
    screens: dict[str, Any] = {}

    for strategy_index, strategy in enumerate(FOCUS_STRATEGIES):
        baseline_part = _strategy_part(baseline_returns, strategy)
        baseline_part = baseline_part.loc[baseline_part["date"] >= analysis_start]
        baseline_excess = _excess(baseline_part)

        for block in BOOTSTRAP_BLOCKS:
            row = _circular_block_bootstrap(
                baseline_excess,
                block_size=block,
                replications=bootstrap_replications,
                seed=seed + strategy_index * 100 + block,
                annualization_days=365,
            )
            bootstrap_rows.append({"strategy": strategy, **row})

        matrix = _candidate_excess_matrix(
            candidate_returns,
            strategy,
            analysis_start=analysis_start,
        )
        candidate_daily_sharpes = np.array(
            [_daily_sharpe(matrix[column].to_numpy()) for column in matrix.columns],
            dtype=float,
        )
        dsr = _deflated_sharpe(matrix[BASELINE_LABEL], candidate_daily_sharpes)
        dsr_rows.append({"strategy": strategy, **dsr})

        splits, pbo = _cscv_pbo(matrix)
        splits.insert(0, "strategy", strategy)
        pbo_rows.append(splits)
        pbo_summary_rows.append({"strategy": strategy, **pbo})

        folds, wf = _walk_forward(
            candidate_returns,
            strategy,
            start=start,
            end=end,
        )
        wf_rows.append(folds)
        wf_summary[strategy] = wf

        boot63 = next(
            row for row in bootstrap_rows
            if row["strategy"] == strategy and row["block_size_days"] == 63
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
    dsr = pd.DataFrame(dsr_rows)
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
    dsr.to_parquet(paths["dsr"], index=False)
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
            "CSCV/PBO uses contiguous slices. Passing is not final proof of alpha."
        ),
        **{name: str(path) for name, path in paths.items()},
    }
    tmp = summary_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(summary_path)
    return {**summary, "summary": str(summary_path)}
