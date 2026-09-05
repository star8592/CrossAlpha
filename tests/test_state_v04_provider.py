from __future__ import annotations

import pytest

from crossalpha.state.v04_provider import (
    FUNDING_SEMANTICS,
    _settled_funding_from_rows,
    parse_venue_snapshot,
)


def test_settled_funding_interval_is_inferred_from_two_settlements() -> None:
    rows = [
        {"fundingRate": "0.0001", "fundingTime": "1000000000000"},
        {"fundingRate": "0.0002", "fundingTime": str(1000000000000 + 4 * 3600 * 1000)},
    ]
    rate, hours, stamp = _settled_funding_from_rows(
        rows, rate_field="fundingRate", time_field="fundingTime"
    )
    assert rate == pytest.approx(0.0002)
    assert hours == pytest.approx(4.0)
    assert stamp is not None


def test_single_settlement_does_not_guess_interval() -> None:
    rate, hours, _ = _settled_funding_from_rows(
        [{"fundingRate": "0.0001", "fundingTime": "1000000000000"}],
        rate_field="fundingRate",
        time_field="fundingTime",
    )
    assert rate == pytest.approx(0.0001)
    assert hours is None


def test_okx_uses_realized_settled_rate_not_predicted_rate() -> None:
    t0 = 1_700_000_000_000
    payload = {
        "venue": "okx",
        "asset": "BTC",
        "spot_symbol": "BTC-USDT",
        "perp_symbol": "BTC-USDT-SWAP",
        "spot": {"code": "0", "data": [{"bidPx": "100", "askPx": "101", "ts": str(t0)}]},
        "perp": {"code": "0", "data": [{"bidPx": "101", "askPx": "102", "ts": str(t0)}]},
        "funding_history": {
            "code": "0",
            "data": [
                {
                    "fundingRate": "0.0099",
                    "realizedRate": "0.0002",
                    "fundingTime": str(t0),
                },
                {
                    "fundingRate": "0.0088",
                    "realizedRate": "0.0001",
                    "fundingTime": str(t0 - 8 * 3600 * 1000),
                },
            ],
        },
        "open_interest": {"code": "0", "data": [{"oiUsd": "123456", "ts": str(t0)}]},
    }
    row = parse_venue_snapshot(payload, known_at="2026-09-05T12:00:00Z")
    assert row["funding_semantics"] == FUNDING_SEMANTICS
    assert row["funding_rate_settled_raw"] == pytest.approx(0.0002)
    assert row["funding_interval_hours"] == pytest.approx(8.0)
    assert row["funding_rate_8h"] == pytest.approx(0.0002)


def test_bybit_history_is_settled_and_interval_normalized() -> None:
    t0 = 1_700_000_000_000
    payload = {
        "venue": "bybit",
        "asset": "ETH",
        "spot_symbol": "ETHUSDT",
        "perp_symbol": "ETHUSDT",
        "spot": {
            "retCode": 0,
            "time": t0,
            "result": {"list": [{"bid1Price": "2000", "ask1Price": "2001"}]},
        },
        "perp": {
            "retCode": 0,
            "time": t0,
            "result": {
                "list": [
                    {
                        "bid1Price": "2001",
                        "ask1Price": "2002",
                        "markPrice": "2001.5",
                        "indexPrice": "2000.5",
                        "openInterestValue": "999999",
                    }
                ]
            },
        },
        "funding_history": {
            "retCode": 0,
            "result": {
                "list": [
                    {"fundingRate": "0.0003", "fundingRateTimestamp": str(t0)},
                    {
                        "fundingRate": "0.0001",
                        "fundingRateTimestamp": str(t0 - 4 * 3600 * 1000),
                    },
                ]
            },
        },
    }
    row = parse_venue_snapshot(payload, known_at="2026-09-05T12:00:00Z")
    assert row["funding_rate_settled_raw"] == pytest.approx(0.0003)
    assert row["funding_interval_hours"] == pytest.approx(4.0)
    assert row["funding_rate_8h"] == pytest.approx(0.0006)
