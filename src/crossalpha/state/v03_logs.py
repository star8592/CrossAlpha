from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from crossalpha.state.v03_rpc import (
    AAVE_V3_ETHEREUM_CORE_POOL,
    BORROW_EVENT_TOPIC0,
    resolve_rpc_candidates,
)


BLOCKSCOUT_ETHEREUM_API_URL = "https://eth.blockscout.com/api"
BLOCKSCOUT_ETHEREUM_RPC_URL = "https://eth.blockscout.com/api/eth-rpc"
BLOCKSCOUT_LOG_SOURCE = "BLOCKSCOUT_INDEXED_LOGS"
BLOCKSCOUT_STATE_RPC_SOURCE = "BLOCKSCOUT_ETH_RPC_ZERO_COST_FALLBACK"
BLOCKSCOUT_MAX_LOG_RESULTS = 1000


class BorrowLogResultLimit(RuntimeError):
    """Raised when an indexed-log response may have hit the provider hard limit."""


@dataclass(frozen=True)
class BorrowLogPolicy:
    timeout_seconds: float = 30.0
    max_results: int = BLOCKSCOUT_MAX_LOG_RESULTS


def resolve_state_rpc_candidates(configured: str | None) -> list[tuple[str, str]]:
    """Prefer an operator RPC, then Blockscout state RPC, then the legacy free pool."""
    base = resolve_rpc_candidates(configured)
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    if configured:
        result.append((configured, "EVM_RPC_URL"))
        seen.add(configured)
    if BLOCKSCOUT_ETHEREUM_RPC_URL not in seen:
        result.append((BLOCKSCOUT_ETHEREUM_RPC_URL, BLOCKSCOUT_STATE_RPC_SOURCE))
        seen.add(BLOCKSCOUT_ETHEREUM_RPC_URL)
    for url, source in base:
        if url not in seen:
            result.append((url, source))
            seen.add(url)
    return result


def parse_blockscout_logs(body: Any, *, max_results: int = BLOCKSCOUT_MAX_LOG_RESULTS) -> list[dict[str, Any]]:
    """Parse Etherscan-compatible Blockscout logs without accepting possible truncation."""
    if not isinstance(body, dict):
        raise ValueError("Blockscout logs returned non-object response")
    result = body.get("result")
    if isinstance(result, list):
        rows = [row for row in result if isinstance(row, dict)]
        if len(result) >= int(max_results):
            raise BorrowLogResultLimit(
                "Blockscout indexed-log response reached the hard result limit; split the block range"
            )
        return rows
    # Never serialize provider response text into research records; it may contain gateway detail.
    raise RuntimeError("Blockscout indexed-log query failed")


class BlockscoutBorrowLogProvider:
    """Zero-cost indexed Aave Borrow-event reader, independent of archive JSON-RPC."""

    def __init__(
        self,
        api_url: str = BLOCKSCOUT_ETHEREUM_API_URL,
        *,
        policy: BorrowLogPolicy | None = None,
    ) -> None:
        if not api_url:
            raise ValueError("api_url is required")
        self.api_url = api_url.rstrip("?")
        self.policy = policy or BorrowLogPolicy()

    async def _query_once(self, from_block: int, to_block: int) -> list[dict[str, Any]]:
        params = {
            "module": "logs",
            "action": "getLogs",
            "fromBlock": str(int(from_block)),
            "toBlock": str(int(to_block)),
            "address": AAVE_V3_ETHEREUM_CORE_POOL,
            "topic0": BORROW_EVENT_TOPIC0,
        }
        async with httpx.AsyncClient(timeout=self.policy.timeout_seconds) as client:
            response = await client.get(self.api_url, params=params)
            response.raise_for_status()
            body = response.json()
        return parse_blockscout_logs(body, max_results=self.policy.max_results)

    async def borrow_logs(self, from_block: int, to_block: int) -> list[dict[str, Any]]:
        """Return a complete range or fail closed; provider result caps split to one block."""
        if int(from_block) < 0 or int(to_block) < int(from_block):
            raise ValueError("invalid block range")
        try:
            return await self._query_once(int(from_block), int(to_block))
        except BorrowLogResultLimit:
            if int(from_block) == int(to_block):
                raise RuntimeError(
                    "Blockscout single-block Borrow log count reached the provider result limit; "
                    "completeness cannot be proven"
                )
            midpoint = (int(from_block) + int(to_block)) // 2
            left = await self.borrow_logs(int(from_block), midpoint)
            right = await self.borrow_logs(midpoint + 1, int(to_block))
            return left + right
