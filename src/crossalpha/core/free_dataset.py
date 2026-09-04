from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from crossalpha.core.free_provider import (
    FREE_CRYPTO_PROXIES,
    FREE_TRADFI_PROXIES,
    FRED_CASH_SERIES,
    FreeCoreRange,
)


def _safe_slug(value: str) -> str:
    return value.replace(":", "-").replace("/", "-").replace(" ", "_")


def _range_dir(value: FreeCoreRange) -> Path:
    return Path(f"start={_safe_slug(value.start)}", f"end={_safe_slug(value.end)}")


def _paths(data_root: Path, value: FreeCoreRange) -> dict[str, Path]:
    range_dir = _range_dir(value)
    proxy_root = data_root / "canonical" / "core" / "free_proxy_daily" / range_dir
    cash_root = data_root / "canonical" / "core" / "cash_rate" / range_dir
    derived_root = data_root / "derived" / "core" / "free_v01" / range_dir
    return {
        "tradfi": proxy_root / "tradfi.parquet",
        "crypto": proxy_root / "crypto.parquet",
        "cash": cash_root / f"{FRED_CASH_SERIES}.parquet",
        "returns": derived_root / "asset_returns.parquet",
        "quality": data_root / "manifests" / "free_core_quality.json",
    }


def _series_audit(
    frame: pd.DataFrame,
    *,
    asset_col: str,
    price_col: str,
    expected_assets: set[str],
    source_name: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, Any]:
    missing_columns = sorted({"date", asset_col, price_col} - set(frame.columns))
    if missing_columns:
        return {
            "ok": False,
            "source": source_name,
            "missing_columns": missing_columns,
            "missing_assets": sorted(expected_assets),
            "series": {},
        }

    data = frame.copy()
    data["date"] = pd.to_datetime(data["date"], utc=True)
    data[price_col] = pd.to_numeric(data[price_col], errors="coerce")
    found_assets = set(data[asset_col].dropna().astype(str))
    series: dict[str, Any] = {}
    source_ok = True

    for asset in sorted(expected_assets | found_assets):
        part = data.loc[data[asset_col].astype(str) == asset].sort_values("date").copy()
        if part.empty:
            series[asset] = {
                "ok": False,
                "rows": 0,
                "start": None,
                "end": None,
                "duplicate_dates": 0,
                "null_price": 0,
                "non_positive_price": 0,
                "rows_before_start": 0,
                "rows_at_or_after_exclusive_end": 0,
                "max_calendar_gap_days": None,
            }
            source_ok = False
            continue

        duplicate_dates = int(part["date"].duplicated().sum())
        null_price = int(part[price_col].isna().sum())
        non_positive = int((part[price_col].dropna() <= 0).sum())
        rows_before_start = int((part["date"] < start).sum())
        rows_after_end = int((part["date"] >= end).sum())
        gaps = part["date"].diff().dt.total_seconds().div(86_400.0).dropna()
        max_gap = float(gaps.max()) if not gaps.empty else None
        item_ok = (
            duplicate_dates == 0
            and null_price == 0
            and non_positive == 0
            and rows_before_start == 0
            and rows_after_end == 0
        )
        source_ok = source_ok and item_ok
        series[asset] = {
            "ok": item_ok,
            "rows": int(len(part)),
            "start": part["date"].iloc[0].isoformat(),
            "end": part["date"].iloc[-1].isoformat(),
            "duplicate_dates": duplicate_dates,
            "null_price": null_price,
            "non_positive_price": non_positive,
            "rows_before_start": rows_before_start,
            "rows_at_or_after_exclusive_end": rows_after_end,
            "max_calendar_gap_days": max_gap,
        }

    missing_assets = sorted(expected_assets - found_assets)
    return {
        "ok": source_ok and not missing_assets,
        "source": source_name,
        "missing_columns": [],
        "missing_assets": missing_assets,
        "series": series,
    }


def audit_free_core(
    data_root: Path,
    value: FreeCoreRange,
    *,
    write_report: bool = True,
) -> dict[str, Any]:
    paths = _paths(data_root, value)
    missing_files = [
        str(path)
        for key, path in paths.items()
        if key in {"tradfi", "crypto", "cash"} and not path.exists()
    ]
    if missing_files:
        report = {
            "ok": False,
            "mode": "free_only",
            "data_cost_usd": 0,
            "range_semantics": "start_inclusive_end_exclusive",
            "start": value.start,
            "end": value.end,
            "missing_files": missing_files,
        }
        if write_report:
            paths["quality"].parent.mkdir(parents=True, exist_ok=True)
            paths["quality"].write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return report

    start = pd.to_datetime(value.start, utc=True).normalize()
    end = pd.to_datetime(value.end, utc=True).normalize()
    tradfi = pd.read_parquet(paths["tradfi"])
    crypto = pd.read_parquet(paths["crypto"])
    cash = pd.read_parquet(paths["cash"])

    tradfi_audit = _series_audit(
        tradfi,
        asset_col="economic_asset",
        price_col="adj_close",
        expected_assets=set(FREE_TRADFI_PROXIES),
        source_name="tiingo_eod",
        start=start,
        end=end,
    )
    crypto_audit = _series_audit(
        crypto,
        asset_col="economic_asset",
        price_col="close",
        expected_assets=set(FREE_CRYPTO_PROXIES),
        source_name="binance_spot_public",
        start=start,
        end=end,
    )

    cash = cash.copy()
    cash["date"] = pd.to_datetime(cash["date"], utc=True)
    cash["rate_percent"] = pd.to_numeric(cash["rate_percent"], errors="coerce")
    cash_duplicates = int(cash["date"].duplicated().sum())
    cash_before_start = int((cash["date"] < start).sum())
    cash_after_end = int((cash["date"] >= end).sum())
    known_cash = cash.loc[cash["rate_percent"].notna()].sort_values("date")
    cash_audit = {
        "ok": bool(
            len(cash) > 0
            and cash_duplicates == 0
            and cash_before_start == 0
            and cash_after_end == 0
            and not known_cash.empty
        ),
        "source": "fred",
        "series_id": FRED_CASH_SERIES,
        "rows": int(len(cash)),
        "known_rate_rows": int(len(known_cash)),
        "missing_rate_rows": int(cash["rate_percent"].isna().sum()),
        "duplicate_dates": cash_duplicates,
        "rows_before_start": cash_before_start,
        "rows_at_or_after_exclusive_end": cash_after_end,
        "start": cash["date"].min().isoformat() if not cash.empty else None,
        "end": cash["date"].max().isoformat() if not cash.empty else None,
        "first_known_rate": known_cash["date"].iloc[0].isoformat() if not known_cash.empty else None,
        "last_known_rate": known_cash["date"].iloc[-1].isoformat() if not known_cash.empty else None,
    }

    report = {
        "ok": bool(tradfi_audit["ok"] and crypto_audit["ok"] and cash_audit["ok"]),
        "mode": "free_only",
        "data_cost_usd": 0,
        "range_semantics": "start_inclusive_end_exclusive",
        "start": value.start,
        "end": value.end,
        "missing_files": [],
        "tradfi": tradfi_audit,
        "crypto": crypto_audit,
        "cash": cash_audit,
    }
    if write_report:
        paths["quality"].parent.mkdir(parents=True, exist_ok=True)
        tmp = paths["quality"].with_suffix(".json.tmp")
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(paths["quality"])
    return report


def build_free_core_returns(data_root: Path, value: FreeCoreRange) -> dict[str, Any]:
    quality = audit_free_core(data_root, value)
    if not quality.get("ok"):
        raise ValueError("free Core quality gate failed; inspect manifests/free_core_quality.json")

    paths = _paths(data_root, value)
    tradfi = pd.read_parquet(paths["tradfi"]).copy()
    crypto = pd.read_parquet(paths["crypto"]).copy()
    cash = pd.read_parquet(paths["cash"]).copy()

    tradfi["date"] = pd.to_datetime(tradfi["date"], utc=True)
    tradfi["price"] = pd.to_numeric(tradfi["adj_close"], errors="coerce")
    tradfi["return"] = tradfi.groupby("economic_asset", sort=False)["price"].pct_change(
        fill_method=None
    )
    tradfi_out = tradfi[
        ["date", "economic_asset", "source", "symbol", "price", "return"]
    ].copy()

    crypto["date"] = pd.to_datetime(crypto["date"], utc=True)
    crypto["price"] = pd.to_numeric(crypto["close"], errors="coerce")
    crypto["return"] = crypto.groupby("economic_asset", sort=False)["price"].pct_change(
        fill_method=None
    )
    crypto_out = crypto[
        ["date", "economic_asset", "source", "symbol", "price", "return"]
    ].copy()

    cash["date"] = pd.to_datetime(cash["date"], utc=True)
    cash["rate_percent"] = pd.to_numeric(cash["rate_percent"], errors="coerce")
    start = pd.to_datetime(value.start, utc=True).normalize()
    end = pd.to_datetime(value.end, utc=True).normalize()
    calendar = pd.DataFrame(
        {"date": pd.date_range(start=start, end=end, inclusive="left", freq="D")}
    )
    known_rates = cash.loc[
        cash["rate_percent"].notna(), ["date", "rate_percent"]
    ].sort_values("date")
    cash_daily = pd.merge_asof(
        calendar.sort_values("date"),
        known_rates,
        on="date",
        direction="backward",
        allow_exact_matches=False,
    )
    cash_daily["economic_asset"] = "CASH"
    cash_daily["source"] = "fred"
    cash_daily["symbol"] = FRED_CASH_SERIES
    cash_daily["price"] = pd.NA
    cash_daily["return"] = (
        (1.0 + cash_daily["rate_percent"] / 100.0) ** (1.0 / 365.0) - 1.0
    )
    cash_out = cash_daily[
        ["date", "economic_asset", "source", "symbol", "price", "return"]
    ].copy()

    combined = pd.concat([tradfi_out, crypto_out, cash_out], ignore_index=True)
    combined = combined.sort_values(["date", "economic_asset"]).reset_index(drop=True)
    if combined.duplicated(["date", "economic_asset"]).any():
        raise ValueError("free Core returns contain duplicate date/economic_asset rows")

    paths["returns"].parent.mkdir(parents=True, exist_ok=True)
    tmp = paths["returns"].with_suffix(".parquet.tmp")
    combined.to_parquet(tmp, index=False)
    tmp.replace(paths["returns"])

    coverage: dict[str, Any] = {}
    for asset, part in combined.groupby("economic_asset", sort=True):
        valid = part.loc[part["return"].notna()]
        coverage[str(asset)] = {
            "rows": int(len(part)),
            "valid_return_rows": int(len(valid)),
            "first_return": valid["date"].iloc[0].isoformat() if not valid.empty else None,
            "last_return": valid["date"].iloc[-1].isoformat() if not valid.empty else None,
        }

    return {
        "mode": "free_only",
        "data_cost_usd": 0,
        "range_semantics": "start_inclusive_end_exclusive",
        "rows": int(len(combined)),
        "assets": sorted(coverage),
        "coverage": coverage,
        "output": str(paths["returns"]),
        "quality_report": str(paths["quality"]),
    }
