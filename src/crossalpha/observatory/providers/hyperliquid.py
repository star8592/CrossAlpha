from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from crossalpha.domain.models import ObservationEnvelope, SourceType
from crossalpha.observatory.providers.base import SnapshotProvider


class HyperliquidProvider(SnapshotProvider):
    URL = "https://api.hyperliquid.xyz/info"

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    async def _post(self, payload: dict) -> object:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(self.URL, json=payload)
                    response.raise_for_status()
                    return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
        assert last_error is not None
        raise last_error

    async def collect(self) -> list[ObservationEnvelope]:
        now = datetime.now(timezone.utc)
        requests = [
            ("metaAndAssetCtxs", {"type": "metaAndAssetCtxs"}),
            ("allMids", {"type": "allMids"}),
        ]
        output: list[ObservationEnvelope] = []
        for observation_type, request in requests:
            payload = await self._post(request)
            output.append(
                ObservationEnvelope(
                    observed_at=now,
                    known_at=now,
                    source_type=SourceType.EXCHANGE,
                    source_id="hyperliquid",
                    observation_type=observation_type,
                    payload=payload,
                    metadata={"request": request, "endpoint": self.URL},
                )
            )
        return output
