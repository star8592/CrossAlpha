from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from typing import Any

import httpx
import pandas as pd


ASSETS = ("BTC", "ETH")
VENUES = ("binance", "okx", "bybit")


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _mid(bid: Any, ask: Any) -> float | None:
    b = _number(bid)
    a = _number(ask)
    if b is None or a is None or b <= 0 or a <= 0 or a < b:
        return None
    return (b + a) / 2.0


def _spread_bps(bid: Any, ask: Any) -> float | None:
    b = _number(bid)
    a = _number(ask)
    midpoint = _mid(bid, ask)
    if midpoint is None or b is None or a is None:
        return None
    return (a - b) / midpoint * 10_000.0


def _basis_bps(perp_mid: float | None, spot_mid: float | None) -> float | None:
    if perp_mid is None or spot_mid is None or spot_mid <= 0:
        return None
    return (perp_mid / spot_mid - 1.0) * 10_000.0


def _funding_8h(rate: Any, interval_hours: Any) -> float | None:
    r = _number(rate)
    hours = _number(interval_hours)
    if r is None or hours is None or hours <= 0:
        return None
    return r * 8.0 / hours


def _ms_to_iso(value: Any) -> str | None:
    number = _number(value)
    if number is None or number <= 0:
        return None
    return datetime.fromtimestamp(number / 1000.0, tz=timezone.utc).isoformat()


def _latest_source_time(values: list[Any]) -> str | None:
    parsed: list[pd.Timestamp] = []
    for value in values:
        if value in (None, "", 0, "0"):
            continue
        try:
            if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
                ts = pd.Timestamp(float(value), unit="ms", tz="UTC")
            else:
                ts = pd.Timestamp(value)
                ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
            parsed.append(ts)
        except Exception:
            continue
    return max(parsed).isoformat() if parsed else None


class MultiVenueCollector:
    """Read-only BTC/ETH spot/perpetual mechanics from public venue APIs."""

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    async def _get(self, client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> Any:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    async def _binance(self, client: httpx.AsyncClient, asset: str) -> dict[str, Any]:
        symbol = f"{asset}USDT"
        spot_url = "https://api.binance.com/api/v3/ticker/bookTicker"
        futures = "https://fapi.binance.com"
        spot, depth, premium, oi, funding = await asyncio.gather(
            self._get(client, spot_url, {"symbol": symbol}),
            self._get(client, futures + "/fapi/v1/depth", {"symbol": symbol, "limit": 5}),
            self._get(client, futures + "/fapi/v1/premiumIndex", {"symbol": symbol}),
            self._get(client, futures + "/fapi/v1/openInterest", {"symbol": symbol}),
            self._get(client, futures + "/fapi/v1/fundingRate", {"symbol": symbol, "limit": 2}),
        )
        return {
            "venue": "binance",
            "asset": asset,
            "spot_symbol": symbol,
            "perp_symbol": symbol,
            "spot": spot,
            "perp_depth": depth,
            "premium": premium,
            "open_interest": oi,
            "funding_history": funding,
        }

    async def _okx(self, client: httpx.AsyncClient, asset: str) -> dict[str, Any]:
        base = "https://www.okx.com"
        spot_id = f"{asset}-USDT"
        swap_id = f"{asset}-USDT-SWAP"
        spot, swap, funding, oi = await asyncio.gather(
            self._get(client, base + "/api/v5/market/ticker", {"instId": spot_id}),
            self._get(client, base + "/api/v5/market/ticker", {"instId": swap_id}),
            self._get(client, base + "/api/v5/public/funding-rate", {"instId": swap_id}),
            self._get(
                client,
                base + "/api/v5/public/open-interest",
                {"instType": "SWAP", "instId": swap_id},
            ),
        )
        return {
            "venue": "okx",
            "asset": asset,
            "spot_symbol": spot_id,
            "perp_symbol": swap_id,
            "spot": spot,
            "perp": swap,
            "funding": funding,
            "open_interest": oi,
        }

    async def _bybit(self, client: httpx.AsyncClient, asset: str) -> dict[str, Any]:
        base = "https://api.bybit.com/v5/market/tickers"
        symbol = f"{asset}USDT"
        spot, perp = await asyncio.gather(
            self._get(client, base, {"category": "spot", "symbol": symbol}),
            self._get(client, base, {"category": "linear", "symbol": symbol}),
        )
        return {
            "venue": "bybit",
            "asset": asset,
            "spot_symbol": symbol,
            "perp_symbol": symbol,
            "spot": spot,
            "perp": perp,
        }

    async def collect(self) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            tasks = []
            for asset in ASSETS:
                tasks.extend(
                    [
                        self._binance(client, asset),
                        self._okx(client, asset),
                        self._bybit(client, asset),
                    ]
                )
            return list(await asyncio.gather(*tasks))


def _okx_item(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        return {}
    data = payload.get("data")
    return data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}


def _bybit_item(payload: Any) -> tuple[dict[str, Any], Any]:
    if not isinstance(payload, dict) or int(payload.get("retCode", -1)) != 0:
        return {}, None
    result = payload.get("result")
    data = result.get("list") if isinstance(result, dict) else None
    item = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
    return item, payload.get("time")


def parse_venue_snapshot(payload: dict[str, Any], *, known_at: Any | None = None) -> dict[str, Any]:
    venue = str(payload.get("venue", "")).lower()
    asset = str(payload.get("asset", "")).upper()
    if venue not in VENUES or asset not in ASSETS:
        raise ValueError("unsupported State V0.4 venue/asset")
    current = pd.Timestamp(known_at or datetime.now(timezone.utc))
    current = current.tz_localize("UTC") if current.tzinfo is None else current.tz_convert("UTC")

    spot_bid = spot_ask = perp_bid = perp_ask = None
    mark = index = funding_rate = funding_interval = oi_usd = None
    source_time = None

    if venue == "binance":
        spot = payload.get("spot") if isinstance(payload.get("spot"), dict) else {}
        depth = payload.get("perp_depth") if isinstance(payload.get("perp_depth"), dict) else {}
        premium = payload.get("premium") if isinstance(payload.get("premium"), dict) else {}
        oi = payload.get("open_interest") if isinstance(payload.get("open_interest"), dict) else {}
        history = payload.get("funding_history") if isinstance(payload.get("funding_history"), list) else []
        spot_bid, spot_ask = spot.get("bidPrice"), spot.get("askPrice")
        bids = depth.get("bids") if isinstance(depth.get("bids"), list) else []
        asks = depth.get("asks") if isinstance(depth.get("asks"), list) else []
        perp_bid = bids[0][0] if bids and isinstance(bids[0], list) and bids[0] else None
        perp_ask = asks[0][0] if asks and isinstance(asks[0], list) and asks[0] else None
        mark, index = premium.get("markPrice"), premium.get("indexPrice")
        history_rows = [row for row in history if isinstance(row, dict)]
        history_rows.sort(key=lambda row: int(row.get("fundingTime", 0)))
        if history_rows:
            funding_rate = history_rows[-1].get("fundingRate")
        if len(history_rows) >= 2:
            delta_ms = int(history_rows[-1].get("fundingTime", 0)) - int(
                history_rows[-2].get("fundingTime", 0)
            )
            if delta_ms > 0:
                funding_interval = delta_ms / 3_600_000.0
        perp_mid = _mid(perp_bid, perp_ask)
        oi_base = _number(oi.get("openInterest"))
        oi_usd = oi_base * perp_mid if oi_base is not None and perp_mid is not None else None
        source_time = _latest_source_time(
            [premium.get("time"), oi.get("time"), depth.get("E"), depth.get("T")]
            + [row.get("fundingTime") for row in history_rows]
        )

    elif venue == "okx":
        spot = _okx_item(payload.get("spot"))
        perp = _okx_item(payload.get("perp"))
        funding = _okx_item(payload.get("funding"))
        oi = _okx_item(payload.get("open_interest"))
        spot_bid, spot_ask = spot.get("bidPx"), spot.get("askPx")
        perp_bid, perp_ask = perp.get("bidPx"), perp.get("askPx")
        mark = None
        index = None
        funding_rate = funding.get("fundingRate")
        funding_time = _number(funding.get("fundingTime"))
        next_funding = _number(funding.get("nextFundingTime"))
        if funding_time is not None and next_funding is not None and next_funding > funding_time:
            funding_interval = (next_funding - funding_time) / 3_600_000.0
        oi_usd = _number(oi.get("oiUsd"))
        source_time = _latest_source_time(
            [spot.get("ts"), perp.get("ts"), funding.get("ts"), oi.get("ts")]
        )

    else:
        spot, spot_time = _bybit_item(payload.get("spot"))
        perp, perp_time = _bybit_item(payload.get("perp"))
        spot_bid, spot_ask = spot.get("bid1Price"), spot.get("ask1Price")
        perp_bid, perp_ask = perp.get("bid1Price"), perp.get("ask1Price")
        mark, index = perp.get("markPrice"), perp.get("indexPrice")
        funding_rate = perp.get("fundingRate")
        funding_interval = perp.get("fundingIntervalHour")
        oi_usd = _number(perp.get("openInterestValue"))
        source_time = _latest_source_time([spot_time, perp_time])

    spot_mid = _mid(spot_bid, spot_ask)
    perp_mid = _mid(perp_bid, perp_ask)
    mark_number = _number(mark)
    index_number = _number(index)
    return {
        "protocol": "CROSSALPHA_STATE_V0_4",
        "observed_at": source_time or current.isoformat(),
        "known_at": current.isoformat(),
        "venue": venue,
        "asset": asset,
        "spot_symbol": payload.get("spot_symbol"),
        "perp_symbol": payload.get("perp_symbol"),
        "spot_bid": _number(spot_bid),
        "spot_ask": _number(spot_ask),
        "spot_mid": spot_mid,
        "spot_spread_bps": _spread_bps(spot_bid, spot_ask),
        "perp_bid": _number(perp_bid),
        "perp_ask": _number(perp_ask),
        "perp_mid": perp_mid,
        "perp_spread_bps": _spread_bps(perp_bid, perp_ask),
        "mark_price": mark_number,
        "index_price": index_number,
        "basis_bps": _basis_bps(perp_mid, spot_mid),
        "mark_index_basis_bps": (
            _basis_bps(mark_number, index_number)
            if mark_number is not None and index_number is not None
            else None
        ),
        "funding_rate_raw": _number(funding_rate),
        "funding_interval_hours": _number(funding_interval),
        "funding_rate_8h": _funding_8h(funding_rate, funding_interval),
        "open_interest_usd": oi_usd,
        "data_cost_usd": 0,
    }
