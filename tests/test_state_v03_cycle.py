from __future__ import annotations

import asyncio
from pathlib import Path

import pandas as pd

from crossalpha.settings import Settings
from crossalpha.state import v03_cycle
from crossalpha.state.v03_rpc import AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK, BORROW_EVENT_TOPIC0


DEBTOR = "0x2222222222222222222222222222222222222222"


def _topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


class _FakeRpc:
    def __init__(self, *_args, **_kwargs):
        pass

    async def latest_block(self) -> int:
        return AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK + v03_cycle.FINALITY_LAG_BLOCKS + 100

    async def borrow_logs(self, from_block: int, to_block: int):
        assert from_block <= to_block
        return [
            {
                "removed": False,
                "topics": [
                    BORROW_EVENT_TOPIC0,
                    _topic("0x1111111111111111111111111111111111111111"),
                    _topic(DEBTOR),
                    "0x" + "0" * 64,
                ],
            }
        ]

    async def account_data(self, addresses: list[str], *, block_number: int) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "address": address,
                    "success": True,
                    "error": None,
                    "total_collateral_usd": 2_000_000.0,
                    "total_debt_usd": 1_000_000.0,
                    "available_borrows_usd": 0.0,
                    "current_liquidation_threshold_pct": 82.5,
                    "ltv_pct": 75.0,
                    "health_factor": 1.10,
                }
                for address in addresses
            ]
        )


def test_cycle_without_rpc_is_blocked_without_mutating_prior_versions(tmp_path: Path) -> None:
    settings = Settings(crossalpha_data_dir=tmp_path, evm_rpc_url=None)
    report = asyncio.run(v03_cycle.run_state_v03_cycle(settings))
    assert report["status"] == "BLOCKED_NO_EVM_RPC_URL"
    assert report["risk_multiplier"] is None
    assert report["mutates_v01_or_v02"] is False


def test_cycle_bootstrap_can_catch_up_and_record_nonprospective_full_census(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(v03_cycle, "AaveBorrowerRpc", _FakeRpc)
    settings = Settings(crossalpha_data_dir=tmp_path, evm_rpc_url="http://example.invalid")
    report = asyncio.run(v03_cycle.run_state_v03_cycle(settings))
    assert report["status"] == "FULL_CENSUS_RECORDED"
    assert report["census"]["valid_full_census"] is True
    assert report["census"]["candidate_address_count"] == 1
    assert report["prospective"]["status"] == "not_frozen_no_prospective_write"
    assert report["mutates_v01_or_v02"] is False
    assert (tmp_path / "derived" / "state" / "v03" / "borrower_universe.parquet").exists()
    assert not (tmp_path / "research" / "state_v03" / "prospective").exists()


def test_followup_cycle_uses_watchlist_until_next_full_census(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(v03_cycle, "AaveBorrowerRpc", _FakeRpc)
    settings = Settings(crossalpha_data_dir=tmp_path, evm_rpc_url="http://example.invalid")
    first = asyncio.run(v03_cycle.run_state_v03_cycle(settings))
    assert first["status"] == "FULL_CENSUS_RECORDED"
    second = asyncio.run(v03_cycle.run_state_v03_cycle(settings))
    assert second["status"] == "WATCHLIST_RECORDED"
    assert second["watchlist"]["scope"] == "WATCHLIST_ONLY"
    assert second["watchlist"]["full_market_census_claim_allowed"] is False
