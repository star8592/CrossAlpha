from __future__ import annotations

import pandas as pd
import pytest

from crossalpha.core.contracts import normalize_parent_futures_daily


def test_parent_normalization_drops_spreads_and_keeps_outrights() -> None:
    bars = pd.DataFrame(
        [
            {
                "ts_event": "2026-01-05T22:00:00Z",
                "instrument_id": 1,
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
                "volume": 1000,
            },
            {
                "ts_event": "2026-01-05T22:00:00Z",
                "instrument_id": 2,
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.5,
                "volume": 500,
            },
        ]
    )
    definitions = pd.DataFrame(
        [
            {
                "ts_recv": "2025-12-01T00:00:00Z",
                "instrument_id": 1,
                "raw_symbol": "ESH6",
                "expiration": "2026-03-20T13:30:00Z",
                "instrument_class": "F",
                "asset": "ES",
                "contract_multiplier": 50,
            },
            {
                "ts_recv": "2025-12-01T00:00:00Z",
                "instrument_id": 2,
                "raw_symbol": "ESH6-ESM6",
                "expiration": "2026-03-20T13:30:00Z",
                "instrument_class": "S",
                "asset": "ES",
                "contract_multiplier": 50,
            },
        ]
    )

    result = normalize_parent_futures_daily(bars, definitions)
    assert len(result) == 1
    assert result.iloc[0]["contract"] == "ESH6"
    assert result.iloc[0]["asset"] == "ES"


def test_definition_join_is_point_in_time_not_latest_revision() -> None:
    bars = pd.DataFrame(
        [
            {
                "ts_event": "2026-01-15T22:00:00Z",
                "instrument_id": 1,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 100,
            },
            {
                "ts_event": "2026-02-15T22:00:00Z",
                "instrument_id": 1,
                "open": 101.0,
                "high": 102.0,
                "low": 100.0,
                "close": 101.5,
                "volume": 200,
            },
        ]
    )
    definitions = pd.DataFrame(
        [
            {
                "ts_recv": "2026-01-01T00:00:00Z",
                "instrument_id": 1,
                "raw_symbol": "ESH6",
                "expiration": "2026-03-20T13:30:00Z",
                "instrument_class": "F",
                "contract_multiplier": 50,
            },
            {
                "ts_recv": "2026-02-01T00:00:00Z",
                "instrument_id": 1,
                "raw_symbol": "ESH6",
                "expiration": "2026-03-20T13:30:00Z",
                "instrument_class": "F",
                "contract_multiplier": 25,
            },
        ]
    )

    result = normalize_parent_futures_daily(bars, definitions)
    assert list(result["contract_multiplier"]) == [50, 25]
    assert result.iloc[0]["definition_known_at"] == pd.Timestamp("2026-01-01", tz="UTC")
    assert result.iloc[1]["definition_known_at"] == pd.Timestamp("2026-02-01", tz="UTC")


def test_future_instrument_class_integer_code_is_supported() -> None:
    bars = pd.DataFrame(
        [
            {
                "ts_event": "2026-01-05T22:00:00Z",
                "instrument_id": 1,
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
                "volume": 1000,
            }
        ]
    )
    definitions = pd.DataFrame(
        [
            {
                "ts_recv": "2025-12-01T00:00:00Z",
                "instrument_id": 1,
                "raw_symbol": "ESH6",
                "expiration": "2026-03-20T13:30:00Z",
                "instrument_class": ord("F"),
            }
        ]
    )

    result = normalize_parent_futures_daily(bars, definitions)
    assert result.iloc[0]["contract"] == "ESH6"


def test_normalization_rejects_negative_volume() -> None:
    bars = pd.DataFrame(
        [
            {
                "ts_event": "2026-01-05T22:00:00Z",
                "instrument_id": 1,
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
                "volume": -1,
            }
        ]
    )
    definitions = pd.DataFrame(
        [
            {
                "ts_recv": "2025-12-01T00:00:00Z",
                "instrument_id": 1,
                "raw_symbol": "ESH6",
                "expiration": "2026-03-20T13:30:00Z",
                "instrument_class": "F",
            }
        ]
    )

    with pytest.raises(ValueError, match="negative volume"):
        normalize_parent_futures_daily(bars, definitions)
