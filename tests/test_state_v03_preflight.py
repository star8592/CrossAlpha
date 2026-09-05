from __future__ import annotations

import asyncio

import pandas as pd

from crossalpha.settings import Settings
from crossalpha.state import v03_preflight
from crossalpha.state.v03_logs import BLOCKSCOUT_LOG_SOURCE
from crossalpha.state.v03_rpc import AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK


BLOCK_TIME = "2026-09-05T00:00:00+00:00"


class _ProbeRpc:
    def __init__(self, rpc_url: str, *_args, **_kwargs):
        self.rpc_url = rpc_url

    async def latest_block(self) -> int:
        if self.rpc_url == "http://configured-secret.invalid/token-should-not-leak":
            raise RuntimeError("configured endpoint is pruned: token-should-not-leak")
        return AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK + 10_000

    async def block_timestamp(self, _block_number: int) -> str:
        return BLOCK_TIME

    async def account_data(self, addresses: list[str], *, block_number: int) -> pd.DataFrame:
        assert block_number >= AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK
        return pd.DataFrame(
            [
                {
                    "address": address,
                    "success": True,
                    "error": None,
                    "total_collateral_usd": 0.0,
                    "total_debt_usd": 0.0,
                    "available_borrows_usd": 0.0,
                    "current_liquidation_threshold_pct": 0.0,
                    "ltv_pct": 0.0,
                    "health_factor": None,
                }
                for address in addresses
            ]
        )


class _ProbeLogs:
    def __init__(self, *_args, **_kwargs):
        pass

    async def borrow_logs(self, from_block: int, to_block: int):
        assert from_block <= to_block
        return []


def test_preflight_splits_indexed_history_from_state_rpc_and_redacts_failures(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(v03_preflight, "AaveBorrowerRpc", _ProbeRpc)
    monkeypatch.setattr(v03_preflight, "BlockscoutBorrowLogProvider", _ProbeLogs)
    settings = Settings(
        crossalpha_data_dir=tmp_path,
        evm_rpc_url="http://configured-secret.invalid/token-should-not-leak",
    )
    report = asyncio.run(v03_preflight.run_v03_preflight(settings))
    assert report["split_data_plane"] is True
    assert report["archive_rpc_required"] is False
    assert report["borrow_log_source"] == BLOCKSCOUT_LOG_SOURCE
    assert report["state_rpc_source"] == "BLOCKREQ_ARCHIVE_ZERO_COST_FALLBACK"
    assert report["state_rpc_candidate_failures_before_selection"] == {
        "EVM_RPC_URL": "RuntimeError"
    }
    assert report["historical_log_scan_ok"] is True
    assert report["fixed_block_account_call_ok"] is True
    assert report["risk_multiplier"] is None
    assert report["data_cost_usd"] == 0
    rendered = str(report)
    assert "token-should-not-leak" not in rendered
    assert "configured-secret.invalid" not in rendered
