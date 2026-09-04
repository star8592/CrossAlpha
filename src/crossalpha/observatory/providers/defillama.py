from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from crossalpha.domain.models import ObservationEnvelope, SourceType
from crossalpha.observatory.providers.base import SnapshotProvider


class DefiLlamaStablecoinProvider(SnapshotProvider):
    URL = "https://stablecoins.llama.fi/stablecoins?includePrices=true"

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    async def collect(self) -> list[ObservationEnvelope]:
        last_error: Exception | None = None
        payload = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(self.URL)
                    response.raise_for_status()
                    payload = response.json()
                break
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
        if payload is None:
            assert last_error is not None
            raise last_error
        now = datetime.now(timezone.utc)
        return [ObservationEnvelope(observed_at=now, known_at=now, source_type=SourceType.AGGREGATOR, source_id="defillama", observation_type="stablecoins_snapshot", payload=payload, metadata={"endpoint": self.URL})]
