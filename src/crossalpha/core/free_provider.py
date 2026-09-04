from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pandas as pd


FREE_TRADFI_PROXIES: dict[str, str] = {
    "US_EQUITY": "SPY",
    "US_GROWTH": "QQQ",
    "GOLD": "GLD",
    "SILVER": "SLV",
    "COPPER": "CPER",
    "WTI": "USO",
}

FREE_CRYPTO_PROXIES: dict[str, str] = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
}

FRED_CASH_SERIES = "DGS3MO"


@dataclass(frozen=True)
class FreeCoreRange:
    start: str
    end: str

    def __post_init__(self) -> None:
        start = pd.Timestamp(self.start, tz="UTC")
        end = pd.Timestamp(self.end, tz="UTC")
        if end <= start:
            raise ValueError("end must be after start")


def _safe_slug(value: str) -> str:
    return value.replace(":", "-").replace("/", "-").replace(" ", "_")


def _range_dir(value: FreeCoreRange) -> Path:
    return Path(f"start={_safe_slug(value.start)}", f"end={_safe_slug(value.end)}")


def parse_tiingo_eod_payload(
    economic_asset: str,
    ticker: str,
    payload: list[dict[str, Any]],
) -> pd.DataFrame:
    if not isinstance(payload, list):
        raise ValueError("Tiingo EOD payload must be a list")
    rows: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "date": item.get("date"),
                "economic_asset": economic_asset,
                "source": "tiingo_eod",
                "symbol": ticker,
                "open": item.get("open"),
                "high": item.get("high"),
                "low": item.get("low"),
                "close": item.get("close"),
                "volume": item.get("volume"),
                "adj_open": item.get("adjOpen"),
                "adj_high": item.get("adjHigh"),
                "adj_low": item.get("adjLow"),
                "adj_close": item.get("adjClose"),
                "adj_volume": item.get("adjVolume"),
                "div_cash": item.get("divCash"),
                "split_factor": item.get("splitFactor"),
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame["date"] = pd.to_datetime(frame["date"], utc=True)
    for column in (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "adj_open",
        "adj_high",
        "adj_low",
        "adj_close",
        "adj_volume",
        "div_cash",
        "split_factor",
    ):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if frame["date"].duplicated().any():
        raise ValueError(f"duplicate Tiingo dates for {ticker}")
    if frame["adj_close"].isna().any() or (frame["adj_close"] <= 0).any():
        raise ValueError(f"invalid adjusted close for {ticker}")
    return frame.sort_values("date").reset_index(drop=True)


def parse_binance_klines(
    economic_asset: str,
    symbol: str,
    payload: list[list[Any]],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, list) or len(item) < 11:
            raise ValueError(f"invalid Binance kline row for {symbol}")
        rows.append(
            {
                "date": pd.to_datetime(int(item[0]), unit="ms", utc=True),
                "economic_asset": economic_asset,
                "source": "binance_spot_public",
                "symbol": symbol,
                "open": float(item[1]),
                "high": float(item[2]),
                "low": float(item[3]),
                "close": float(item[4]),
                "volume": float(item[5]),
                "quote_volume": float(item[7]),
                "trade_count": int(item[8]),
                "taker_buy_base_volume": float(item[9]),
                "taker_buy_quote_volume": float(item[10]),
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    if frame["date"].duplicated().any():
        raise ValueError(f"duplicate Binance dates for {symbol}")
    if (frame[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError(f"invalid Binance OHLC for {symbol}")
    return frame.sort_values("date").reset_index(drop=True)


def parse_fred_observations(series_id: str, payload: dict[str, Any]) -> pd.DataFrame:
    observations = payload.get("observations")
    if not isinstance(observations, list):
        raise ValueError("FRED payload missing observations")
    rows: list[dict[str, Any]] = []
    for item in observations:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        rate = None if value in (None, ".") else float(value)
        rows.append(
            {
                "date": item.get("date"),
                "series_id": series_id,
                "rate_percent": rate,
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame["date"] = pd.to_datetime(frame["date"], utc=True)
    frame["rate_percent"] = pd.to_numeric(frame["rate_percent"], errors="coerce")
    return frame.sort_values("date").reset_index(drop=True)


class FreeCoreProvider:
    """Zero-data-fee Core downloader.

    Required sources:
    - Tiingo Starter ($0 account token) for investable TradFi ETF/ETP proxies.
    - Binance public market-data API for BTC/ETH spot daily OHLCV (no API key).
    - FRED API (free account key) for the cash rate series.

    Raw vendor responses are saved locally and canonical Parquet is rebuildable.
    """

    TIINGO_BASE = "https://api.tiingo.com/tiingo/daily"
    BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
    FRED_OBSERVATIONS = "https://api.stlouisfed.org/fred/series/observations"

    def __init__(
        self,
        *,
        tiingo_token: str,
        fred_api_key: str,
        timeout: float = 30.0,
    ) -> None:
        if not tiingo_token:
            raise ValueError("TIINGO_API_TOKEN is required; the Starter account is free")
        if not fred_api_key:
            raise ValueError("FRED_API_KEY is required; FRED API keys are free")
        self.tiingo_token = tiingo_token
        self.fred_api_key = fred_api_key
        self.timeout = timeout

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout, follow_redirects=True)

    def fetch_tradfi(self, value: FreeCoreRange, data_root: Path) -> dict[str, object]:
        raw_root = data_root / "raw" / "free_core" / "tiingo" / _range_dir(value)
        canonical_path = (
            data_root
            / "canonical"
            / "core"
            / "free_proxy_daily"
            / _range_dir(value)
            / "tradfi.parquet"
        )
        raw_root.mkdir(parents=True, exist_ok=True)
        frames: list[pd.DataFrame] = []
        with self._client() as client:
            for economic_asset, ticker in FREE_TRADFI_PROXIES.items():
                response = client.get(
                    f"{self.TIINGO_BASE}/{ticker}/prices",
                    params={"startDate": value.start, "endDate": value.end},
                    headers={"Authorization": f"Token {self.tiingo_token}"},
                )
                response.raise_for_status()
                payload = response.json()
                raw_path = raw_root / f"{ticker}.json"
                raw_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                frame = parse_tiingo_eod_payload(economic_asset, ticker, payload)
                if frame.empty:
                    raise ValueError(f"Tiingo returned no data for {ticker}")
                frames.append(frame)
        combined = pd.concat(frames, ignore_index=True).sort_values(["date", "economic_asset"])
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = canonical_path.with_suffix(".parquet.tmp")
        combined.to_parquet(tmp, index=False)
        tmp.replace(canonical_path)
        return {
            "source": "tiingo_eod",
            "data_cost_usd": 0,
            "symbols": list(FREE_TRADFI_PROXIES.values()),
            "rows": len(combined),
            "canonical": str(canonical_path),
        }

    def _fetch_binance_symbol(
        self,
        client: httpx.Client,
        economic_asset: str,
        symbol: str,
        value: FreeCoreRange,
        raw_dir: Path,
    ) -> pd.DataFrame:
        start_ms = int(pd.Timestamp(value.start, tz="UTC").timestamp() * 1000)
        end_ms = int(pd.Timestamp(value.end, tz="UTC").timestamp() * 1000) - 1
        cursor = start_ms
        pages: list[list[Any]] = []
        page_no = 0
        while cursor <= end_ms:
            response = client.get(
                self.BINANCE_KLINES,
                params={
                    "symbol": symbol,
                    "interval": "1d",
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": 1000,
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError(f"Binance returned invalid payload for {symbol}")
            if not payload:
                break
            page_no += 1
            (raw_dir / f"{symbol}_page={page_no:04d}.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            pages.extend(payload)
            last_open_ms = int(payload[-1][0])
            next_cursor = last_open_ms + 86_400_000
            if next_cursor <= cursor:
                raise RuntimeError(f"Binance pagination did not advance for {symbol}")
            cursor = next_cursor
            if len(payload) < 1000:
                break
        return parse_binance_klines(economic_asset, symbol, pages)

    def fetch_crypto(self, value: FreeCoreRange, data_root: Path) -> dict[str, object]:
        raw_root = data_root / "raw" / "free_core" / "binance" / _range_dir(value)
        canonical_path = (
            data_root
            / "canonical"
            / "core"
            / "free_proxy_daily"
            / _range_dir(value)
            / "crypto.parquet"
        )
        raw_root.mkdir(parents=True, exist_ok=True)
        frames: list[pd.DataFrame] = []
        with self._client() as client:
            for economic_asset, symbol in FREE_CRYPTO_PROXIES.items():
                frame = self._fetch_binance_symbol(
                    client,
                    economic_asset,
                    symbol,
                    value,
                    raw_root,
                )
                if frame.empty:
                    raise ValueError(f"Binance returned no data for {symbol}")
                frames.append(frame)
        combined = pd.concat(frames, ignore_index=True).sort_values(["date", "economic_asset"])
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = canonical_path.with_suffix(".parquet.tmp")
        combined.to_parquet(tmp, index=False)
        tmp.replace(canonical_path)
        return {
            "source": "binance_spot_public",
            "data_cost_usd": 0,
            "symbols": list(FREE_CRYPTO_PROXIES.values()),
            "rows": len(combined),
            "canonical": str(canonical_path),
        }

    def fetch_cash(self, value: FreeCoreRange, data_root: Path) -> dict[str, object]:
        raw_root = data_root / "raw" / "free_core" / "fred" / _range_dir(value)
        canonical_path = (
            data_root
            / "canonical"
            / "core"
            / "cash_rate"
            / _range_dir(value)
            / f"{FRED_CASH_SERIES}.parquet"
        )
        raw_root.mkdir(parents=True, exist_ok=True)
        with self._client() as client:
            response = client.get(
                self.FRED_OBSERVATIONS,
                params={
                    "series_id": FRED_CASH_SERIES,
                    "api_key": self.fred_api_key,
                    "file_type": "json",
                    "observation_start": value.start,
                    "observation_end": value.end,
                },
            )
            response.raise_for_status()
            payload = response.json()
        (raw_root / f"{FRED_CASH_SERIES}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        frame = parse_fred_observations(FRED_CASH_SERIES, payload)
        if frame.empty:
            raise ValueError(f"FRED returned no data for {FRED_CASH_SERIES}")
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = canonical_path.with_suffix(".parquet.tmp")
        frame.to_parquet(tmp, index=False)
        tmp.replace(canonical_path)
        return {
            "source": "fred",
            "data_cost_usd": 0,
            "series_id": FRED_CASH_SERIES,
            "rows": len(frame),
            "canonical": str(canonical_path),
        }

    def fetch_all(self, value: FreeCoreRange, data_root: Path) -> dict[str, object]:
        tradfi = self.fetch_tradfi(value, data_root)
        crypto = self.fetch_crypto(value, data_root)
        cash = self.fetch_cash(value, data_root)
        return {
            "mode": "free_only",
            "data_cost_usd": 0,
            "start": value.start,
            "end": value.end,
            "tradfi": tradfi,
            "crypto": crypto,
            "cash": cash,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
