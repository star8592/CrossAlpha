from __future__ import annotations

import pandas as pd


def build_previous_volume_roll_map(
    bars: pd.DataFrame,
    contract_metadata: pd.DataFrame,
    *,
    date_col: str = "date",
    contract_col: str = "contract",
    volume_col: str = "volume",
    expiry_col: str = "expiration_date",
    safety_days: int = 5,
) -> pd.DataFrame:
    """Build a deterministic, point-in-time-safe futures roll map.

    The contract selected for trading date `t` uses only volume observed on the prior
    available trading date. Current-day volume never participates in the decision.
    Once the map advances to a later expiry it never rolls backward. Contracts inside
    `safety_days` of expiry are ineligible; if the currently held contract becomes
    ineligible, the map advances to an eligible later expiry.

    The first bar date is warm-up only because there is no prior-day volume available;
    returned selections begin on the second available bar date.
    """
    if safety_days < 0:
        raise ValueError("safety_days must be non-negative")

    required_bars = {date_col, contract_col, volume_col}
    required_meta = {contract_col, expiry_col}
    missing_bars = sorted(required_bars - set(bars.columns))
    missing_meta = sorted(required_meta - set(contract_metadata.columns))
    if missing_bars:
        raise ValueError(f"bars missing columns: {missing_bars}")
    if missing_meta:
        raise ValueError(f"contract_metadata missing columns: {missing_meta}")

    bars = bars[[date_col, contract_col, volume_col]].copy()
    meta = contract_metadata[[contract_col, expiry_col]].copy()
    bars[date_col] = pd.to_datetime(bars[date_col], utc=True)
    meta[expiry_col] = pd.to_datetime(meta[expiry_col], utc=True)
    bars[volume_col] = pd.to_numeric(bars[volume_col], errors="coerce")

    if bars.duplicated([date_col, contract_col]).any():
        raise ValueError("bars contain duplicate date/contract rows")
    if meta.duplicated([contract_col]).any():
        raise ValueError("contract_metadata contains duplicate contracts")
    if bars[volume_col].isna().any() or (bars[volume_col] < 0).any():
        raise ValueError("volume must be non-negative and non-null")
    if meta[expiry_col].isna().any():
        raise ValueError("expiration dates must be non-null")

    merged = bars.merge(meta, on=contract_col, how="left", validate="many_to_one")
    if merged[expiry_col].isna().any():
        missing = sorted(merged.loc[merged[expiry_col].isna(), contract_col].astype(str).unique())
        raise ValueError(f"missing contract metadata for: {missing}")

    dates = sorted(merged[date_col].unique())
    if len(dates) < 2:
        raise ValueError("at least two trading dates are required")

    expiry_lookup = meta.set_index(contract_col)[expiry_col].to_dict()
    held_contract: str | None = None
    held_expiry: pd.Timestamp | None = None
    rows: list[dict[str, object]] = []

    for previous_date, current_date in zip(dates, dates[1:], strict=True):
        previous = merged.loc[merged[date_col] == previous_date].copy()
        current_available = set(
            merged.loc[merged[date_col] == current_date, contract_col].astype(str)
        )
        cutoff = pd.Timestamp(current_date) + pd.Timedelta(days=safety_days)

        eligible = previous.loc[
            (previous[expiry_col] > cutoff)
            & previous[contract_col].astype(str).isin(current_available)
        ].copy()
        if held_expiry is not None:
            eligible = eligible.loc[eligible[expiry_col] >= held_expiry]
        if eligible.empty:
            raise ValueError(f"no eligible contract for {pd.Timestamp(current_date).isoformat()}")

        # Highest prior-day volume wins; ties prefer the nearer expiry, then symbol.
        eligible[contract_col] = eligible[contract_col].astype(str)
        eligible = eligible.sort_values(
            [volume_col, expiry_col, contract_col],
            ascending=[False, True, True],
        )
        candidate = str(eligible.iloc[0][contract_col])
        candidate_expiry = pd.Timestamp(expiry_lookup[candidate])

        forced_roll = False
        decision_reason = "previous_volume"
        if held_contract is not None and held_expiry is not None:
            held_is_available = held_contract in current_available
            held_is_safe = held_expiry > cutoff
            held_prior = previous.loc[previous[contract_col].astype(str) == held_contract]

            if held_is_available and held_is_safe and not held_prior.empty:
                held_volume = float(held_prior.iloc[0][volume_col])
                candidate_volume = float(eligible.iloc[0][volume_col])
                # Stay put unless a later contract strictly exceeds held prior-day volume.
                if candidate_expiry == held_expiry or candidate_volume <= held_volume:
                    candidate = held_contract
                    candidate_expiry = held_expiry
                    decision_reason = "hold"
            else:
                forced_roll = True
                decision_reason = "expiry_safety" if not held_is_safe else "contract_unavailable"

        rolled = held_contract is not None and candidate != held_contract
        rows.append(
            {
                "date": pd.Timestamp(current_date),
                "contract": candidate,
                "expiration_date": candidate_expiry,
                "decision_volume_date": pd.Timestamp(previous_date),
                "rolled": rolled,
                "forced_roll": forced_roll,
                "decision_reason": decision_reason,
            }
        )
        held_contract = candidate
        held_expiry = candidate_expiry

    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
