from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd


PROTOCOL = "CROSSALPHA_STATE_V0_3"
MODE = "PROSPECTIVE_BORROWER_RISK_SHADOW"
ACTIONABILITY = "DESCRIPTIVE_ONLY"
HF_THRESHOLDS = (1.00, 1.02, 1.05, 1.10, 1.20, 1.50)
HF_BANDS: tuple[tuple[float, float | None], ...] = (
    (0.00, 1.00),
    (1.00, 1.02),
    (1.02, 1.05),
    (1.05, 1.10),
    (1.10, 1.20),
    (1.20, 1.50),
    (1.50, None),
)


@dataclass(frozen=True)
class CensusPolicy:
    maximum_failed_call_ratio: float = 0.01
    watchlist_health_factor_max: float = 1.50
    watchlist_debt_usd_min: float = 1_000_000.0


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def weighted_quantile(values: pd.Series, weights: pd.Series, q: float) -> float | None:
    if not 0 <= q <= 1:
        raise ValueError("q must be in [0, 1]")
    frame = pd.DataFrame(
        {
            "value": pd.to_numeric(values, errors="coerce"),
            "weight": pd.to_numeric(weights, errors="coerce"),
        }
    ).dropna()
    frame = frame.loc[(frame["weight"] > 0) & np.isfinite(frame["value"])]
    if frame.empty:
        return None
    frame = frame.sort_values("value")
    cumulative = frame["weight"].cumsum()
    target = float(frame["weight"].sum()) * q
    index = cumulative.searchsorted(target, side="left")
    index = min(int(index), len(frame) - 1)
    return float(frame.iloc[index]["value"])


def _band_name(low: float, high: float | None) -> str:
    if high is None:
        return f"hf_ge_{low:.2f}".replace(".", "_")
    return f"hf_{low:.2f}_to_{high:.2f}".replace(".", "_")


def compute_borrower_census(
    rows: pd.DataFrame,
    *,
    total_candidate_addresses: int,
    bootstrap_complete: bool,
    block_number: int,
    captured_at: Any | None = None,
    policy: CensusPolicy | None = None,
) -> dict[str, Any]:
    """Aggregate an auditable Aave V3 borrower health-factor census.

    Rows must come from Pool.getUserAccountData calls made at one common block tag.
    Historical Borrow events define the candidate universe; they are not themselves
    used to estimate current balances or health factors.
    """

    cfg = policy or CensusPolicy()
    captured = pd.Timestamp(captured_at or datetime.now(timezone.utc))
    if captured.tzinfo is None:
        captured = captured.tz_localize("UTC")
    else:
        captured = captured.tz_convert("UTC")
    if total_candidate_addresses < 0:
        raise ValueError("total_candidate_addresses must be non-negative")
    if block_number < 0:
        raise ValueError("block_number must be non-negative")

    data = rows.copy()
    required = {
        "address",
        "success",
        "total_collateral_usd",
        "total_debt_usd",
        "available_borrows_usd",
        "current_liquidation_threshold_pct",
        "ltv_pct",
        "health_factor",
    }
    missing = sorted(required - set(data.columns))
    if missing:
        raise ValueError(f"borrower census rows missing columns: {missing}")
    if data["address"].duplicated().any():
        raise ValueError("borrower census contains duplicate addresses")

    success = data.loc[data["success"].astype(bool)].copy()
    for column in (
        "total_collateral_usd",
        "total_debt_usd",
        "available_borrows_usd",
        "current_liquidation_threshold_pct",
        "ltv_pct",
        "health_factor",
    ):
        success[column] = pd.to_numeric(success[column], errors="coerce")

    successful_calls = int(len(success))
    failed_calls = max(int(total_candidate_addresses) - successful_calls, 0)
    failed_ratio = (
        failed_calls / total_candidate_addresses if total_candidate_addresses > 0 else 0.0
    )
    coverage_ratio = (
        successful_calls / total_candidate_addresses if total_candidate_addresses > 0 else 1.0
    )

    active = success.loc[success["total_debt_usd"].fillna(0.0) > 0].copy()
    active = active.loc[active["health_factor"].notna()].copy()
    active_debt = pd.to_numeric(active["total_debt_usd"], errors="coerce").fillna(0.0)
    total_debt = float(active_debt.sum())
    total_collateral = float(
        pd.to_numeric(active["total_collateral_usd"], errors="coerce").fillna(0.0).sum()
    )

    thresholds: dict[str, dict[str, Any]] = {}
    for threshold in HF_THRESHOLDS:
        mask = active["health_factor"] <= threshold
        debt = float(active.loc[mask, "total_debt_usd"].fillna(0.0).sum())
        thresholds[f"hf_le_{threshold:.2f}".replace(".", "_")] = {
            "threshold": threshold,
            "borrower_count": int(mask.sum()),
            "debt_usd": debt,
            "debt_share": debt / total_debt if total_debt > 0 else 0.0,
        }

    bands: dict[str, dict[str, Any]] = {}
    for low, high in HF_BANDS:
        if high is None:
            mask = active["health_factor"] >= low
        elif low == 0.0:
            mask = active["health_factor"] < high
        else:
            mask = (active["health_factor"] >= low) & (active["health_factor"] < high)
        debt = float(active.loc[mask, "total_debt_usd"].fillna(0.0).sum())
        bands[_band_name(low, high)] = {
            "low": low,
            "high": high,
            "borrower_count": int(mask.sum()),
            "debt_usd": debt,
            "debt_share": debt / total_debt if total_debt > 0 else 0.0,
        }

    watchlist_mask = (
        (active["health_factor"] <= cfg.watchlist_health_factor_max)
        | (active["total_debt_usd"] >= cfg.watchlist_debt_usd_min)
    )
    watchlist = active.loc[watchlist_mask].copy()
    watchlist = watchlist.sort_values(["health_factor", "total_debt_usd"], ascending=[True, False])

    full_census = bool(
        bootstrap_complete
        and total_candidate_addresses > 0
        and failed_ratio <= cfg.maximum_failed_call_ratio
    )
    if full_census:
        confidence = "FULL_CENSUS"
    elif bootstrap_complete and total_candidate_addresses == 0:
        confidence = "FULL_CENSUS_EMPTY_UNIVERSE"
    elif bootstrap_complete:
        confidence = "PARTIAL_RPC_COVERAGE"
    else:
        confidence = "BOOTSTRAP_INCOMPLETE"

    liquidatable = thresholds["hf_le_1_00"]
    near_cliff = thresholds["hf_le_1_20"]
    critical = thresholds["hf_le_1_05"]

    return {
        "protocol": PROTOCOL,
        "mode": MODE,
        "actionability": ACTIONABILITY,
        "risk_multiplier": None,
        "mutates_frozen_core": False,
        "mutates_state_v01": False,
        "mutates_state_ab_v01": False,
        "mutates_state_v02": False,
        "captured_at": captured.isoformat(),
        "block_number": int(block_number),
        "bootstrap_complete": bool(bootstrap_complete),
        "candidate_address_count": int(total_candidate_addresses),
        "successful_account_calls": successful_calls,
        "failed_account_calls": failed_calls,
        "account_call_coverage_ratio": float(coverage_ratio),
        "account_call_failed_ratio": float(failed_ratio),
        "data_confidence": confidence,
        "valid_full_census": full_census,
        "active_borrower_count": int(len(active)),
        "total_active_debt_usd": total_debt,
        "total_active_collateral_usd": total_collateral,
        "debt_weighted_hf_p10": weighted_quantile(active["health_factor"], active_debt, 0.10),
        "debt_weighted_hf_p25": weighted_quantile(active["health_factor"], active_debt, 0.25),
        "debt_weighted_hf_p50": weighted_quantile(active["health_factor"], active_debt, 0.50),
        "thresholds": thresholds,
        "liquidation_cliff_bands": bands,
        "liquidatable_debt_usd": liquidatable["debt_usd"],
        "liquidatable_debt_share": liquidatable["debt_share"],
        "critical_hf_le_1_05_debt_usd": critical["debt_usd"],
        "critical_hf_le_1_05_debt_share": critical["debt_share"],
        "near_cliff_hf_le_1_20_debt_usd": near_cliff["debt_usd"],
        "near_cliff_hf_le_1_20_debt_share": near_cliff["debt_share"],
        "watchlist_count": int(len(watchlist)),
        "watchlist_addresses": watchlist["address"].astype(str).tolist(),
        "interpretation": (
            "Current Aave V3 Ethereum Core borrower health distribution at one block tag. "
            "This is descriptive evidence, not a liquidation-price simulation or trading signal."
        ),
    }
