from __future__ import annotations

import pandas as pd
import pytest

from crossalpha.core.free_provider import (
    FreeCoreRange,
    free_credential_status,
    parse_binance_klines,
    parse_fred_observations,
    parse_tiingo_eod_payload,
    validate_fred_api_key,
    validate_tiingo_token,
)


def test_free_core_range_accepts_utc_timestamps() -> None:
    value = FreeCoreRange("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
    assert value.start.startswith("2026-01-01")
    with pytest.raises(ValueError, match="end must be after start"):
        FreeCoreRange("2026-02-01", "2026-01-01")


def test_tiingo_parser_preserves_raw_and_adjusted_prices() -> None:
    payload = [
        {
            "date": "2026-01-02T00:00:00.000Z",
            "open": 100.0,
            "high": 102.0,
            "low": 99.0,
            "close": 101.0,
            "volume": 1000,
            "adjOpen": 99.0,
            "adjHigh": 100.98,
            "adjLow": 98.01,
            "adjClose": 99.99,
            "adjVolume": 1000,
            "divCash": 0.5,
            "splitFactor": 1.0,
        }
    ]
    frame = parse_tiingo_eod_payload("US_EQUITY", "SPY", payload)
    assert frame.iloc[0]["economic_asset"] == "US_EQUITY"
    assert frame.iloc[0]["close"] == pytest.approx(101.0)
    assert frame.iloc[0]["adj_close"] == pytest.approx(99.99)
    assert frame.iloc[0]["source"] == "tiingo_eod"


def test_binance_parser_builds_daily_spot_rows() -> None:
    payload = [
        [
            1767225600000,
            "90000.0",
            "91000.0",
            "89000.0",
            "90500.0",
            "100.0",
            1767311999999,
            "9050000.0",
            1234,
            "55.0",
            "4977500.0",
            "0",
        ]
    ]
    frame = parse_binance_klines("BTC", "BTCUSDT", payload)
    assert frame.iloc[0]["economic_asset"] == "BTC"
    assert frame.iloc[0]["close"] == pytest.approx(90500.0)
    assert frame.iloc[0]["trade_count"] == 1234
    assert frame.iloc[0]["source"] == "binance_spot_public"


def test_fred_parser_keeps_missing_rate_unknown() -> None:
    payload = {
        "observations": [
            {"date": "2026-01-02", "value": "4.25"},
            {"date": "2026-01-03", "value": "."},
        ]
    }
    frame = parse_fred_observations("DGS3MO", payload)
    assert frame.iloc[0]["rate_percent"] == pytest.approx(4.25)
    assert pd.isna(frame.iloc[1]["rate_percent"])


def test_tiingo_credential_rejects_unicode_placeholder_before_http() -> None:
    valid, error = validate_tiingo_token("你的token")
    assert valid is False
    assert error is not None
    assert "ASCII" in error


def test_tiingo_credential_accepts_transport_safe_ascii_token() -> None:
    valid, error = validate_tiingo_token("0123456789abcdef0123456789abcdef01234567")
    assert valid is True
    assert error is None


def test_fred_credential_uses_documented_32_lowercase_alphanumeric_shape() -> None:
    valid, error = validate_fred_api_key("abc123")
    assert valid is False
    assert error is not None
    valid, error = validate_fred_api_key("abcdefghijklmnopqrstuvwxyz123456")
    assert valid is True
    assert error is None


def test_free_credential_status_requires_both_valid_credentials() -> None:
    bad = free_credential_status("你的token", "abcdefghijklmnopqrstuvwxyz123456")
    assert bad["ready"] is False
    assert bad["tiingo_token_configured"] is True
    assert bad["tiingo_token_valid"] is False

    good = free_credential_status(
        "0123456789abcdef0123456789abcdef01234567",
        "abcdefghijklmnopqrstuvwxyz123456",
    )
    assert good["ready"] is True
    assert good["credential_errors"] == []
