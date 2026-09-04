from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from crossalpha.core.free_dataset import audit_free_core, build_free_core_returns
from crossalpha.core.free_provider import FREE_CRYPTO_PROXIES, FREE_TRADFI_PROXIES, FreeCoreRange


def _write_fixture(root: Path, value: FreeCoreRange, *, include_vendor_end: bool = False) -> None:
    range_dir = Path(f"start={value.start}", f"end={value.end}")
    proxy = root / "canonical" / "core" / "free_proxy_daily" / range_dir
    cash = root / "canonical" / "core" / "cash_rate" / range_dir
    proxy.mkdir(parents=True, exist_ok=True)
    cash.mkdir(parents=True, exist_ok=True)

    tradfi_rows = []
    for offset, (asset, symbol) in enumerate(FREE_TRADFI_PROXIES.items()):
        start = pd.Timestamp("2026-01-02", tz="UTC") + pd.Timedelta(days=offset)
        for i, price in enumerate((100.0, 101.0, 103.0)):
            tradfi_rows.append(
                {
                    "date": start + pd.Timedelta(days=i),
                    "economic_asset": asset,
                    "source": "tiingo_eod",
                    "symbol": symbol,
                    "adj_close": price,
                }
            )
        if include_vendor_end:
            tradfi_rows.append(
                {
                    "date": pd.Timestamp(value.end, tz="UTC"),
                    "economic_asset": asset,
                    "source": "tiingo_eod",
                    "symbol": symbol,
                    "adj_close": 104.0,
                }
            )
    pd.DataFrame(tradfi_rows).to_parquet(proxy / "tradfi.parquet", index=False)

    crypto_rows = []
    for asset, symbol in FREE_CRYPTO_PROXIES.items():
        for i, price in enumerate((200.0, 220.0, 198.0)):
            crypto_rows.append(
                {
                    "date": pd.Timestamp("2026-01-03", tz="UTC") + pd.Timedelta(days=i),
                    "economic_asset": asset,
                    "source": "binance_spot_public",
                    "symbol": symbol,
                    "close": price,
                }
            )
    pd.DataFrame(crypto_rows).to_parquet(proxy / "crypto.parquet", index=False)

    cash_rows = [
        {
            "date": pd.Timestamp("2026-01-02", tz="UTC"),
            "series_id": "DGS3MO",
            "rate_percent": 4.0,
        },
        {
            "date": pd.Timestamp("2026-01-03", tz="UTC"),
            "series_id": "DGS3MO",
            "rate_percent": None,
        },
        {
            "date": pd.Timestamp("2026-01-05", tz="UTC"),
            "series_id": "DGS3MO",
            "rate_percent": 4.1,
        },
    ]
    if include_vendor_end:
        cash_rows.append(
            {
                "date": pd.Timestamp(value.end, tz="UTC"),
                "series_id": "DGS3MO",
                "rate_percent": 4.2,
            }
        )
    pd.DataFrame(cash_rows).to_parquet(cash / "DGS3MO.parquet", index=False)


def test_free_core_audit_accepts_availability_aware_starts(tmp_path: Path) -> None:
    value = FreeCoreRange("2026-01-01", "2026-01-10")
    _write_fixture(tmp_path, value)

    report = audit_free_core(tmp_path, value)
    assert report["ok"] is True
    assert report["data_cost_usd"] == 0
    assert report["tradfi"]["series"]["COPPER"]["rows"] == 3
    assert report["crypto"]["series"]["BTC"]["rows"] == 3
    assert report["cash"]["missing_rate_rows"] == 1


def test_free_core_returns_do_not_fill_before_asset_inception(tmp_path: Path) -> None:
    value = FreeCoreRange("2026-01-01", "2026-01-10")
    _write_fixture(tmp_path, value)

    result = build_free_core_returns(tmp_path, value)
    frame = pd.read_parquet(result["output"])

    copper = frame.loc[frame["economic_asset"] == "COPPER"].sort_values("date")
    assert len(copper) == 3
    assert pd.isna(copper.iloc[0]["return"])
    assert copper.iloc[1]["return"] > 0

    btc = frame.loc[frame["economic_asset"] == "BTC"].sort_values("date")
    assert len(btc) == 3
    assert pd.isna(btc.iloc[0]["return"])
    assert btc.iloc[1]["return"] == pytest.approx(0.1)


def test_cash_return_uses_only_prior_known_rate(tmp_path: Path) -> None:
    value = FreeCoreRange("2026-01-01", "2026-01-10")
    _write_fixture(tmp_path, value)

    result = build_free_core_returns(tmp_path, value)
    frame = pd.read_parquet(result["output"])
    cash = frame.loc[frame["economic_asset"] == "CASH"].set_index("date")

    assert pd.isna(cash.loc[pd.Timestamp("2026-01-02", tz="UTC"), "return"])
    assert cash.loc[pd.Timestamp("2026-01-03", tz="UTC"), "return"] > 0
    assert cash.loc[pd.Timestamp("2026-01-04", tz="UTC"), "return"] > 0


def test_exact_vendor_end_is_reported_but_dropped_from_derived(tmp_path: Path) -> None:
    value = FreeCoreRange("2026-01-01", "2026-01-10")
    _write_fixture(tmp_path, value, include_vendor_end=True)

    report = audit_free_core(tmp_path, value)
    assert report["ok"] is True
    assert report["tradfi"]["series"]["US_EQUITY"]["rows_exactly_at_exclusive_end"] == 1
    assert report["tradfi"]["series"]["US_EQUITY"]["rows_strictly_after_exclusive_end"] == 0
    assert report["cash"]["rows_exactly_at_exclusive_end"] == 1

    result = build_free_core_returns(tmp_path, value)
    frame = pd.read_parquet(result["output"])
    assert frame["date"].max() < pd.Timestamp(value.end, tz="UTC")
