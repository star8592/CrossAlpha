from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from crossalpha.state.v03_rpc import (
    AAVE_V3_ETHEREUM_CORE_POOL,
    BORROW_EVENT_TOPIC0,
    AaveBorrowerRpc,
    RpcPolicy,
    resolve_rpc_candidates,
)


BLOCKSCOUT_ETHEREUM_API_URL = "https://eth.blockscout.com/api"
BLOCKSCOUT_ETHEREUM_RPC_URL = "https://eth.blockscout.com/api/eth-rpc"
BLOCKSCOUT_LOG_SOURCE = "BLOCKSCOUT_INDEXED_LOGS"
BLOCKSCOUT_STATE_RPC_SOURCE = "BLOCKSCOUT_ETH_RPC_ZERO_COST_FALLBACK"
BLOCKSCOUT_MAX_LOG_RESULTS = 1000
RPC_LOG_SOURCE_SUFFIX = "_ETH_GETLOGS"


class BorrowLogResultLimit(RuntimeError):
    """Raised when an indexed-log response may have hit the provider hard limit."""


@dataclass(frozen=True)
class BorrowLogPolicy:
    timeout_seconds: float = 30.0
    max_results: int = BLOCKSCOUT_MAX_LOG_RESULTS
    transient_retries: int = 6
    retry_base_seconds: float = 1.0
    retry_max_seconds: float = 20.0


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


def resolve_borrow_log_rpc_candidates(configured: str | None) -> list[tuple[str, str]]:
    """Prefer operator/non-Blockscout JSON-RPC for logs; keep Blockscout as last resort."""
    base = resolve_rpc_candidates(configured)
    preferred: list[tuple[str, str]] = []
    blockscout: list[tuple[str, str]] = []
    for url, source in base:
        item = (url, f"{source}{RPC_LOG_SOURCE_SUFFIX}")
        if url == BLOCKSCOUT_ETHEREUM_RPC_URL:
            blockscout.append(item)
        else:
            preferred.append(item)
    return preferred + blockscout


def parse_blockscout_logs(
    body: Any, *, max_results: int = BLOCKSCOUT_MAX_LOG_RESULTS
) -> list[dict[str, Any]]:
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

    @staticmethod
    def _retryable_status(status_code: int) -> bool:
        return status_code == 429 or 500 <= status_code <= 599

    def _retry_delay(self, attempt: int, response: httpx.Response | None = None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    value = float(retry_after)
                except ValueError:
                    value = 0.0
                if value > 0:
                    return min(value, self.policy.retry_max_seconds)
        return min(
            self.policy.retry_base_seconds * (2**attempt),
            self.policy.retry_max_seconds,
        )

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
            last_error: Exception | None = None
            for attempt in range(self.policy.transient_retries + 1):
                response: httpx.Response | None = None
                try:
                    response = await client.get(self.api_url, params=params)
                    response.raise_for_status()
                    body = response.json()
                    return parse_blockscout_logs(body, max_results=self.policy.max_results)
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    last_error = exc
                except httpx.HTTPStatusError as exc:
                    if not self._retryable_status(exc.response.status_code):
                        raise
                    last_error = exc
                    response = exc.response
                if attempt >= self.policy.transient_retries:
                    assert last_error is not None
                    raise last_error
                await asyncio.sleep(self._retry_delay(attempt, response))
        raise RuntimeError("Blockscout indexed-log query exhausted retries")

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


class FailoverBorrowLogProvider:
    """Sticky zero-cost eth_getLogs provider with indexed Blockscout as final fallback."""

    def __init__(
        self,
        configured_rpc: str | None,
        *,
        policy: BorrowLogPolicy | None = None,
    ) -> None:
        self.policy = policy or BorrowLogPolicy()
        self._rpc_candidates = resolve_borrow_log_rpc_candidates(configured_rpc)
        self._selected_index: int | None = None
        self.selected_source: str | None = None
        self.candidate_failures: dict[str, str] = {}
        self._blockscout = BlockscoutBorrowLogProvider(policy=self.policy)

    async def _rpc_logs(
        self, url: str, from_block: int, to_block: int
    ) -> list[dict[str, Any]]:
        rpc = AaveBorrowerRpc(
            url,
            policy=RpcPolicy(
                batch_size=100,
                timeout_seconds=self.policy.timeout_seconds,
            ),
        )
        return await rpc.borrow_logs(from_block, to_block)

    async def borrow_logs(self, from_block: int, to_block: int) -> list[dict[str, Any]]:
        if int(from_block) < 0 or int(to_block) < int(from_block):
            raise ValueError("invalid block range")

        order: list[int] = []
        if self._selected_index is not None:
            order.append(self._selected_index)
        order.extend(index for index in range(len(self._rpc_candidates)) if index not in order)

        for index in order:
            url, source = self._rpc_candidates[index]
            try:
                rows = await self._rpc_logs(url, int(from_block), int(to_block))
                self._selected_index = index
                self.selected_source = source
                return rows
            except Exception as exc:
                # Never persist endpoint URLs or exception messages; configured RPC may contain secrets.
                self.candidate_failures[source] = type(exc).__name__
                if self._selected_index == index:
                    self._selected_index = None

        try:
            rows = await self._blockscout.borrow_logs(int(from_block), int(to_block))
            self.selected_source = BLOCKSCOUT_LOG_SOURCE
            return rows
        except Exception as exc:
            self.candidate_failures[BLOCKSCOUT_LOG_SOURCE] = type(exc).__name__
            raise RuntimeError(
                "No V0.3 Borrow-log source completed the requested range"
            ) from exc
