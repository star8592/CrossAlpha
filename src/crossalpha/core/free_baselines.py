from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


RISK_ASSETS = (
    "US_EQUITY",
    "US_GROWTH",
    "GOLD",
    "SILVER",
    "COPPER",
    "WTI",
    "BTC",
    "ETH",
)
ALL_ASSETS = (*RISK_ASSETS, "CASH")
STRATEGIES = (
    "B0_CASH",
    "B1_EQUAL_WEIGHT",
    "B2_INVERSE_VOLATILITY",
    "B3_ABSOLUTE_TREND_EQUAL_WEIGHT",
    "B4_ABSOLUTE_TREND_INVERSE_VOLATILITY",
    "B5_MULTI_HORIZON_TREND_INVERSE_VOLATILITY",
    "B6_DUAL_MOMENTUM_INVERSE_VOLATILITY",
    "B7_MULTI_HORIZON_TREND_HRP",
)
SLEEVES: dict[str, tuple[tuple[str, ...], float]] = {
    "crypto": (("BTC", "ETH"), 0.35),
    "equity": (("US_EQUITY", "US_GROWTH"), 0.40),
    "precious_metals": (("GOLD", "SILVER"), 0.35),
    "industrials": (("COPPER",), 0.25),
    "energy": (("WTI",), 0.25),
}


@dataclass(frozen=True)
class FreeBaselineConfig:
    cost_bps: float = 5.0
    vol_window_days: int = 63
    trend_window_days: int = 365
    multi_horizons_days: tuple[int, ...] = (30, 90, 180, 365)
    hrp_window_days: int = 126
    annualization_days: int = 365
    rebalance_weekday: int = 0  # Monday
    execution_lag_days: int = 1
    single_asset_max: float = 0.25
    dual_momentum_top_fraction: float = 0.5


def _safe_slug(value: str) -> str:
    return value.replace(":", "-").replace("/", "-").replace(" ", "_")


def _input_path(data_root: Path, start: str, end: str) -> Path:
    return (
        data_root
        / "derived"
        / "core"
        / "free_v01"
        / f"start={_safe_slug(start)}"
        / f"end={_safe_slug(end)}"
        / "asset_returns.parquet"
    )


def _output_root(data_root: Path, start: str, end: str) -> Path:
    return (
        data_root
        / "research"
        / "free_v01"
        / "baselines"
        / f"start={_safe_slug(start)}"
        / f"end={_safe_slug(end)}"
    )


def _load_daily_panel(
    data_root: Path,
    start: str,
    end: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    path = _input_path(data_root, start, end)
    if not path.exists():
        raise FileNotFoundError(f"free Core returns missing: {path}")

    frame = pd.read_parquet(path).copy()
    required = {"date", "economic_asset", "return"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"free Core return file missing columns: {missing}")

    frame["date"] = pd.to_datetime(frame["date"], utc=True).dt.normalize()
    frame["return"] = pd.to_numeric(frame["return"], errors="coerce")
    start_ts = pd.to_datetime(start, utc=True).normalize()
    end_ts = pd.to_datetime(end, utc=True).normalize()
    calendar = pd.date_range(start_ts, end_ts, inclusive="left", freq="D")

    risk = pd.DataFrame(index=calendar, columns=RISK_ASSETS, dtype=float)
    available = pd.DataFrame(False, index=calendar, columns=RISK_ASSETS, dtype=bool)

    for asset in RISK_ASSETS:
        part = frame.loc[frame["economic_asset"] == asset, ["date", "return"]].sort_values("date")
        if part.empty:
            continue
        if part["date"].duplicated().any():
            raise ValueError(f"duplicate research dates for {asset}")
        inception = part["date"].min()
        values = part.set_index("date")["return"].reindex(calendar)
        active = calendar >= inception
        # Canonical audit already checks there are no suspicious long gaps. On calendar
        # days when an ETF market is closed, economic mark-to-market return is zero.
        values.loc[active] = values.loc[active].fillna(0.0)
        risk[asset] = values
        available.loc[active, asset] = True

    cash_part = frame.loc[frame["economic_asset"] == "CASH", ["date", "return"]].sort_values("date")
    if cash_part.empty:
        raise ValueError("CASH series missing from free Core returns")
    cash = cash_part.set_index("date")["return"].reindex(calendar)
    cash = pd.to_numeric(cash, errors="coerce").ffill().fillna(0.0)

    daily = risk.copy()
    daily["CASH"] = cash
    return daily, available


def _rolling_compound(returns: pd.DataFrame, window: int) -> pd.DataFrame:
    logs = np.log1p(returns)
    return np.expm1(logs.rolling(window=window, min_periods=window).sum())


def _compute_features(
    risk_returns: pd.DataFrame,
    config: FreeBaselineConfig,
) -> dict[str, Any]:
    vol = risk_returns.rolling(
        window=config.vol_window_days,
        min_periods=config.vol_window_days,
    ).std(ddof=1) * math.sqrt(config.annualization_days)
    trend = _rolling_compound(risk_returns, config.trend_window_days)
    horizon_returns = {
        horizon: _rolling_compound(risk_returns, horizon)
        for horizon in config.multi_horizons_days
    }
    score_parts = [np.sign(frame) for frame in horizon_returns.values()]
    multi_score = sum(score_parts) / float(len(score_parts))
    return {
        "vol": vol,
        "trend": trend,
        "horizon_returns": horizon_returns,
        "multi_score": multi_score,
    }


def _normalize_positive(raw: pd.Series) -> pd.Series:
    raw = pd.to_numeric(raw, errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0.0)
    raw = raw.clip(lower=0.0)
    total = float(raw.sum())
    if total <= 0:
        return raw * 0.0
    return raw / total


def _apply_constraints(raw: pd.Series, config: FreeBaselineConfig) -> pd.Series:
    weights = _normalize_positive(raw.reindex(RISK_ASSETS, fill_value=0.0))
    weights = weights.clip(upper=config.single_asset_max)
    for _, (assets, cap) in SLEEVES.items():
        group = list(assets)
        total = float(weights[group].sum())
        if total > cap and total > 0:
            weights.loc[group] *= cap / total
    gross = float(weights.sum())
    if gross > 1.0:
        weights /= gross
        gross = 1.0
    result = pd.Series(0.0, index=ALL_ASSETS, dtype=float)
    result.loc[list(RISK_ASSETS)] = weights
    result.loc["CASH"] = max(0.0, 1.0 - gross)
    return result


def _cluster_distance(distance: pd.DataFrame, left: list[str], right: list[str]) -> float:
    values = [float(distance.loc[a, b]) for a in left for b in right]
    return min(values) if values else 0.0


def _hrp_leaf_order(corr: pd.DataFrame) -> list[str]:
    assets = list(corr.columns)
    if len(assets) <= 1:
        return assets
    distance = np.sqrt(((1.0 - corr.clip(-1.0, 1.0)) / 2.0).clip(lower=0.0))
    clusters: dict[int, list[str]] = {i: [asset] for i, asset in enumerate(assets)}
    children: dict[int, tuple[int, int]] = {}
    next_id = len(assets)

    while len(clusters) > 1:
        ids = sorted(clusters)
        best: tuple[float, int, int] | None = None
        for i, left_id in enumerate(ids[:-1]):
            for right_id in ids[i + 1 :]:
                value = _cluster_distance(distance, clusters[left_id], clusters[right_id])
                candidate = (value, left_id, right_id)
                if best is None or candidate < best:
                    best = candidate
        assert best is not None
        _, left_id, right_id = best
        merged = clusters.pop(left_id) + clusters.pop(right_id)
        children[next_id] = (left_id, right_id)
        clusters[next_id] = merged
        next_id += 1

    root = next(iter(clusters))

    def walk(node: int) -> list[str]:
        if node < len(assets):
            return [assets[node]]
        left, right = children[node]
        return walk(left) + walk(right)

    return walk(root)


def _cluster_variance(cov: pd.DataFrame, assets: list[str]) -> float:
    sub = cov.loc[assets, assets]
    diagonal = np.diag(sub.to_numpy(dtype=float))
    inverse = np.where(diagonal > 0, 1.0 / diagonal, 0.0)
    if inverse.sum() <= 0:
        weights = np.repeat(1.0 / len(assets), len(assets))
    else:
        weights = inverse / inverse.sum()
    matrix = sub.to_numpy(dtype=float)
    return float(weights @ matrix @ weights)


def _hrp_weights(history: pd.DataFrame) -> pd.Series:
    clean = history.dropna(axis=1, how="all").fillna(0.0)
    assets = list(clean.columns)
    if not assets:
        return pd.Series(dtype=float)
    if len(assets) == 1:
        return pd.Series({assets[0]: 1.0})
    cov = clean.cov()
    corr = clean.corr().fillna(0.0)
    np.fill_diagonal(corr.values, 1.0)
    order = _hrp_leaf_order(corr)
    weights = pd.Series(1.0, index=order, dtype=float)
    clusters = [order]
    while clusters:
        cluster = clusters.pop(0)
        if len(cluster) <= 1:
            continue
        split = len(cluster) // 2
        left = cluster[:split]
        right = cluster[split:]
        var_left = _cluster_variance(cov, left)
        var_right = _cluster_variance(cov, right)
        denom = var_left + var_right
        alpha = 0.5 if denom <= 0 else 1.0 - var_left / denom
        weights.loc[left] *= alpha
        weights.loc[right] *= 1.0 - alpha
        clusters.extend([left, right])
    total = float(weights.sum())
    return weights / total if total > 0 else weights


def _targets_for_date(
    date: pd.Timestamp,
    signal_date: pd.Timestamp,
    risk_returns: pd.DataFrame,
    available: pd.DataFrame,
    features: dict[str, Any],
    config: FreeBaselineConfig,
) -> dict[str, pd.Series]:
    empty = pd.Series(0.0, index=RISK_ASSETS, dtype=float)
    avail = available.loc[signal_date].reindex(RISK_ASSETS).fillna(False)
    vol = features["vol"].loc[signal_date].reindex(RISK_ASSETS)
    trend = features["trend"].loc[signal_date].reindex(RISK_ASSETS)
    score = features["multi_score"].loc[signal_date].reindex(RISK_ASSETS)

    targets: dict[str, pd.Series] = {}
    targets["B0_CASH"] = _apply_constraints(empty, config)

    raw = pd.Series({asset: 1.0 if bool(avail[asset]) else 0.0 for asset in RISK_ASSETS})
    targets["B1_EQUAL_WEIGHT"] = _apply_constraints(raw, config)

    inv_vol = pd.Series(0.0, index=RISK_ASSETS)
    valid_vol = avail & vol.notna() & (vol > 0)
    inv_vol.loc[valid_vol] = 1.0 / vol.loc[valid_vol]
    targets["B2_INVERSE_VOLATILITY"] = _apply_constraints(inv_vol, config)

    eligible_trend = avail & trend.notna() & (trend > 0)
    raw_trend_ew = eligible_trend.astype(float)
    targets["B3_ABSOLUTE_TREND_EQUAL_WEIGHT"] = _apply_constraints(raw_trend_ew, config)

    raw_trend_iv = inv_vol.where(eligible_trend, 0.0)
    targets["B4_ABSOLUTE_TREND_INVERSE_VOLATILITY"] = _apply_constraints(raw_trend_iv, config)

    positive_score = score.clip(lower=0.0).where(avail, 0.0).fillna(0.0)
    raw_multi_iv = inv_vol * positive_score
    targets["B5_MULTI_HORIZON_TREND_INVERSE_VOLATILITY"] = _apply_constraints(raw_multi_iv, config)

    dual_candidates = trend.where(eligible_trend).dropna().sort_values(ascending=False)
    if dual_candidates.empty:
        dual_raw = empty.copy()
    else:
        count = max(1, int(math.ceil(len(dual_candidates) * config.dual_momentum_top_fraction)))
        chosen = set(dual_candidates.index[:count])
        dual_raw = inv_vol.where(inv_vol.index.to_series().isin(chosen), 0.0)
    targets["B6_DUAL_MOMENTUM_INVERSE_VOLATILITY"] = _apply_constraints(dual_raw, config)

    hrp_assets = [asset for asset in RISK_ASSETS if positive_score.get(asset, 0.0) > 0]
    if hrp_assets:
        history_start = signal_date - pd.Timedelta(days=config.hrp_window_days - 1)
        history = risk_returns.loc[history_start:signal_date, hrp_assets]
        enough = [asset for asset in hrp_assets if history[asset].notna().sum() >= config.hrp_window_days]
        if enough:
            hrp = _hrp_weights(history[enough])
            hrp_raw = empty.copy()
            hrp_raw.loc[hrp.index] = hrp
        else:
            hrp_raw = empty.copy()
    else:
        hrp_raw = empty.copy()
    targets["B7_MULTI_HORIZON_TREND_HRP"] = _apply_constraints(hrp_raw, config)
    return targets


def _build_weight_history(
    daily_returns: pd.DataFrame,
    available: pd.DataFrame,
    config: FreeBaselineConfig,
) -> pd.DataFrame:
    risk_returns = daily_returns.loc[:, list(RISK_ASSETS)]
    features = _compute_features(risk_returns, config)
    dates = daily_returns.index
    current = {strategy: pd.Series({asset: 0.0 for asset in ALL_ASSETS}) for strategy in STRATEGIES}
    for strategy in STRATEGIES:
        current[strategy].loc["CASH"] = 1.0

    records: list[dict[str, Any]] = []
    for date in dates:
        if date.weekday() == config.rebalance_weekday:
            signal_date = date - pd.Timedelta(days=config.execution_lag_days)
            if signal_date in dates:
                targets = _targets_for_date(
                    date,
                    signal_date,
                    risk_returns,
                    available,
                    features,
                    config,
                )
                current.update(targets)
        for strategy in STRATEGIES:
            row: dict[str, Any] = {"date": date, "strategy": strategy}
            row.update({asset: float(current[strategy].get(asset, 0.0)) for asset in ALL_ASSETS})
            records.append(row)
    return pd.DataFrame(records)


def _compute_strategy_returns(
    daily_returns: pd.DataFrame,
    weights: pd.DataFrame,
    config: FreeBaselineConfig,
) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    daily = daily_returns.reindex(columns=ALL_ASSETS).fillna(0.0)
    for strategy in STRATEGIES:
        part = weights.loc[weights["strategy"] == strategy].copy().set_index("date")
        w = part.loc[:, list(ALL_ASSETS)].reindex(daily.index).fillna(0.0)
        gross = (w * daily).sum(axis=1)
        changes = w.diff().abs().sum(axis=1).fillna(0.0) * 0.5
        cost = changes * (config.cost_bps / 10_000.0)
        net = gross - cost
        frame = pd.DataFrame(
            {
                "date": daily.index,
                "strategy": strategy,
                "gross_return": gross.to_numpy(),
                "turnover": changes.to_numpy(),
                "cost": cost.to_numpy(),
                "net_return": net.to_numpy(),
                "cash_return": daily["CASH"].to_numpy(),
            }
        )
        rows.append(frame)
    return pd.concat(rows, ignore_index=True)


def _metrics(part: pd.DataFrame, annualization_days: int) -> dict[str, float | int | str | None]:
    returns = pd.to_numeric(part["net_return"], errors="coerce").fillna(0.0)
    cash = pd.to_numeric(part["cash_return"], errors="coerce").fillna(0.0)
    dates = pd.to_datetime(part["date"], utc=True)
    years = max((dates.max() - dates.min()).days / 365.25, 1.0 / 365.25)
    growth = (1.0 + returns).cumprod()
    terminal = float(growth.iloc[-1]) if not growth.empty else 1.0
    cagr = terminal ** (1.0 / years) - 1.0 if terminal > 0 else -1.0
    ann_vol = float(returns.std(ddof=1) * math.sqrt(annualization_days))
    excess = returns - cash
    excess_std = float(excess.std(ddof=1))
    sharpe = (
        float(excess.mean() / excess_std * math.sqrt(annualization_days))
        if excess_std > 0
        else None
    )
    drawdown = growth / growth.cummax() - 1.0
    max_drawdown = float(drawdown.min()) if not drawdown.empty else 0.0
    turnover = float(pd.to_numeric(part["turnover"], errors="coerce").fillna(0.0).sum())
    return {
        "start": dates.min().isoformat(),
        "end": dates.max().isoformat(),
        "days": int(len(part)),
        "years": years,
        "terminal_growth": terminal,
        "cagr": cagr,
        "annualized_volatility": ann_vol,
        "sharpe_excess_cash": sharpe,
        "max_drawdown": max_drawdown,
        "annual_turnover": turnover / years,
        "total_cost": float(pd.to_numeric(part["cost"], errors="coerce").fillna(0.0).sum()),
    }


def run_free_baselines(
    data_root: Path,
    *,
    start: str,
    end: str,
    config: FreeBaselineConfig | None = None,
) -> dict[str, Any]:
    config = config or FreeBaselineConfig()
    if config.cost_bps < 0:
        raise ValueError("cost_bps must be non-negative")

    daily, available = _load_daily_panel(data_root, start, end)
    weights = _build_weight_history(daily, available, config)
    strategy_returns = _compute_strategy_returns(daily, weights, config)

    summary: dict[str, Any] = {
        "protocol": "CROSSALPHA_FREE_V0_1",
        "stage": "DESCRIPTIVE_BASELINES_ONLY",
        "data_cost_usd": 0,
        "start": start,
        "end": end,
        "cost_bps_per_one_way_turnover": config.cost_bps,
        "strategies": {},
    }
    for strategy in STRATEGIES:
        part = strategy_returns.loc[strategy_returns["strategy"] == strategy]
        summary["strategies"][strategy] = _metrics(part, config.annualization_days)

    output_root = _output_root(data_root, start, end)
    output_root.mkdir(parents=True, exist_ok=True)
    weights_path = output_root / "weights.parquet"
    returns_path = output_root / "strategy_returns.parquet"
    summary_path = output_root / "summary.json"

    weights_tmp = weights_path.with_suffix(".parquet.tmp")
    returns_tmp = returns_path.with_suffix(".parquet.tmp")
    weights.to_parquet(weights_tmp, index=False)
    strategy_returns.to_parquet(returns_tmp, index=False)
    weights_tmp.replace(weights_path)
    returns_tmp.replace(returns_path)
    summary_tmp = summary_path.with_suffix(".json.tmp")
    summary_tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_tmp.replace(summary_path)

    return {
        **summary,
        "weights": str(weights_path),
        "strategy_returns": str(returns_path),
        "summary": str(summary_path),
    }
