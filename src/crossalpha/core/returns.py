from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from crossalpha.core.futures_roll import FuturesRollResult, build_roll_mtm_returns
from crossalpha.core.roll_map import build_previous_volume_roll_map


@dataclass(frozen=True)
class CoreFuturesReturnResult:
    asset: str
    roll_map: pd.DataFrame
    returns: FuturesRollResult


def build_asset_futures_return_index(
    normalized_contracts: pd.DataFrame,
    asset: str,
    *,
    safety_days: int = 5,
    roll_cost_bps: float = 0.0,
) -> CoreFuturesReturnResult:
    """Build one asset's explicit volume-roll futures excess-return index.

    The input must already be point-in-time normalized from parent OHLCV and definition
    data. Contract expiration is required to be stable across observed definition
    revisions; an expiration change aborts the research build instead of silently using
    a future revision. Roll selection uses prior-day volume and MTM uses same-contract
    close-to-close price changes across roll dates.
    """
    required = {
        "date",
        "contract",
        "expiration_date",
        "close",
        "volume",
        "asset",
    }
    missing = sorted(required - set(normalized_contracts.columns))
    if missing:
        raise ValueError(f"normalized futures missing columns: {missing}")
    if safety_days < 0:
        raise ValueError("safety_days must be non-negative")
    if roll_cost_bps < 0:
        raise ValueError("roll_cost_bps must be non-negative")

    frame = normalized_contracts.loc[normalized_contracts["asset"].astype(str) == str(asset)].copy()
    if frame.empty:
        raise ValueError(f"no normalized futures rows for asset {asset}")

    frame["date"] = pd.to_datetime(frame["date"], utc=True)
    frame["expiration_date"] = pd.to_datetime(frame["expiration_date"], utc=True)
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")

    if frame[["date", "contract", "expiration_date", "close", "volume"]].isna().any().any():
        raise ValueError(f"asset {asset} contains missing roll/return inputs")
    if (frame["close"] <= 0).any():
        raise ValueError(f"asset {asset} contains non-positive close")
    if (frame["volume"] < 0).any():
        raise ValueError(f"asset {asset} contains negative volume")
    if frame.duplicated(["date", "contract"]).any():
        raise ValueError(f"asset {asset} contains duplicate date/contract rows")

    expiration_counts = frame.groupby("contract")["expiration_date"].nunique(dropna=False)
    unstable = sorted(expiration_counts.loc[expiration_counts != 1].index.astype(str))
    if unstable:
        raise ValueError(
            f"asset {asset} has contracts with changing expiration metadata: {unstable}"
        )

    metadata = (
        frame[["contract", "expiration_date"]]
        .drop_duplicates("contract")
        .sort_values("expiration_date")
        .reset_index(drop=True)
    )
    bars = frame[["date", "contract", "close", "volume"]].sort_values(["date", "contract"])
    roll_map = build_previous_volume_roll_map(
        bars,
        metadata,
        safety_days=safety_days,
    )
    result = build_roll_mtm_returns(
        bars[["date", "contract", "close"]],
        roll_map[["date", "contract"]],
        roll_cost_bps=roll_cost_bps,
    )
    return CoreFuturesReturnResult(asset=str(asset), roll_map=roll_map, returns=result)
