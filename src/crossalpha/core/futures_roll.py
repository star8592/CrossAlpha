from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class FuturesRollResult:
    frame: pd.DataFrame

    @property
    def returns(self) -> pd.Series:
        return self.frame["excess_return"]

    @property
    def return_index(self) -> pd.Series:
        return self.frame["return_index"]


def build_roll_mtm_returns(
    bars: pd.DataFrame,
    roll_map: pd.DataFrame,
    *,
    date_col: str = "date",
    contract_col: str = "contract",
    close_col: str = "close",
    roll_cost_bps: float = 0.0,
) -> FuturesRollResult:
    """Build same-contract close-to-close futures returns across explicit rolls.

    `roll_map[contract_col]` is the contract selected for each date. On a roll date,
    PnL is measured using the newly selected contract on both the current and prior
    date. This deliberately avoids treating the price-level gap between two different
    expiries as investment return.

    The supplied roll map must itself be point-in-time safe. For example, a mapping
    based on previous-day volume/open interest is acceptable; a map using current-day
    closing volume to assume an earlier execution is not.
    """
    required_bar_cols = {date_col, contract_col, close_col}
    required_map_cols = {date_col, contract_col}
    if not required_bar_cols.issubset(bars.columns):
        missing = sorted(required_bar_cols - set(bars.columns))
        raise ValueError(f"bars missing columns: {missing}")
    if not required_map_cols.issubset(roll_map.columns):
        missing = sorted(required_map_cols - set(roll_map.columns))
        raise ValueError(f"roll_map missing columns: {missing}")
    if roll_cost_bps < 0:
        raise ValueError("roll_cost_bps must be non-negative")

    bars = bars[[date_col, contract_col, close_col]].copy()
    roll_map = roll_map[[date_col, contract_col]].copy()
    bars[date_col] = pd.to_datetime(bars[date_col], utc=True)
    roll_map[date_col] = pd.to_datetime(roll_map[date_col], utc=True)

    if bars.duplicated([date_col, contract_col]).any():
        raise ValueError("bars contain duplicate date/contract rows")
    if roll_map.duplicated([date_col]).any():
        raise ValueError("roll_map contains duplicate dates")
    if (bars[close_col] <= 0).any() or bars[close_col].isna().any():
        raise ValueError("bars close prices must be positive and non-null")

    bars = bars.sort_values([date_col, contract_col]).set_index([date_col, contract_col])
    roll_map = roll_map.sort_values(date_col).reset_index(drop=True)
    if roll_map.empty:
        raise ValueError("roll_map is empty")

    rows: list[dict[str, object]] = []
    previous_date: pd.Timestamp | None = None
    previous_selected_contract: str | None = None
    return_index = 1.0

    for row in roll_map.itertuples(index=False):
        current_date = getattr(row, date_col)
        selected_contract = str(getattr(row, contract_col))
        key_current = (current_date, selected_contract)
        if key_current not in bars.index:
            raise ValueError(f"missing current price for {selected_contract} on {current_date}")
        current_close = float(bars.loc[key_current, close_col])

        rolled = previous_selected_contract is not None and selected_contract != previous_selected_contract
        previous_close: float | None = None
        excess_return: float | None = None

        if previous_date is not None:
            key_previous_same_contract = (previous_date, selected_contract)
            if key_previous_same_contract not in bars.index:
                raise ValueError(
                    "missing prior-date price for newly selected contract "
                    f"{selected_contract} on {previous_date}; cannot construct gap-free MTM return"
                )
            previous_close = float(bars.loc[key_previous_same_contract, close_col])
            excess_return = current_close / previous_close - 1.0
            if rolled and roll_cost_bps:
                excess_return -= roll_cost_bps / 10_000.0
            return_index *= 1.0 + excess_return

        rows.append(
            {
                "date": current_date,
                "contract": selected_contract,
                "previous_contract": previous_selected_contract,
                "close": current_close,
                "previous_close": previous_close,
                "rolled": rolled,
                "excess_return": excess_return,
                "return_index": return_index,
            }
        )
        previous_date = current_date
        previous_selected_contract = selected_contract

    result = pd.DataFrame(rows).set_index("date")
    return FuturesRollResult(frame=result)
