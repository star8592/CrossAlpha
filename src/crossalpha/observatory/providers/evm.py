from __future__ import annotations

from datetime import datetime, timezone

import httpx

from crossalpha.domain.models import ObservationEnvelope, SourceType

ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class EvmRpcProvider:
    """Minimal generic JSON-RPC collector for point-in-time ERC-20 Transfer logs."""

    def __init__(self, rpc_url: str, timeout: float = 30.0):
        self.rpc_url = rpc_url
        self.timeout = timeout

    async def _rpc(self, method: str, params: list) -> object:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            response.raise_for_status()
            body = response.json()
            if "error" in body:
                raise RuntimeError(body["error"])
            return body["result"]

    async def latest_block(self) -> int:
        raw = await self._rpc("eth_blockNumber", [])
        return int(str(raw), 16)

    async def transfer_logs(self, contract: str, from_block: int, to_block: int, chain: str = "ethereum") -> ObservationEnvelope:
        logs = await self._rpc("eth_getLogs", [{"address": contract, "fromBlock": hex(from_block), "toBlock": hex(to_block), "topics": [ERC20_TRANSFER_TOPIC]}])
        now = datetime.now(timezone.utc)
        return ObservationEnvelope(observed_at=now, known_at=now, source_type=SourceType.CHAIN, source_id=f"evm:{chain}", observation_type="erc20_transfer_logs", payload=logs, metadata={"contract": contract, "from_block": from_block, "to_block": to_block})
