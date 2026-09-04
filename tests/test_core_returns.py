from __future__ import annotations

import pandas as pd
import pytest

from crossalpha.core.returns import build_asset_futures_return_index


def _normalized() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    data = {
        "2026-09-01": {"F1": (100.0, 100), "F2": (110.0, 50)},
        "2026-09-02": {"F1": (101.0, 80), "F2": (111.0, 120)},
        "2026-09-03": {"F1": (102.0, 50), "F2": (112.0, 150)},
        "2026-09-04": {"F1": (103.0, 20), "F2": (113.0, 160)},
    }
    for day, contracts in data.items():
        for contract, (close, volume) in contracts.items():
            rows.append(
                {
                    "date": day,
                    "asset": "ES",
                    "contract": contract,
                    "expiration_date": "2026-09-30" if contract == "F1" else "2026-12-31",
                    "close": close,
                    "volume": volume,
                }
            )
    return pd.DataFrame(rows)


def test_explicit_roll_pipeline_uses_new_contract_on_both_sides_of_roll_return() -> None:
    result = build_asset_futures_return_index(_normalized(), "ES", safety_days=2)

    assert list(result.roll_map["contract"]) == ["F1", "F2", "F2"]
    mtm = result.returns.frame
    assert bool(mtm.iloc[1]["rolled"]) is True
    assert mtm.iloc[1]["previous_close"] == pytest.approx(111.0)
    assert mtm.iloc[1]["close"] == pytest.approx(112.0)
    assert mtm.iloc[1]["excess_return"] == pytest.approx(112.0 / 111.0 - 1.0)
    assert mtm.iloc[2]["excess_return"] == pytest.approx(113.0 / 112.0 - 1.0)


def test_roll_cost_is_applied_only_on_actual_roll_date() -> None:
    result = build_asset_futures_return_index(
        _normalized(),
        "ES",
        safety_days=2,
        roll_cost_bps=10.0,
    )
    mtm = result.returns.frame
    assert mtm.iloc[1]["excess_return"] == pytest.approx(112.0 / 111.0 - 1.0 - 0.001)
    assert mtm.iloc[2]["excess_return"] == pytest.approx(113.0 / 112.0 - 1.0)


def test_changing_expiration_metadata_aborts_return_build() -> None:
    frame = _normalized()
    mask = (frame["contract"] == "F1") & (frame["date"] == "2026-09-04")
    frame.loc[mask, "expiration_date"] = "2026-10-01"

    with pytest.raises(ValueError, match="changing expiration metadata"):
        build_asset_futures_return_index(frame, "ES")
