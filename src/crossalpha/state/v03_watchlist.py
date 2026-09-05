from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from crossalpha.state.v03 import ACTIONABILITY, MODE, PROTOCOL


def compute_watchlist_snapshot(
    rows: pd.DataFrame,
    *,
    expected_addresses: int,
    block_number: int,
    captured_at: Any | None = None,
) -> dict[str, Any]:
    """Describe a previously selected borrower watchlist without claiming market coverage."""
    captured = pd.Timestamp(captured_at or datetime.now(timezone.utc))
    if captured.tzinfo is None:
        captured = captured.tz_localize("UTC")
    else:
        captured = captured.tz_convert("UTC")
    data = rows.copy()
    required = {"address", "success", "total_debt_usd", "health_factor"}
    missing = sorted(required - set(data.columns))
    if missing:
        raise ValueError(f"watchlist rows missing columns: {missing}")
    success = data.loc[data["success"].astype(bool)].copy()
    success["total_debt_usd"] = pd.to_numeric(success["total_debt_usd"], errors="coerce")
    success["health_factor"] = pd.to_numeric(success["health_factor"], errors="coerce")
    active = success.loc[
        (success["total_debt_usd"].fillna(0.0) > 0) & success["health_factor"].notna()
    ].copy()
    total_debt = float(active["total_debt_usd"].fillna(0.0).sum())
    liquidatable = active.loc[active["health_factor"] <= 1.0]
    critical = active.loc[active["health_factor"] <= 1.05]
    near = active.loc[active["health_factor"] <= 1.20]
    succeeded = int(len(success))
    failed = max(int(expected_addresses) - succeeded, 0)
    return {
        "protocol": PROTOCOL,
        "mode": MODE,
        "scope": "WATCHLIST_ONLY",
        "actionability": ACTIONABILITY,
        "risk_multiplier": None,
        "captured_at": captured.isoformat(),
        "block_number": int(block_number),
        "expected_watchlist_addresses": int(expected_addresses),
        "successful_account_calls": succeeded,
        "failed_account_calls": failed,
        "watchlist_call_coverage_ratio": (
            succeeded / expected_addresses if expected_addresses > 0 else 1.0
        ),
        "active_watchlist_borrower_count": int(len(active)),
        "watchlist_active_debt_usd": total_debt,
        "minimum_health_factor": (
            float(active["health_factor"].min()) if not active.empty else None
        ),
        "liquidatable_borrower_count": int(len(liquidatable)),
        "liquidatable_debt_usd": float(liquidatable["total_debt_usd"].fillna(0.0).sum()),
        "critical_hf_le_1_05_borrower_count": int(len(critical)),
        "critical_hf_le_1_05_debt_usd": float(critical["total_debt_usd"].fillna(0.0).sum()),
        "near_cliff_hf_le_1_20_borrower_count": int(len(near)),
        "near_cliff_hf_le_1_20_debt_usd": float(near["total_debt_usd"].fillna(0.0).sum()),
        "full_market_census_claim_allowed": False,
        "interpretation": (
            "Fast refresh of addresses selected by the previous valid full census. "
            "It cannot estimate whole-market borrower shares or replace a full census."
        ),
    }
