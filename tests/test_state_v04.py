from __future__ import annotations

import pandas as pd
import pytest

from crossalpha.state.v04 import FUNDING_SEMANTICS, compute_market_mechanics


def _rows() -> pd.DataFrame:
    generated = pd.Timestamp("2026-09-05T12:00:00Z")
    rows = []
    for asset, base in (("BTC", 100_000.0), ("ETH", 4_000.0)):
        for index, venue in enumerate(("binance", "okx", "bybit")):
            spot = base * (1 + index * 0.0001)
            perp = spot * (1 + (index - 1) * 0.0002)
            rows.append(
                {
                    "observed_at": generated - pd.Timedelta(seconds=10 + index),
                    "known_at": generated - pd.Timedelta(seconds=2),
                    "venue": venue,
                    "asset": asset,
                    "spot_mid": spot,
                    "perp_mid": perp,
                    "basis_bps": (perp / spot - 1) * 10_000,
                    "funding_semantics": FUNDING_SEMANTICS,
                    "funding_rate_settled_raw": 0.0001 * (index + 1),
                    "funding_settlement_time": generated - pd.Timedelta(hours=8),
                    "funding_interval_hours": 8.0,
                    "funding_rate_8h": 0.0001 * (index + 1),
                    "perp_spread_bps": 0.5 + index,
                    "open_interest_usd": 1_000_000.0 * (index + 1),
                }
            )
    return pd.DataFrame(rows)


def test_market_mechanics_is_descriptive_and_full_with_three_venues() -> None:
    report = compute_market_mechanics(_rows(), generated_at="2026-09-05T12:00:00Z")
    assert report["data_confidence"] == "FULL"
    assert report["actionability"] == "DESCRIPTIVE_ONLY"
    assert report["risk_multiplier"] is None
    assert report["funding_semantics"] == FUNDING_SEMANTICS
    assert report["no_composite_stress_score"] is True
    assert report["assets"]["BTC"]["valid_venue_count"] == 3
    assert report["assets"]["BTC"]["funding_comparable_venue_count"] == 3
    assert report["assets"]["BTC"]["funding_8h_median"] == pytest.approx(0.0002)


def test_unknown_funding_interval_is_excluded_not_assumed_8h() -> None:
    rows = _rows()
    mask = (rows["asset"] == "BTC") & (rows["venue"] == "okx")
    rows.loc[mask, "funding_interval_hours"] = None
    rows.loc[mask, "funding_rate_8h"] = None
    report = compute_market_mechanics(rows, generated_at="2026-09-05T12:00:00Z")
    assert report["assets"]["BTC"]["valid_venue_count"] == 3
    assert report["assets"]["BTC"]["funding_comparable_venue_count"] == 2


def test_stale_venue_is_excluded_from_mechanics() -> None:
    rows = _rows()
    mask = (rows["asset"] == "ETH") & (rows["venue"] == "bybit")
    rows.loc[mask, "observed_at"] = pd.Timestamp("2026-09-05T11:55:00Z")
    report = compute_market_mechanics(rows, generated_at="2026-09-05T12:00:00Z")
    assert report["assets"]["ETH"]["data_confidence"] == "PARTIAL"
    assert report["assets"]["ETH"]["valid_venue_count"] == 2
