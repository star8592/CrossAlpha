from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from crossalpha.settings import Settings
from crossalpha.state.v03_cycle import FINALITY_LAG_BLOCKS
from crossalpha.state.v03_logs import (
    BLOCKSCOUT_LOG_SOURCE,
    BlockscoutBorrowLogProvider,
    BorrowLogPolicy,
    resolve_state_rpc_candidates,
)
from crossalpha.state.v03_rpc import (
    AAVE_V3_ETHEREUM_CORE_POOL,
    AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK,
    AaveBorrowerRpc,
    RpcPolicy,
)


async def run_v03_preflight(settings: Settings) -> dict[str, Any]:
    """Probe the split V0.3 data plane without mutating any research ledger."""
    log_provider = BlockscoutBorrowLogProvider(
        policy=BorrowLogPolicy(timeout_seconds=settings.crossalpha_http_timeout)
    )
    historical_from = AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK
    historical_to = historical_from + 255
    try:
        historical_logs = await log_provider.borrow_logs(historical_from, historical_to)
    except Exception as exc:
        raise RuntimeError(
            "State V0.3 indexed Borrow-log source failed historical probe: "
            f"{type(exc).__name__}"
        ) from exc

    attempts: dict[str, str] = {}
    for rpc_url, rpc_source in resolve_state_rpc_candidates(settings.evm_rpc_url):
        rpc = AaveBorrowerRpc(
            rpc_url,
            policy=RpcPolicy(batch_size=100, timeout_seconds=settings.crossalpha_http_timeout),
        )
        try:
            latest = await rpc.latest_block()
            finalized = max(latest - FINALITY_LAG_BLOCKS, AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK)
            finalized_block_time = await rpc.block_timestamp(finalized)
            block_time = pd.Timestamp(finalized_block_time)
            block_time = (
                block_time.tz_localize("UTC")
                if block_time.tzinfo is None
                else block_time.tz_convert("UTC")
            )
            now = pd.Timestamp(datetime.now(timezone.utc))
            if block_time > now:
                raise RuntimeError("finalized block timestamp is in the future")

            probe = await rpc.account_data([AAVE_V3_ETHEREUM_CORE_POOL], block_number=finalized)
            if probe.empty or not bool(probe.iloc[0]["success"]):
                raise RuntimeError("getUserAccountData fixed-block probe failed")

            recent_from = max(finalized - 127, AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK)
            recent_logs = await log_provider.borrow_logs(recent_from, finalized)
            return {
                "protocol": "CROSSALPHA_STATE_V0_3_PREFLIGHT",
                "data_cost_usd": 0,
                "split_data_plane": True,
                "archive_rpc_required": False,
                "borrow_log_source": BLOCKSCOUT_LOG_SOURCE,
                "state_rpc_source": rpc_source,
                "rpc_source": rpc_source,
                "state_rpc_candidate_failures_before_selection": attempts,
                "rpc_candidate_failures_before_selection": attempts,
                "latest_block": latest,
                "finalized_block": finalized,
                "finalized_block_time": block_time.isoformat(),
                "block_time_source": "eth_getBlockByNumber(finalized_block)",
                "finality_lag_blocks": FINALITY_LAG_BLOCKS,
                "recent_borrow_scan_from_block": recent_from,
                "recent_borrow_scan_to_block": finalized,
                "recent_borrow_log_count": len(recent_logs),
                "historical_log_scan_from_block": historical_from,
                "historical_log_scan_to_block": historical_to,
                "historical_log_scan_ok": True,
                "historical_borrow_log_count": len(historical_logs),
                "fixed_block_account_call_ok": True,
                "actionability": "DESCRIPTIVE_ONLY",
                "risk_multiplier": None,
            }
        except Exception as exc:
            # Do not serialize the configured URL or exception message: either may contain a token.
            attempts[rpc_source] = type(exc).__name__

    raise RuntimeError(
        "No State V0.3 state RPC passed finalized-block/fixed-call probes; "
        f"attempts={attempts}"
    )
