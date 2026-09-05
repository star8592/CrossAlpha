from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
import pandas as pd


AAVE_V3_ETHEREUM_CORE_POOL = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"
AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK = 16_291_127
BORROW_EVENT_TOPIC0 = "0xb3d084820fb1a9decffb176436bd02558d15fac9b0ddfed8c465bc7359d7dce0"
GET_USER_ACCOUNT_DATA_SELECTOR = "0xbf92857c"
DEFAULT_PUBLIC_ETHEREUM_RPC = "https://ethereum-rpc.publicnode.com"
BASE_CURRENCY_SCALE = 100_000_000.0
HEALTH_FACTOR_SCALE = 1_000_000_000_000_000_000.0
UINT256_MAX = 2**256 - 1


@dataclass(frozen=True)
class RpcPolicy:
    batch_size: int = 100
    timeout_seconds: float = 30.0


def resolve_rpc_url(configured: str | None) -> tuple[str, str]:
    if configured:
        return configured, "EVM_RPC_URL"
    return DEFAULT_PUBLIC_ETHEREUM_RPC, "PUBLICNODE_ZERO_COST_FALLBACK"


def _normalize_address(value: str) -> str:
    text = str(value).lower()
    if not text.startswith("0x") or len(text) != 42:
        raise ValueError(f"invalid EVM address: {value}")
    int(text[2:], 16)
    return text


def _topic_address(value: Any) -> str | None:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 66:
        return None
    candidate = "0x" + value[-40:].lower()
    try:
        return _normalize_address(candidate)
    except ValueError:
        return None


def borrow_log_debtor(log: dict[str, Any]) -> str | None:
    """Return Borrow.onBehalfOf, the address that actually receives the debt."""
    if bool(log.get("removed", False)):
        return None
    topics = log.get("topics")
    if not isinstance(topics, list) or len(topics) < 3:
        return None
    if str(topics[0]).lower() != BORROW_EVENT_TOPIC0:
        return None
    return _topic_address(topics[2])


def encode_get_user_account_data(address: str) -> str:
    normalized = _normalize_address(address)
    return GET_USER_ACCOUNT_DATA_SELECTOR + normalized[2:].rjust(64, "0")


def decode_get_user_account_data(result: str) -> dict[str, float | None]:
    if not isinstance(result, str) or not result.startswith("0x"):
        raise ValueError("eth_call result is not hex")
    raw = result[2:]
    if len(raw) < 64 * 6 or len(raw) % 64 != 0:
        raise ValueError("getUserAccountData returned unexpected byte length")
    words = [int(raw[i : i + 64], 16) for i in range(0, 64 * 6, 64)]
    (
        total_collateral_base,
        total_debt_base,
        available_borrows_base,
        current_liquidation_threshold,
        ltv,
        health_factor_raw,
    ) = words
    health_factor = None
    if health_factor_raw != UINT256_MAX:
        health_factor = health_factor_raw / HEALTH_FACTOR_SCALE
    return {
        "total_collateral_usd": total_collateral_base / BASE_CURRENCY_SCALE,
        "total_debt_usd": total_debt_base / BASE_CURRENCY_SCALE,
        "available_borrows_usd": available_borrows_base / BASE_CURRENCY_SCALE,
        "current_liquidation_threshold_pct": current_liquidation_threshold / 100.0,
        "ltv_pct": ltv / 100.0,
        "health_factor": health_factor,
    }


class AaveBorrowerRpc:
    """Minimal JSON-RPC reader; no web3 dependency and no paid vendor dependency."""

    def __init__(self, rpc_url: str, *, policy: RpcPolicy | None = None) -> None:
        if not rpc_url:
            raise ValueError("rpc_url is required")
        self.rpc_url = rpc_url
        self.policy = policy or RpcPolicy()

    async def _single(
        self,
        client: httpx.AsyncClient,
        method: str,
        params: list[Any],
        *,
        request_id: int = 1,
    ) -> Any:
        response = await client.post(
            self.rpc_url,
            json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError(f"JSON-RPC {method} returned non-object response")
        if body.get("error") is not None:
            raise RuntimeError(f"JSON-RPC {method} error: {body['error']}")
        return body.get("result")

    async def latest_block(self) -> int:
        async with httpx.AsyncClient(timeout=self.policy.timeout_seconds) as client:
            raw = await self._single(client, "eth_blockNumber", [])
        return int(str(raw), 16)

    async def block_timestamp(self, block_number: int) -> str:
        if int(block_number) < 0:
            raise ValueError("block_number must be non-negative")
        async with httpx.AsyncClient(timeout=self.policy.timeout_seconds) as client:
            block = await self._single(
                client,
                "eth_getBlockByNumber",
                [hex(int(block_number)), False],
            )
        if not isinstance(block, dict) or not isinstance(block.get("timestamp"), str):
            raise ValueError("eth_getBlockByNumber returned no timestamp")
        timestamp = int(block["timestamp"], 16)
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()

    async def borrow_logs(self, from_block: int, to_block: int) -> list[dict[str, Any]]:
        if from_block < 0 or to_block < from_block:
            raise ValueError("invalid block range")
        params = [
            {
                "address": AAVE_V3_ETHEREUM_CORE_POOL,
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "topics": [BORROW_EVENT_TOPIC0],
            }
        ]
        async with httpx.AsyncClient(timeout=self.policy.timeout_seconds) as client:
            result = await self._single(client, "eth_getLogs", params)
        if not isinstance(result, list):
            raise ValueError("eth_getLogs returned non-list result")
        return [row for row in result if isinstance(row, dict)]

    async def _batch_account_calls(
        self,
        client: httpx.AsyncClient,
        addresses: list[str],
        block_number: int,
        *,
        id_offset: int,
    ) -> dict[str, dict[str, Any]]:
        payload = []
        id_to_address: dict[int, str] = {}
        block_tag = hex(block_number)
        for offset, address in enumerate(addresses):
            request_id = id_offset + offset
            normalized = _normalize_address(address)
            id_to_address[request_id] = normalized
            payload.append(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "eth_call",
                    "params": [
                        {
                            "to": AAVE_V3_ETHEREUM_CORE_POOL,
                            "data": encode_get_user_account_data(normalized),
                        },
                        block_tag,
                    ],
                }
            )
        response = await client.post(
            self.rpc_url,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, list):
            raise ValueError("JSON-RPC endpoint does not support batch responses")
        result: dict[str, dict[str, Any]] = {}
        for item in body:
            if not isinstance(item, dict):
                continue
            request_id = item.get("id")
            if not isinstance(request_id, int) or request_id not in id_to_address:
                continue
            address = id_to_address[request_id]
            if item.get("error") is not None:
                result[address] = {
                    "address": address,
                    "success": False,
                    "error": str(item.get("error")),
                }
                continue
            try:
                decoded = decode_get_user_account_data(item.get("result"))
                result[address] = {"address": address, "success": True, "error": None, **decoded}
            except Exception as exc:
                result[address] = {
                    "address": address,
                    "success": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
        return result

    async def _sequential_account_calls(
        self,
        client: httpx.AsyncClient,
        addresses: list[str],
        block_number: int,
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        block_tag = hex(block_number)
        for index, address in enumerate(addresses):
            normalized = _normalize_address(address)
            try:
                raw = await self._single(
                    client,
                    "eth_call",
                    [
                        {
                            "to": AAVE_V3_ETHEREUM_CORE_POOL,
                            "data": encode_get_user_account_data(normalized),
                        },
                        block_tag,
                    ],
                    request_id=index + 1,
                )
                decoded = decode_get_user_account_data(raw)
                result[normalized] = {
                    "address": normalized,
                    "success": True,
                    "error": None,
                    **decoded,
                }
            except Exception as exc:
                result[normalized] = {
                    "address": normalized,
                    "success": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
        return result

    async def account_data(
        self,
        addresses: list[str],
        *,
        block_number: int,
    ) -> pd.DataFrame:
        normalized = sorted({_normalize_address(address) for address in addresses})
        columns = [
            "address",
            "success",
            "error",
            "total_collateral_usd",
            "total_debt_usd",
            "available_borrows_usd",
            "current_liquidation_threshold_pct",
            "ltv_pct",
            "health_factor",
        ]
        if not normalized:
            return pd.DataFrame(columns=columns)

        rows: dict[str, dict[str, Any]] = {}
        batch_size = max(int(self.policy.batch_size), 1)
        async with httpx.AsyncClient(timeout=self.policy.timeout_seconds) as client:
            for chunk_index, start in enumerate(range(0, len(normalized), batch_size)):
                chunk = normalized[start : start + batch_size]
                try:
                    chunk_rows = await self._batch_account_calls(
                        client,
                        chunk,
                        block_number,
                        id_offset=chunk_index * batch_size + 1,
                    )
                    for address in chunk:
                        if address not in chunk_rows:
                            chunk_rows[address] = {
                                "address": address,
                                "success": False,
                                "error": "missing_json_rpc_batch_response",
                            }
                except Exception:
                    chunk_rows = await self._sequential_account_calls(client, chunk, block_number)
                rows.update(chunk_rows)

        return pd.DataFrame([rows[address] for address in normalized]).reindex(columns=columns)
