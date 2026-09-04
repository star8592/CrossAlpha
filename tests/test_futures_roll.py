from __future__ import annotations

import pandas as pd
import pytest

from crossalpha.core.futures_roll import build_roll_mtm_returns


def _bars() -> pd.DataFrame:
    dates = pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"], utc=True)
    rows = []
    f1 = [100.0, 101.0, 102.0, 103.0]
    f2 = [110.0, 111.0, 112.0, 113.0]
    for date, p1, p2 in zip(dates, f1, f2, strict=True):
        rows.append({"date": date, "contract": "F1", "close": p1})
        rows.append({"date": date, "contract": "F2", "close": p2})
    return pd.DataFrame(rows)


def test_roll_uses_same_contract_mtm_not_cross_contract_gap() -> None:
    roll_map = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"], utc=True),
            "contract": ["F1", "F1", "F2", "F2"],
        }
    )

    result = build_roll_mtm_returns(_bars(), roll_map).frame

    assert bool(result.loc[pd.Timestamp("2026-01-03", tz="UTC"), "rolled"])
    expected_roll_day_return = 112.0 / 111.0 - 1.0
    assert result.loc[pd.Timestamp("2026-01-03", tz="UTC"), "excess_return"] == pytest.approx(expected_roll_day_return)
    assert result.loc[pd.Timestamp("2026-01-03", tz="UTC"), "excess_return"] < 0.02


def test_roll_cost_is_applied_only_on_roll_day() -> None:
    roll_map = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"], utc=True),
            "contract": ["F1", "F1", "F2", "F2"],
        }
    )

    no_cost = build_roll_mtm_returns(_bars(), roll_map).frame
    with_cost = build_roll_mtm_returns(_bars(), roll_map, roll_cost_bps=2.0).frame

    roll_date = pd.Timestamp("2026-01-03", tz="UTC")
    non_roll_date = pd.Timestamp("2026-01-04", tz="UTC")
    assert with_cost.loc[roll_date, "excess_return"] == pytest.approx(no_cost.loc[roll_date, "excess_return"] - 0.0002)
    assert with_cost.loc[non_roll_date, "excess_return"] == pytest.approx(no_cost.loc[non_roll_date, "excess_return"])


def test_roll_requires_prior_price_for_new_contract() -> None:
    bars = _bars()
    bars = bars[~((bars["date"] == pd.Timestamp("2026-01-02", tz="UTC")) & (bars["contract"] == "F2"))]
    roll_map = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"], utc=True),
            "contract": ["F1", "F1", "F2"],
        }
    )

    with pytest.raises(ValueError, match="missing prior-date price"):
        build_roll_mtm_returns(bars, roll_map)
