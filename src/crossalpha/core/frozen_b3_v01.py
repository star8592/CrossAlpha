from __future__ import annotations

import math

import numpy as np
import pandas as pd


PROTOCOL = "CROSSALPHA_FREE_V0_1"
STRATEGY = "B3_ABSOLUTE_TREND_EQUAL_WEIGHT"
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
SLEEVES: dict[str, tuple[tuple[str, ...], float]] = {
    "crypto": (("BTC", "ETH"), 0.35),
    "equity": (("US_EQUITY", "US_GROWTH"), 0.40),
    "precious_metals": (("GOLD", "SILVER"), 0.35),
    "cyclical_commodities": (("COPPER", "WTI"), 0.35),
}

TREND_WINDOW_DAYS = 365
VOL_WINDOW_DAYS = 63
ANNUALIZATION_DAYS = 365
TARGET_VOL = 0.10
SINGLE_ASSET_MAX = 0.25
REBALANCE_WEEKDAY = 0
EXECUTION_LAG_DAYS = 1
ONE_WAY_COST_BPS = 5.0
LEVERAGE_CAP = 1.0
ALLOW_SHORT = False


def _rolling_compound(returns: pd.DataFrame, window: int) -> pd.DataFrame:
    logs = np.log1p(returns)
    return np.expm1(logs.rolling(window=window, min_periods=window).sum())


def _normalize_positive(raw: pd.Series) -> pd.Series:
    clean = (
        pd.to_numeric(raw, errors="coerce")
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0.0)
        .clip(lower=0.0)
    )
    total = float(clean.sum())
    return clean / total if total > 0 else clean * 0.0


def _apply_constraints(raw: pd.Series) -> pd.Series:
    weights = _normalize_positive(raw.reindex(RISK_ASSETS, fill_value=0.0))
    weights = weights.clip(upper=SINGLE_ASSET_MAX)
    for assets, cap in SLEEVES.values():
        names = list(assets)
        total = float(weights[names].sum())
        if total > cap and total > 0:
            weights.loc[names] *= cap / total
    gross = float(weights.sum())
    if gross > LEVERAGE_CAP:
        weights *= LEVERAGE_CAP / gross
        gross = LEVERAGE_CAP
    result = pd.Series(0.0, index=ALL_ASSETS, dtype=float)
    result.loc[list(RISK_ASSETS)] = weights
    result.loc["CASH"] = max(0.0, 1.0 - gross)
    return result


def _scale_to_target_vol(weights: pd.Series, history: pd.DataFrame) -> pd.Series:
    result = weights.copy()
    risk_weights = pd.to_numeric(
        result.loc[list(RISK_ASSETS)], errors="coerce"
    ).fillna(0.0)
    selected = list(risk_weights[risk_weights > 0].index)
    if not selected:
        return result
    window = history.loc[:, selected].tail(VOL_WINDOW_DAYS)
    if len(window) < VOL_WINDOW_DAYS:
        return result
    cov = window.fillna(0.0).cov() * float(ANNUALIZATION_DAYS)
    vector = risk_weights.loc[selected].to_numpy(dtype=float)
    variance = float(vector @ cov.to_numpy(dtype=float) @ vector)
    if not math.isfinite(variance) or variance <= 0:
        return result
    predicted_vol = math.sqrt(variance)
    scale = min(1.0, TARGET_VOL / predicted_vol)
    if scale >= 1.0:
        return result
    result.loc[selected] = risk_weights.loc[selected] * scale
    gross = float(result.loc[list(RISK_ASSETS)].sum())
    result.loc["CASH"] = max(0.0, 1.0 - gross)
    return result


def compute_target(
    daily_returns: pd.DataFrame,
    available: pd.DataFrame,
    *,
    signal_date: pd.Timestamp,
) -> pd.Series:
    """Return the frozen B3 V0.1 target known at ``signal_date``.

    The caller is responsible for ensuring that ``daily_returns`` contains no
    observations later than ``signal_date``. This function deliberately has no
    tuning knobs: changing these rules requires a new protocol version.
    """
    signal_date = pd.Timestamp(signal_date)
    if signal_date.tzinfo is None:
        signal_date = signal_date.tz_localize("UTC")
    else:
        signal_date = signal_date.tz_convert("UTC")
    if signal_date not in daily_returns.index or signal_date not in available.index:
        raise ValueError(f"signal date missing from frozen B3 inputs: {signal_date}")
    if daily_returns.index.max() > signal_date:
        raise ValueError("frozen B3 inputs contain observations after signal_date")

    risk = daily_returns.loc[:, list(RISK_ASSETS)]
    trend = _rolling_compound(risk, TREND_WINDOW_DAYS)
    trend_row = trend.loc[signal_date].reindex(RISK_ASSETS)
    avail = available.loc[signal_date].reindex(RISK_ASSETS).fillna(False)
    eligible = avail & trend_row.notna() & (trend_row > 0)
    raw = eligible.astype(float)
    constrained = _apply_constraints(raw)

    history_start = signal_date - pd.Timedelta(days=VOL_WINDOW_DAYS - 1)
    history = risk.loc[history_start:signal_date]
    return _scale_to_target_vol(constrained, history)


def frozen_parameters() -> dict[str, object]:
    return {
        "protocol": PROTOCOL,
        "strategy": STRATEGY,
        "trend_window_calendar_days": TREND_WINDOW_DAYS,
        "vol_window_calendar_days": VOL_WINDOW_DAYS,
        "annualization_days": ANNUALIZATION_DAYS,
        "target_vol": TARGET_VOL,
        "single_asset_max": SINGLE_ASSET_MAX,
        "sleeves": {
            name: {"assets": list(assets), "max": cap}
            for name, (assets, cap) in SLEEVES.items()
        },
        "rebalance_weekday": "monday",
        "execution_lag_calendar_days": EXECUTION_LAG_DAYS,
        "one_way_cost_bps": ONE_WAY_COST_BPS,
        "shorting": ALLOW_SHORT,
        "leverage_cap": LEVERAGE_CAP,
    }
