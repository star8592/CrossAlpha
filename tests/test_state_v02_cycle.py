from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from crossalpha.domain.models import ObservationEnvelope, SourceType
from crossalpha.settings import Settings
from crossalpha.state import v02_cycle


async def _market_collect(self):
    now = datetime.now(timezone.utc)
    return [
        ObservationEnvelope(
            observed_at=now,
            known_at=now,
            source_type=SourceType.AGGREGATOR,
            source_id="aave:v3:graphql",
            observation_type="markets_snapshot",
            payload={"data": {"markets": [{"address": "0xcore", "reserves": []}]}},
        )
    ]


async def _rpc_fail(self):
    raise RuntimeError("optional rpc unavailable")


def _settings(tmp_path, *, rpc: bool) -> Settings:
    return Settings(
        crossalpha_data_dir=tmp_path,
        crossalpha_http_timeout=1.0,
        evm_rpc_url="http://rpc.invalid" if rpc else None,
    )


def _patch_downstream(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        v02_cycle,
        "canonicalize_aave",
        lambda _root, recent_days=2: {
            "mode": "recent_2d",
            "market_snapshots": 1,
            "market_written": 1,
            "liquidation_snapshots": 0,
        },
    )
    monkeypatch.setattr(
        v02_cycle,
        "build_latest_state_v02",
        lambda _root, write=True: {
            "protocol": "CROSSALPHA_STATE_V0_2",
            "status": "written",
            "data_confidence": "PARTIAL",
        },
    )
    monkeypatch.setattr(v02_cycle, "build_catalog", lambda _root: {"database": "x", "views": []})
    monkeypatch.setattr(v02_cycle, "_freeze_path", lambda _root: tmp_path / "missing-freeze.json")


def test_optional_liquidation_rpc_failure_does_not_fail_v02_market_cycle(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(v02_cycle.AaveV3MarketProvider, "collect", _market_collect)
    monkeypatch.setattr(v02_cycle.AaveV3LiquidationRpcProvider, "collect", _rpc_fail)
    _patch_downstream(monkeypatch, tmp_path)
    report = asyncio.run(v02_cycle.run_state_v02_cycle(_settings(tmp_path, rpc=True)))
    assert report["aave_market"]["ok"] is True
    assert report["aave_liquidations_rpc"]["configured"] is True
    assert report["aave_liquidations_rpc"]["ok"] is False
    assert "optional rpc unavailable" in report["aave_liquidations_rpc"]["error"]
    assert report["v01_collector_or_paper_mutated"] is False


def test_required_aave_market_failure_fails_only_v02_cycle(monkeypatch, tmp_path) -> None:
    async def fail_market(self):
        raise RuntimeError("required Aave API unavailable")

    monkeypatch.setattr(v02_cycle.AaveV3MarketProvider, "collect", fail_market)
    with pytest.raises(RuntimeError, match="required Aave API unavailable"):
        asyncio.run(v02_cycle.run_state_v02_cycle(_settings(tmp_path, rpc=False)))
