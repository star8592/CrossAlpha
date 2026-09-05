from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import httpx

from crossalpha.state.v04_provider import ASSETS, MultiVenueCollector


class FaultIsolatedMultiVenueCollector(MultiVenueCollector):
    """Keep all six venue/asset slots even when one public venue request fails."""

    async def _safe(
        self,
        venue: str,
        asset: str,
        call: Callable[[httpx.AsyncClient, str], Awaitable[dict[str, Any]]],
        client: httpx.AsyncClient,
    ) -> dict[str, Any]:
        try:
            return await call(client, asset)
        except Exception as exc:
            symbol = f"{asset}USDT"
            return {
                "venue": venue,
                "asset": asset,
                "spot_symbol": symbol,
                "perp_symbol": symbol,
                "collection_error": f"{type(exc).__name__}: {exc}",
            }

    async def collect(self) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            tasks = []
            for asset in ASSETS:
                tasks.extend(
                    [
                        self._safe("binance", asset, self._binance, client),
                        self._safe("okx", asset, self._okx, client),
                        self._safe("bybit", asset, self._bybit, client),
                    ]
                )
            rows = list(await asyncio.gather(*tasks))
        expected = {(asset, venue) for asset in ASSETS for venue in ("binance", "okx", "bybit")}
        actual = {(str(row.get("asset")), str(row.get("venue"))) for row in rows}
        if len(rows) != 6 or actual != expected:
            raise RuntimeError(f"State V0.4 slot preservation failed: {actual}")
        return rows
