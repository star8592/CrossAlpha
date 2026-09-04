from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from crossalpha.core.free_baselines import ALL_ASSETS, RISK_ASSETS, _load_daily_panel, _metrics, _safe_slug

B3 = "B3_ABSOLUTE_TREND_EQUAL_WEIGHT"
B4 = "B4_ABSOLUTE_TREND_INVERSE_VOLATILITY"
B1 = "B1_EQUAL_WEIGHT"
FINAL_PROTOCOL = "CROSSALPHA_FREE_V0_1"


def _research_root(data_root: Path, start: str, end: str) -> Path:
    return data_root / "research" / "free_v01"


def _range_root(base: Path, stage: str, start: str, end: str) -> Path:
    return base / stage / f"start={_safe_slug(start)}" / f"end={_safe_slug(end)}"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"required research artifact missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _baseline_paths(data_root: Path, start: str, end: str) -> tuple[Path, Path, Path]:
    root = _range_root(_research_root(data_root, start, end), "baselines", start, end)
    return root / "summary.json", root / "weights.parquet", root / "strategy_returns.parquet"


def _neutralize_year(part: pd.DataFrame, year: int) -> pd.DataFrame:
    stressed = part.copy()
    dates = pd.to_datetime(stressed["date"], utc=True)
    mask = dates.dt.year == year
    stressed.loc[mask, "net_return"] = stressed.loc[mask, "cash_return"]
    stressed.loc[mask, "gross_return"] = stressed.loc[mask, "cash_return"]
    stressed.loc[mask, "turnover"] = 0.0
    stressed.loc[mask, "cost"] = 0.0
    return stressed


def _leave_one_year_out(strategy_returns: pd.DataFrame, strategy: str) -> pd.DataFrame:
    part = strategy_returns.loc[strategy_returns["strategy"] == strategy].copy()
    part["date"] = pd.to_datetime(part["date"], utc=True)
    years = sorted(int(v) for v in part["date"].dt.year.unique())
    rows: list[dict[str, Any]] = []
    for year in years:
        stressed = _neutralize_year(part, year)
        m = _metrics(stressed, 365)
        rows.append({"strategy": strategy, "neutralized_year": year, **m})
    return pd.DataFrame(rows)


def _weight_concentration(weights: pd.DataFrame, strategy: str) -> dict[str, Any]:
    part = weights.loc[weights["strategy"] == strategy].copy()
    risk = part.loc[:, list(RISK_ASSETS)].clip(lower=0.0).fillna(0.0)
    gross = risk.sum(axis=1)
    normalized = risk.div(gross.replace(0.0, np.nan), axis=0).fillna(0.0)
    hhi = (normalized**2).sum(axis=1)
    effective_n = pd.Series(np.where(hhi > 0, 1.0 / hhi, np.nan), index=hhi.index)
    asset_avg = risk.mean().sort_values(ascending=False)
    asset_max = risk.max().sort_values(ascending=False)
    return {
        "average_risk_gross": float(gross.mean()),
        "max_risk_gross": float(gross.max()),
        "average_effective_asset_count": float(effective_n.dropna().mean()),
        "min_effective_asset_count": float(effective_n.dropna().min()),
        "average_weights": {k: float(v) for k, v in asset_avg.items()},
        "max_weights": {k: float(v) for k, v in asset_max.items()},
    }


def _asset_contribution(
    daily: pd.DataFrame,
    weights: pd.DataFrame,
    strategy: str,
) -> pd.DataFrame:
    part = weights.loc[weights["strategy"] == strategy].copy()
    part["date"] = pd.to_datetime(part["date"], utc=True)
    w = part.set_index("date").loc[:, list(ALL_ASSETS)].reindex(daily.index).fillna(0.0)
    aligned = daily.reindex(columns=ALL_ASSETS).fillna(0.0)
    contribution = w * aligned
    rows: list[dict[str, Any]] = []
    total_abs = float(contribution.loc[:, list(RISK_ASSETS)].sum().abs().sum())
    for asset in ALL_ASSETS:
        total = float(contribution[asset].sum())
        rows.append(
            {
                "strategy": strategy,
                "asset": asset,
                "sum_daily_gross_contribution": total,
                "annualized_arithmetic_contribution": float(contribution[asset].mean() * 365.0),
                "share_of_abs_risk_contribution": (
                    abs(total) / total_abs if asset in RISK_ASSETS and total_abs > 0 else None
                ),
                "average_weight": float(w[asset].mean()),
                "max_weight": float(w[asset].max()),
            }
        )
    return pd.DataFrame(rows)


def _drawdown_episodes(part: pd.DataFrame) -> pd.DataFrame:
    frame = part.copy().sort_values("date").reset_index(drop=True)
    frame["date"] = pd.to_datetime(frame["date"], utc=True)
    returns = pd.to_numeric(frame["net_return"], errors="coerce").fillna(0.0)
    wealth = (1.0 + returns).cumprod()
    running_max = wealth.cummax()
    dd = wealth / running_max - 1.0

    episodes: list[dict[str, Any]] = []
    in_dd = False
    peak_idx = 0
    trough_idx = 0
    for i, value in enumerate(dd):
        if value < -1e-12 and not in_dd:
            in_dd = True
            peak_idx = max(i - 1, 0)
            trough_idx = i
        if in_dd and value < dd.iloc[trough_idx]:
            trough_idx = i
        recovered = in_dd and value >= -1e-12
        last = i == len(dd) - 1
        if in_dd and (recovered or last):
            end_idx = i
            episodes.append(
                {
                    "peak_date": frame.loc[peak_idx, "date"],
                    "trough_date": frame.loc[trough_idx, "date"],
                    "recovery_date": frame.loc[end_idx, "date"] if recovered else pd.NaT,
                    "benchmark_drawdown": float(dd.iloc[trough_idx]),
                }
            )
            in_dd = False
    return pd.DataFrame(episodes).sort_values("benchmark_drawdown").reset_index(drop=True)


def _period_return(part: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> float:
    frame = part.copy()
    dates = pd.to_datetime(frame["date"], utc=True)
    values = pd.to_numeric(frame.loc[(dates >= start) & (dates <= end), "net_return"], errors="coerce").fillna(0.0)
    return float((1.0 + values).prod() - 1.0) if len(values) else float("nan")


def _benchmark_stress_comparison(strategy_returns: pd.DataFrame, top_n: int = 5) -> pd.DataFrame:
    benchmark = strategy_returns.loc[strategy_returns["strategy"] == B1].copy()
    b3 = strategy_returns.loc[strategy_returns["strategy"] == B3].copy()
    episodes = _drawdown_episodes(benchmark).head(top_n)
    rows: list[dict[str, Any]] = []
    for rank, row in episodes.iterrows():
        start = pd.Timestamp(row["peak_date"])
        trough = pd.Timestamp(row["trough_date"])
        b1_return = _period_return(benchmark, start, trough)
        b3_return = _period_return(b3, start, trough)
        rows.append(
            {
                "rank": rank + 1,
                "peak_date": start,
                "trough_date": trough,
                "benchmark_drawdown": float(row["benchmark_drawdown"]),
                "B1_period_return": b1_return,
                "B3_period_return": b3_return,
                "B3_minus_B1": b3_return - b1_return,
                "B3_outperformed": bool(b3_return > b1_return),
            }
        )
    return pd.DataFrame(rows)


def run_free_final_evaluation(data_root: Path, *, start: str, end: str) -> dict[str, Any]:
    research = _research_root(data_root, start, end)
    baseline_summary_path, weights_path, returns_path = _baseline_paths(data_root, start, end)
    stage1_path = _range_root(research, "robustness_stage1", start, end) / "summary.json"
    stage2_path = _range_root(research, "robustness_stage2", start, end) / "summary.json"

    baseline = _read_json(baseline_summary_path)
    stage1 = _read_json(stage1_path)
    stage2 = _read_json(stage2_path)
    weights = pd.read_parquet(weights_path)
    strategy_returns = pd.read_parquet(returns_path)
    daily, _ = _load_daily_panel(data_root, start, end)

    b3_loyo = _leave_one_year_out(strategy_returns, B3)
    b4_loyo = _leave_one_year_out(strategy_returns, B4)
    loyo = pd.concat([b3_loyo, b4_loyo], ignore_index=True)
    contribution = _asset_contribution(daily, weights, B3)
    stress = _benchmark_stress_comparison(strategy_returns, top_n=5)
    concentration = _weight_concentration(weights, B3)

    b3_stage2 = stage2["focus_screen"][B3]
    b4_stage2 = stage2["focus_screen"][B4]
    b3_wf = stage2["walk_forward"][B3]
    b4_wf = stage2["walk_forward"][B4]

    b3_loyo_min = float(pd.to_numeric(b3_loyo["sharpe_excess_cash"], errors="coerce").min())
    b4_loyo_min = float(pd.to_numeric(b4_loyo["sharpe_excess_cash"], errors="coerce").min())
    b3_loyo_all_positive = bool((pd.to_numeric(b3_loyo["sharpe_excess_cash"], errors="coerce") > 0).all())

    # Decision semantics deliberately reserve SUPPORTED for a truly prospective
    # frozen run. Historical walk-forward and falsification can nominate a candidate,
    # but they cannot manufacture unseen future data.
    b3_decision = (
        "PROMISING_BUT_UNPROVEN"
        if bool(b3_stage2["survives_stage2"]) and b3_loyo_all_positive
        else "REJECTED"
    )
    b4_decision = "REJECTED" if not bool(b4_stage2["survives_stage2"]) else "PROMISING_BUT_UNPROVEN"

    final = {
        "protocol": FINAL_PROTOCOL,
        "stage": "FINAL_HISTORICAL_EVALUATION",
        "data_cost_usd": 0,
        "start": start,
        "end": end,
        "core_candidate": B3,
        "candidate_config_frozen": True,
        "parameter_optimization_allowed": False,
        "candidate_parameters": {
            "trend_window_calendar_days": 365,
            "vol_window_calendar_days": 63,
            "target_vol": 0.10,
            "rebalance_weekday": "monday",
            "execution_lag_calendar_days": 1,
            "one_way_cost_bps": 5.0,
            "shorting": False,
            "leverage_cap": 1.0,
        },
        "decisions": {
            B3: {
                "state": b3_decision,
                "role": "FROZEN_CORE_V0_1_CANDIDATE",
                "baseline": baseline["strategies"][B3],
                "stage1": stage1["focus_screen"][B3],
                "stage2": b3_stage2,
                "loyo_min_sharpe": b3_loyo_min,
                "loyo_all_positive": b3_loyo_all_positive,
                "walk_forward_selected_parameter_oos": b3_wf["selected_parameter_oos"],
                "walk_forward_frozen_parameter_oos": b3_wf["frozen_parameter_oos"],
                "tuning_value_add": (
                    float(b3_wf["selected_parameter_oos"]["sharpe_excess_cash"])
                    - float(b3_wf["frozen_parameter_oos"]["sharpe_excess_cash"])
                ),
            },
            B4: {
                "state": b4_decision,
                "role": "RESEARCH_BENCHMARK_ONLY",
                "baseline": baseline["strategies"][B4],
                "stage1": stage1["focus_screen"][B4],
                "stage2": b4_stage2,
                "loyo_min_sharpe": b4_loyo_min,
                "walk_forward_selected_parameter_oos": b4_wf["selected_parameter_oos"],
                "walk_forward_frozen_parameter_oos": b4_wf["frozen_parameter_oos"],
                "tuning_value_add": (
                    float(b4_wf["selected_parameter_oos"]["sharpe_excess_cash"])
                    - float(b4_wf["frozen_parameter_oos"]["sharpe_excess_cash"])
                ),
            },
        },
        "B3_concentration": concentration,
        "B3_benchmark_stress_outperformance_count": int(stress["B3_outperformed"].sum()) if len(stress) else 0,
        "B3_benchmark_stress_episode_count": int(len(stress)),
        "interpretation": (
            "B3 is frozen as the only Core V0.1 candidate if it survives historical falsification and every "
            "single-year neutralization. The state remains PROMISING_BUT_UNPROVEN until genuinely prospective "
            "paper/live observations accumulate without changing the frozen rules. B4 remains a benchmark after "
            "failing the pre-registered Stage-2 PBO gate."
        ),
    }

    output = _range_root(research, "final_evaluation", start, end)
    output.mkdir(parents=True, exist_ok=True)
    loyo_path = output / "leave_one_year_out.parquet"
    contribution_path = output / "B3_asset_contribution.parquet"
    stress_path = output / "B1_drawdown_B3_comparison.parquet"
    summary_path = output / "final_decision.json"
    loyo.to_parquet(loyo_path, index=False)
    contribution.to_parquet(contribution_path, index=False)
    stress.to_parquet(stress_path, index=False)
    tmp = summary_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(final, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(summary_path)
    return {
        **final,
        "leave_one_year_out": str(loyo_path),
        "asset_contribution": str(contribution_path),
        "stress_comparison": str(stress_path),
        "summary": str(summary_path),
    }
