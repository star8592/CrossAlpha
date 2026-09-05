from __future__ import annotations

import asyncio

from crossalpha.state.v04_safe_provider import FaultIsolatedMultiVenueCollector


class _FakeCollector(FaultIsolatedMultiVenueCollector):
    async def _binance(self, _client, asset: str):
        raise RuntimeError(f"binance unavailable {asset}")

    async def _okx(self, _client, asset: str):
        return {"venue": "okx", "asset": asset}

    async def _bybit(self, _client, asset: str):
        return {"venue": "bybit", "asset": asset}


def test_fault_isolated_collector_preserves_all_six_slots() -> None:
    rows = asyncio.run(_FakeCollector(timeout=1).collect())
    assert len(rows) == 6
    assert {(row["asset"], row["venue"]) for row in rows} == {
        (asset, venue)
        for asset in ("BTC", "ETH")
        for venue in ("binance", "okx", "bybit")
    }
    failures = [row for row in rows if row.get("collection_error")]
    assert len(failures) == 2
    assert all(row["venue"] == "binance" for row in failures)
