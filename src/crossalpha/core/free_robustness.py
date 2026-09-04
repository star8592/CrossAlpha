from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pandas as pd

from crossalpha.core.free_baselines import (
    RISK_ASSETS,
    STRATEGIES,
    FreeBaselineConfig,
    _build_weight_history,
    _compute_strategy_returns,
    _load_daily_panel,
    _metrics,
    _safe_slug,
)


FOCUS_STRATEGIES = (
    "B3_ABSOLUTE_TREND_EQUAL_WEIGHT",
    "B4_ABSOLUTE_TREND_INVERSE_VOLATILITY",
)


def _output_root(data_root: Path, start: str, end: str) -> Path:
    return (
        data_root
        / "research"
        / "free_v01"
        / "robustness_stage1"
        / f"start={_safe_slug(start)}"
        / f"end={_safe_slug(end)}"
    )


def _run_scenario(
    daily: pd.DataFrame,
    available: pd.DataFrame,
    config: FreeBaselineConfig,
    *,
    excluded_asset: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    scenario_daily = daily.copy()
    scenario_available = available.copy()
    if excluded_asset is not None:
        if excluded_asset not in RISK_ASSETS:
            raise ValueError(f"unknown excluded asset: {excluded_asset}")
        scenario_available.loc[:, excluded_asset] = False
        scenario_daily.loc[:, excluded_asset] = float("nan")

    weights = _build_weight_history(scenario_daily, scenario_available, config)
    returns = _compute_strategy_returns(scenario_daily, weights, config)
    return weights, returns


def _scenario_metric_rows(
    returns: pd.DataFrame,
    *,
    scenario_type: str,
    scenario: str,
    config: FreeBaselineConfig,
    excluded_asset: str | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for strategy in STRATEGIES:
        part = returns.loc[returns["strategy"] == strategy]
        metrics = _metrics(part, config.annualization_days)
        rows.append(
            {
                "scenario_type": scenario_type,
                "scenario": scenario,
                "strategy": strategy,
                "excluded_asset": excluded_asset,
                "cost_bps": config.cost_bps,
                "execution_lag_days": config.execution_lag_days,
                "vol_window_days": config.vol_window_days,
                "trend_window_days": config.trend_window_days,
                "target_vol": config.target_vol,
                **metrics,
            }
        )
    return rows


def _year_metric_rows(returns: pd.DataFrame, annualization_days: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    frame = returns.copy()
    frame["date"] = pd.to_datetime(frame["date"], utc=True)
    frame["year"] = frame["date"].dt.year
    for (strategy, year), part in frame.groupby(["strategy", "year"], sort=True):
        if len(part) < 30:
            continue
        metrics = _metrics(part, annualization_days)
        rows.append({"strategy": strategy, "year": int(year), **metrics})
    return rows


def _era_metric_rows(returns: pd.DataFrame, annualization_days: int) -> list[dict[str, Any]]:
    frame = returns.copy()
    frame["date"] = pd.to_datetime(frame["date"], utc=True)
    eras = (
        ("2010_2014", "2010-06-01", "2015-01-01"),
        ("2015_2019", "2015-01-01", "2020-01-01"),
        ("2020_2022", "2020-01-01", "2023-01-01"),
        ("2023_2026", "2023-01-01", "2026-09-01"),
    )
    rows: list[dict[str, Any]] = []
    for label, start, end in eras:
        start_ts = pd.Timestamp(start, tz="UTC")
        end_ts = pd.Timestamp(end, tz="UTC")
        era = frame.loc[(frame["date"] >= start_ts) & (frame["date"] < end_ts)]
        for strategy in STRATEGIES:
            part = era.loc[era["strategy"] == strategy]
            if len(part) < 30:
                continue
            rows.append(
                {
                    "era": label,
                    "strategy": strategy,
                    **_metrics(part, annualization_days),
                }
            )
    return rows


def _remove_best_years_rows(
    returns: pd.DataFrame,
    annualization_days: int,
    *,
    max_remove: int = 3,
) -> list[dict[str, Any]]:
    frame = returns.copy()
    frame["date"] = pd.to_datetime(frame["date"], utc=True)
    frame["year"] = frame["date"].dt.year
    rows: list[dict[str, Any]] = []

    for strategy in STRATEGIES:
        part = frame.loc[frame["strategy"] == strategy].copy()
        if strategy == "B0_CASH":
            continue
        yearly = (
            part.groupby("year")["net_return"]
            .apply(lambda values: float((1.0 + values).prod() - 1.0))
            .sort_values(ascending=False)
        )
        ranked_years = [int(value) for value in yearly.index]
        for remove_count in range(1, min(max_remove, len(ranked_years)) + 1):
            removed = ranked_years[:remove_count]
            stressed = part.copy()
            mask = stressed["year"].isin(removed)
            # Neutralize the strategy's excess return in its best years rather than
            # deleting time. This preserves chronology and the cash opportunity set.
            stressed.loc[mask, "net_return"] = stressed.loc[mask, "cash_return"]
            stressed.loc[mask, "gross_return"] = stressed.loc[mask, "cash_return"]
            stressed.loc[mask, "turnover"] = 0.0
            stressed.loc[mask, "cost"] = 0.0
            metrics = _metrics(stressed, annualization_days)
            rows.append(
                {
                    "strategy": strategy,
                    "removed_count": remove_count,
                    "removed_years": ",".join(str(year) for year in removed),
                    **metrics,
                }
            )
    return rows


def _focus_screen(
    scenario_metrics: pd.DataFrame,
    era_metrics: pd.DataFrame,
    remove_best: pd.DataFrame,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for strategy in FOCUS_STRATEGIES:
        part = scenario_metrics.loc[scenario_metrics["strategy"] == strategy].copy()
        sharpe = pd.to_numeric(part["sharpe_excess_cash"], errors="coerce").dropna()
        positive_share = float((sharpe > 0).mean()) if not sharpe.empty else 0.0
        median_sharpe = float(sharpe.median()) if not sharpe.empty else None
        worst_sharpe = float(sharpe.min()) if not sharpe.empty else None

        eras = era_metrics.loc[era_metrics["strategy"] == strategy]
        era_sharpe = pd.to_numeric(eras["sharpe_excess_cash"], errors="coerce").dropna()
        positive_eras = int((era_sharpe > 0).sum())

        stress = remove_best.loc[
            (remove_best["strategy"] == strategy)
            & (remove_best["removed_count"] == 1)
        ]
        after_best_year_sharpe = None
        if not stress.empty:
            value = pd.to_numeric(stress["sharpe_excess_cash"], errors="coerce").dropna()
            if not value.empty:
                after_best_year_sharpe = float(value.iloc[0])

        result[strategy] = {
            "scenario_count": int(len(part)),
            "positive_sharpe_share": positive_share,
            "median_scenario_sharpe": median_sharpe,
            "worst_scenario_sharpe": worst_sharpe,
            "positive_eras": positive_eras,
            "era_count": int(len(era_sharpe)),
            "sharpe_after_neutralizing_best_year": after_best_year_sharpe,
        }
    return result


def run_free_robustness_stage1(
    data_root: Path,
    *,
    start: str,
    end: str,
) -> dict[str, Any]:
    daily, available = _load_daily_panel(data_root, start, end)
    baseline = FreeBaselineConfig()

    scenario_rows: list[dict[str, Any]] = []
    scenario_returns: dict[str, pd.DataFrame] = {}

    # Frozen baseline.
    _, returns = _run_scenario(daily, available, baseline)
    scenario_returns["baseline"] = returns
    scenario_rows.extend(
        _scenario_metric_rows(
            returns,
            scenario_type="baseline",
            scenario="baseline",
            config=baseline,
        )
    )

    # Full parameter neighborhood: 5 trend windows x 3 volatility windows.
    for trend in (270, 315, 365, 420, 455):
        for vol in (42, 63, 126):
            if trend == baseline.trend_window_days and vol == baseline.vol_window_days:
                continue
            config = replace(baseline, trend_window_days=trend, vol_window_days=vol)
            label = f"trend_{trend}_vol_{vol}"
            _, returns = _run_scenario(daily, available, config)
            scenario_rows.extend(
                _scenario_metric_rows(
                    returns,
                    scenario_type="parameter_neighborhood",
                    scenario=label,
                    config=config,
                )
            )

    # Execution and transaction-cost attacks.
    for delay in (2, 3):
        config = replace(baseline, execution_lag_days=delay)
        label = f"delay_{delay}d"
        _, returns = _run_scenario(daily, available, config)
        scenario_rows.extend(
            _scenario_metric_rows(
                returns,
                scenario_type="execution_delay",
                scenario=label,
                config=config,
            )
        )

    config = replace(baseline, cost_bps=10.0)
    _, returns = _run_scenario(daily, available, config)
    scenario_rows.extend(
        _scenario_metric_rows(
            returns,
            scenario_type="double_cost",
            scenario="cost_10bps",
            config=config,
        )
    )

    # Asset dependence attack.
    for asset in RISK_ASSETS:
        _, returns = _run_scenario(daily, available, baseline, excluded_asset=asset)
        scenario_rows.extend(
            _scenario_metric_rows(
                returns,
                scenario_type="leave_one_asset_out",
                scenario=f"without_{asset}",
                config=baseline,
                excluded_asset=asset,
            )
        )

    baseline_returns = scenario_returns["baseline"]
    scenario_metrics = pd.DataFrame(scenario_rows)
    year_metrics = pd.DataFrame(_year_metric_rows(baseline_returns, baseline.annualization_days))
    era_metrics = pd.DataFrame(_era_metric_rows(baseline_returns, baseline.annualization_days))
    remove_best = pd.DataFrame(
        _remove_best_years_rows(baseline_returns, baseline.annualization_days)
    )
    focus_screen = _focus_screen(scenario_metrics, era_metrics, remove_best)

    output_root = _output_root(data_root, start, end)
    output_root.mkdir(parents=True, exist_ok=True)
    scenario_path = output_root / "scenario_metrics.parquet"
    year_path = output_root / "year_metrics.parquet"
    era_path = output_root / "era_metrics.parquet"
    remove_path = output_root / "remove_best_years.parquet"
    summary_path = output_root / "summary.json"

    scenario_metrics.to_parquet(scenario_path, index=False)
    year_metrics.to_parquet(year_path, index=False)
    era_metrics.to_parquet(era_path, index=False)
    remove_best.to_parquet(remove_path, index=False)

    summary = {
        "protocol": "CROSSALPHA_FREE_V0_1",
        "stage": "ROBUSTNESS_STAGE_1",
        "data_cost_usd": 0,
        "start": start,
        "end": end,
        "baseline_config": asdict(baseline),
        "scenario_count": int(scenario_metrics["scenario"].nunique()),
        "scenario_types": sorted(scenario_metrics["scenario_type"].unique().tolist()),
        "focus_screen": focus_screen,
        "interpretation": (
            "This stage is falsification screening, not a final support decision. "
            "Block bootstrap, DSR/PBO and formal walk-forward remain downstream."
        ),
        "scenario_metrics": str(scenario_path),
        "year_metrics": str(year_path),
        "era_metrics": str(era_path),
        "remove_best_years": str(remove_path),
    }
    tmp = summary_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(summary_path)
    return {**summary, "summary": str(summary_path)}
