from __future__ import annotations

import asyncio
from pathlib import Path

import pandas as pd

from crossalpha.settings import Settings
from crossalpha.state import v03_cycle
from crossalpha.state.v03_rpc import AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK, BORROW_EVENT_TOPIC0


DEBTOR = "0x2222222222222222222222222222222222222222"
DEBTOR2 = "0x3333333333333333333333333333333333333333"
BLOCK_TIME = "2026-09-05T00:00:00+00:00"


def _topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


class _FakeRpc:
    last_url: str | None = None

    def __init__(self, rpc_url: str, *_args, **_kwargs):
        self.rpc_url = rpc_url
        type(self).last_url = rpc_url

    async def latest_block(self) -> int:
        return AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK + v03_cycle.FINALITY_LAG_BLOCKS + 100

    async def block_timestamp(self, block_number: int) -> str:
        assert block_number >= AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK
        return BLOCK_TIME

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


class _FailingConfiguredRpc(_FakeRpc):
    async def latest_block(self) -> int:
        if self.rpc_url == "http://configured-but-pruned.invalid":
            raise RuntimeError("pruned history unavailable")
        return await super().latest_block()


class _AdvancingRpc(_FakeRpc):
    latest_calls = 0

    def __init__(self, rpc_url: str, *_args, **_kwargs):
        super().__init__(rpc_url)

    async def latest_block(self) -> int:
        type(self).latest_calls += 1
        advance = 100 if type(self).latest_calls == 1 else 200
        return AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK + v03_cycle.FINALITY_LAG_BLOCKS + advance

    async def borrow_logs(self, from_block: int, to_block: int):
        assert from_block <= to_block
        debtor = (
            DEBTOR
            if from_block <= AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK + 100
            else DEBTOR2
        )
        return [
            {
                "removed": False,
                "topics": [
                    BORROW_EVENT_TOPIC0,
                    _topic("0x1111111111111111111111111111111111111111"),
                    _topic(debtor),
                    "0x" + "0" * 64,
                ],
            }
        ]


def test_cycle_without_configured_rpc_uses_zero_cost_fallback(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(v03_cycle, "AaveBorrowerRpc", _FakeRpc)
    settings = Settings(crossalpha_data_dir=tmp_path, evm_rpc_url=None)
    report = asyncio.run(v03_cycle.run_state_v03_cycle(settings))
    assert report["status"] == "FULL_CENSUS_RECORDED"
    assert report["rpc_source"] == "BLOCKREQ_ARCHIVE_ZERO_COST_FALLBACK"
    assert report["data_cost_usd"] == 0
    assert report["risk_multiplier"] is None
    assert report["mutates_v01_or_v02"] is False
    assert report["finalized_block_time"] == BLOCK_TIME
    assert report["census"]["block_time"] == BLOCK_TIME
    assert _FakeRpc.last_url == "https://ethereum-rpc.blockreq.com/v1/rpc/public"


def test_cycle_falls_back_when_configured_rpc_fails_before_any_write(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(v03_cycle, "AaveBorrowerRpc", _FailingConfiguredRpc)
    settings = Settings(
        crossalpha_data_dir=tmp_path,
        evm_rpc_url="http://configured-but-pruned.invalid",
    )
    report = asyncio.run(v03_cycle.run_state_v03_cycle(settings))
    assert report["status"] == "FULL_CENSUS_RECORDED"
    assert report["rpc_source"] == "BLOCKREQ_ARCHIVE_ZERO_COST_FALLBACK"
    assert report["rpc_candidate_failures_before_selection"] == {
        "EVM_RPC_URL": "RuntimeError"
    }
    assert (tmp_path / "derived" / "state" / "v03" / "borrower_universe.parquet").exists()


def test_cycle_bootstrap_can_catch_up_and_record_nonprospective_full_census(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(v03_cycle, "AaveBorrowerRpc", _FakeRpc)
    settings = Settings(crossalpha_data_dir=tmp_path, evm_rpc_url="http://example.invalid")
    report = asyncio.run(v03_cycle.run_state_v03_cycle(settings))
    assert report["status"] == "FULL_CENSUS_RECORDED"
    assert report["rpc_source"] == "EVM_RPC_URL"
    assert report["census"]["valid_full_census"] is True
    assert report["census"]["candidate_address_count"] == 1
    assert report["census"]["block_time"] == BLOCK_TIME
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
    assert second["watchlist"]["block_time"] == BLOCK_TIME
    assert second["watchlist"]["full_market_census_claim_allowed"] is False


def test_new_borrower_between_full_censuses_is_added_to_temporary_watchlist(
    monkeypatch, tmp_path: Path
) -> None:
    _AdvancingRpc.latest_calls = 0
    monkeypatch.setattr(v03_cycle, "AaveBorrowerRpc", _AdvancingRpc)
    settings = Settings(crossalpha_data_dir=tmp_path, evm_rpc_url="http://example.invalid")

    first = asyncio.run(v03_cycle.run_state_v03_cycle(settings))
    assert first["status"] == "FULL_CENSUS_RECORDED"
    assert first["candidate_address_count"] == 1

    second = asyncio.run(v03_cycle.run_state_v03_cycle(settings))
    assert second["status"] == "WATCHLIST_RECORDED"
    assert second["candidate_address_count"] == 2
    assert second["pending_new_borrower_count"] == 1
    assert second["watchlist"]["includes_pending_new_borrowers"] is True
    assert second["watchlist"]["pending_new_borrower_count"] == 1
    assert second["watchlist"]["expected_watchlist_addresses"] == 2

    state = v03_cycle._load_state(tmp_path)
    assert state["pending_new_borrowers_since_full"] == [DEBTOR2]


def test_full_census_needs_time_and_new_finalized_block() -> None:
    now = pd.Timestamp("2026-09-05T12:00:00Z")
    state = {
        "last_valid_full_census_at": "2026-09-05T05:00:00+00:00",
        "last_valid_full_census_block": 123456,
    }
    assert v03_cycle._full_census_due(state, now, 123456) is False
    assert v03_cycle._full_census_due(state, now, 123455) is False
    assert v03_cycle._full_census_due(state, now, 123457) is True

    recent = {
        "last_valid_full_census_at": "2026-09-05T11:00:00+00:00",
        "last_valid_full_census_block": 123456,
    }
    assert v03_cycle._full_census_due(recent, now, 123457) is False