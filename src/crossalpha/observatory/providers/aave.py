from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from crossalpha.domain.models import ObservationEnvelope, SourceType


AAVE_V3_GRAPHQL = "https://api.v3.aave.com/graphql"
AAVE_V3_ETHEREUM_POOL = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"
AAVE_V3_ETHEREUM_CHAIN_ID = 1
LIQUIDATION_CALL_TOPIC = (
    "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286"
)


def _markets_query(chain_id: int) -> str:
    """Build a literal-chain query to avoid depending on a custom GraphQL scalar name."""
    if int(chain_id) <= 0:
        raise ValueError("chain_id must be positive")
    return f"""
query CrossAlphaAaveMarkets {{
  markets(request: {{ chainIds: [{int(chain_id)}] }}) {{
    address
    name
    reserves {{
      underlyingToken {{ address symbol decimals }}
      supplyInfo {{ apy {{ formatted }} }}
      borrowInfo {{
        apy {{ formatted }}
        availableLiquidity {{ amount {{ value }} usd }}
        borrowCapReached
      }}
      isFrozen
      isPaused
    }}
  }}
}}
"""


class AaveV3MarketProvider:
    """Free, read-only Aave V3 GraphQL market snapshot collector.

    This provider intentionally does not claim to observe the distribution of
    borrower health factors. Market liquidity/rates and user liquidation cliffs
    are different objects and remain separate in State V0.2.
    """

    def __init__(
        self,
        timeout: float = 30.0,
        *,
        endpoint: str = AAVE_V3_GRAPHQL,
        chain_id: int = AAVE_V3_ETHEREUM_CHAIN_ID,
    ) -> None:
        self.timeout = timeout
        self.endpoint = endpoint
        self.chain_id = int(chain_id)

    async def collect(self) -> list[ObservationEnvelope]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self.endpoint,
                json={"query": _markets_query(self.chain_id)},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            response.raise_for_status()
            body = response.json()

        errors = body.get("errors") if isinstance(body, dict) else None
        if errors:
            raise RuntimeError(f"Aave V3 GraphQL returned errors: {errors}")
        markets = body.get("data", {}).get("markets") if isinstance(body, dict) else None
        if not isinstance(markets, list) or not markets:
            raise ValueError("Aave V3 GraphQL returned no markets")

        now = datetime.now(timezone.utc)
        return [
            ObservationEnvelope(
                observed_at=now,
                known_at=now,
                source_type=SourceType.AGGREGATOR,
                source_id="aave:v3:graphql",
                observation_type="markets_snapshot",
                payload=body,
                metadata={
                    "endpoint": self.endpoint,
                    "chain_id": self.chain_id,
                    "data_cost_usd": 0,
                    "scope": "market_level_not_user_health_factor_distribution",
                },
            )
        ]


class AaveV3LiquidationRpcProvider:
    """Optional point-in-time LiquidationCall confirmation using a user RPC URL.

    The provider scans only a bounded recent block window on every run. Canonical
    materialization deduplicates by transaction hash + log index, so no mutable
    cursor is required and overlapping scans are reorg-friendlier than a cursor.
    Block timestamps are attached to each returned log so event_time remains
    distinct from collector observed_at/known_at.
    """

    def __init__(
        self,
        rpc_url: str,
        timeout: float = 30.0,
        *,
        pool_address: str = AAVE_V3_ETHEREUM_POOL,
        lookback_blocks: int = 512,
    ) -> None:
        self.rpc_url = rpc_url
        self.timeout = timeout
        self.pool_address = pool_address
        self.lookback_blocks = max(int(lookback_blocks), 1)

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self.rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            body = response.json()
        if "error" in body:
            raise RuntimeError(body["error"])
        return body["result"]

    async def collect(self) -> list[ObservationEnvelope]:
        latest_raw = await self._rpc("eth_blockNumber", [])
        latest = int(str(latest_raw), 16)
        first = max(0, latest - self.lookback_blocks + 1)
        logs = await self._rpc(
            "eth_getLogs",
            [
                {
                    "address": self.pool_address,
                    "fromBlock": hex(first),
                    "toBlock": hex(latest),
                    "topics": [LIQUIDATION_CALL_TOPIC],
                }
            ],
        )
        if not isinstance(logs, list):
            raise ValueError("Aave liquidation eth_getLogs did not return a list")

        block_cache: dict[str, str | None] = {}
        enriched: list[dict[str, Any]] = []
        for item in logs:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            block_number = row.get("blockNumber")
            if isinstance(block_number, str):
                if block_number not in block_cache:
                    block = await self._rpc("eth_getBlockByNumber", [block_number, False])
                    timestamp = block.get("timestamp") if isinstance(block, dict) else None
                    block_cache[block_number] = timestamp if isinstance(timestamp, str) else None
                raw_timestamp = block_cache[block_number]
                if raw_timestamp is not None:
                    row["blockTimestamp"] = datetime.fromtimestamp(
                        int(raw_timestamp, 16), tz=timezone.utc
                    ).isoformat()
            enriched.append(row)

        now = datetime.now(timezone.utc)
        return [
            ObservationEnvelope(
                observed_at=now,
                known_at=now,
                source_type=SourceType.CHAIN,
                source_id="aave:v3:ethereum",
                observation_type="liquidation_logs",
                payload=enriched,
                metadata={
                    "pool_address": self.pool_address,
                    "chain_id": AAVE_V3_ETHEREUM_CHAIN_ID,
                    "from_block": first,
                    "to_block": latest,
                    "lookback_blocks": self.lookback_blocks,
                    "data_cost_usd": 0,
                },
            )
        ]
