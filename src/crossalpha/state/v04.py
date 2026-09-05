from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd


PROTOCOL = "CROSSALPHA_STATE_V0_4"
MODE = "PROSPECTIVE_MULTI_VENUE_MARKET_MECHANICS_SHADOW"
ACTIONABILITY = "DESCRIPTIVE_ONLY"
ASSETS = ("BTC", "ETH")
VENUES = ("binance", "okx", "bybit")
MINIMUM_VALID_VENUES = 2
FULL_CONFIDENCE_VENUES = 3
MAXIMUM_SNAPSHOT_AGE_SECONDS = 90
FUNDING_SEMANTICS = "LATEST_SETTLED_NORMALIZED_TO_8H"


def _utc(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _range_bps(values: pd.Series) -> float | None:
    data = pd.to_numeric(values, errors="coerce").dropna()
    data = data[data > 0]
    if len(data) < 2:
        return None
    midpoint = float(data.median())
    if midpoint <= 0:
        return None
    return float((data.max() - data.min()) / midpoint * 10_000.0)


def _hhi(values: pd.Series) -> float | None:
    data = pd.to_numeric(values, errors="coerce").dropna()
    data = data[data > 0]
    total = float(data.sum())
    if total <= 0 or len(data) < 2:
        return None
    shares = data / total
    return float((shares * shares).sum())


def _median(values: pd.Series) -> float | None:
    data = pd.to_numeric(values, errors="coerce").dropna()
    return float(data.median()) if not data.empty else None


def _range(values: pd.Series) -> float | None:
    data = pd.to_numeric(values, errors="coerce").dropna()
    return float(data.max() - data.min()) if len(data) >= 2 else None


def _std(values: pd.Series) -> float | None:
    data = pd.to_numeric(values, errors="coerce").dropna()
    return float(data.std(ddof=0)) if len(data) >= 2 else None


def compute_market_mechanics(
    rows: pd.DataFrame,
    *,
    generated_at: Any | None = None,
    maximum_age_seconds: int = MAXIMUM_SNAPSHOT_AGE_SECONDS,
) -> dict[str, Any]:
    """Compute a descriptive cross-venue mechanics vector without a stress score."""
    generated = _utc(generated_at or datetime.now(timezone.utc))
    required = {
        "observed_at",
        "known_at",
        "venue",
        "asset",
        "spot_mid",
        "perp_mid",
        "basis_bps",
        "funding_semantics",
        "funding_rate_settled_raw",
        "funding_settlement_time",
        "funding_interval_hours",
        "funding_rate_8h",
        "perp_spread_bps",
        "open_interest_usd",
    }
    missing = sorted(required - set(rows.columns))
    if missing:
        raise ValueError(f"State V0.4 normalized rows missing columns: {missing}")
    data = rows.copy()
    data["observed_at"] = pd.to_datetime(data["observed_at"], utc=True, errors="coerce")
    data["known_at"] = pd.to_datetime(data["known_at"], utc=True, errors="coerce")
    data["venue"] = data["venue"].astype(str).str.lower()
    data["asset"] = data["asset"].astype(str).str.upper()
    data = data.loc[
        data["venue"].isin(VENUES)
        & data["asset"].isin(ASSETS)
        & (data["known_at"] <= generated)
        & (data["observed_at"] <= generated)
    ].copy()
    if data.empty:
        return {
            "protocol": PROTOCOL,
            "mode": MODE,
            "actionability": ACTIONABILITY,
            "risk_multiplier": None,
            "generated_at": generated.isoformat(),
            "data_confidence": "INSUFFICIENT",
            "assets": {},
            "funding_semantics": FUNDING_SEMANTICS,
            "no_composite_stress_score": True,
        }

    latest_rows: list[pd.Series] = []
    for (_asset, _venue), part in data.groupby(["asset", "venue"], sort=True):
        part = part.sort_values(["observed_at", "known_at"])
        latest = part.iloc[-1]
        age = generated - latest["observed_at"]
        if pd.Timedelta(0) <= age <= pd.Timedelta(seconds=maximum_age_seconds):
            latest_rows.append(latest)
    latest = pd.DataFrame(latest_rows) if latest_rows else pd.DataFrame(columns=data.columns)

    asset_reports: dict[str, dict[str, Any]] = {}
    valid_counts: list[int] = []
    for asset in ASSETS:
        part = latest.loc[latest["asset"] == asset].copy()
        complete = part.loc[
            pd.to_numeric(part["spot_mid"], errors="coerce").gt(0)
            & pd.to_numeric(part["perp_mid"], errors="coerce").gt(0)
            & pd.to_numeric(part["basis_bps"], errors="coerce").notna()
        ].copy()
        valid_venues = sorted(complete["venue"].astype(str).unique().tolist())
        valid_count = len(valid_venues)
        valid_counts.append(valid_count)
        if valid_count >= FULL_CONFIDENCE_VENUES:
            confidence = "FULL"
        elif valid_count >= MINIMUM_VALID_VENUES:
            confidence = "PARTIAL"
        else:
            confidence = "INSUFFICIENT"

        funding_semantic_mask = complete["funding_semantics"].astype(str).eq(FUNDING_SEMANTICS)
        funding_interval = pd.to_numeric(complete["funding_interval_hours"], errors="coerce")
        funding_rate = pd.to_numeric(complete["funding_rate_8h"], errors="coerce")
        comparable_funding = complete.loc[
            funding_semantic_mask & funding_interval.gt(0) & funding_rate.notna()
        ].copy()
        oi = pd.to_numeric(complete["open_interest_usd"], errors="coerce")
        total_oi = float(oi.dropna().sum()) if oi.notna().any() else None
        funding_count = int(len(comparable_funding))
        asset_reports[asset] = {
            "data_confidence": confidence,
            "valid_venue_count": valid_count,
            "valid_venues": valid_venues,
            "funding_comparable_venue_count": funding_count,
            "spot_cross_venue_range_bps": _range_bps(complete["spot_mid"]),
            "basis_median_bps": _median(complete["basis_bps"]),
            "basis_range_bps": _range(complete["basis_bps"]),
            "basis_std_bps": _std(complete["basis_bps"]),
            "funding_8h_median": _median(comparable_funding["funding_rate_8h"]),
            "funding_8h_range": _range(comparable_funding["funding_rate_8h"]),
            "perp_spread_median_bps": _median(complete["perp_spread_bps"]),
            "perp_spread_max_bps": (
                float(pd.to_numeric(complete["perp_spread_bps"], errors="coerce").max())
                if pd.to_numeric(complete["perp_spread_bps"], errors="coerce").notna().any()
                else None
            ),
            "total_open_interest_usd": total_oi,
            "open_interest_hhi": _hhi(complete["open_interest_usd"]),
            "venues": {
                str(row.venue): {
                    "observed_at": row.observed_at.isoformat(),
                    "spot_mid": _number(row.spot_mid),
                    "perp_mid": _number(row.perp_mid),
                    "basis_bps": _number(row.basis_bps),
                    "funding_semantics": str(row.funding_semantics),
                    "funding_rate_settled_raw": _number(row.funding_rate_settled_raw),
                    "funding_settlement_time": (
                        None
                        if pd.isna(row.funding_settlement_time)
                        else str(row.funding_settlement_time)
                    ),
                    "funding_interval_hours": _number(row.funding_interval_hours),
                    "funding_rate_8h": _number(row.funding_rate_8h),
                    "perp_spread_bps": _number(row.perp_spread_bps),
                    "open_interest_usd": _number(row.open_interest_usd),
                }
                for row in complete.itertuples(index=False)
            },
        }

    minimum = min(valid_counts) if valid_counts else 0
    if minimum >= FULL_CONFIDENCE_VENUES:
        confidence = "FULL"
    elif minimum >= MINIMUM_VALID_VENUES:
        confidence = "PARTIAL"
    else:
        confidence = "INSUFFICIENT"
    return {
        "protocol": PROTOCOL,
        "mode": MODE,
        "actionability": ACTIONABILITY,
        "risk_multiplier": None,
        "mutates_frozen_core": False,
        "mutates_state_v01": False,
        "mutates_state_ab_v01": False,
        "mutates_state_v02": False,
        "mutates_state_v03": False,
        "generated_at": generated.isoformat(),
        "data_confidence": confidence,
        "assets": asset_reports,
        "funding_semantics": FUNDING_SEMANTICS,
        "no_composite_stress_score": True,
        "interpretation": (
            "Descriptive cross-venue market-mechanics vector only. Funding uses the most "
            "recent settled rate normalized by the observed settlement interval. Basis, funding, "
            "spread, spot dislocation and OI concentration are not trading signals in V0.4."
        ),
    }
