from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from crossalpha.domain.models import RawSnapshotManifest
from crossalpha.observatory.canonical.aave import LIQUIDATION_COLUMNS, parse_aave_liquidations, parse_aave_markets
from crossalpha.observatory.providers import aave


CORE = aave.AAVE_V3_ETHEREUM_POOL
PRIME = "0x1234567890123456789012345678901234567890"


def _market(address: str, name: str, symbol: str = "USDC") -> dict:
    return {
        "address": address,
        "name": name,
        "reserves": [
            {
                "underlyingToken": {
                    "address": "0x0000000000000000000000000000000000000001",
                    "symbol": symbol,
                    "decimals": 6,
                },
                "supplyInfo": {"apy": {"formatted": "3.25"}},
                "borrowInfo": {
                    "apy": {"formatted": "4.50"},
                    "availableLiquidity": {"amount": {"value": "123.5"}, "usd": "123500000"},
                    "borrowCapReached": False,
                },
                "isFrozen": False,
                "isPaused": False,
            }
        ],
    }


class _Response:
    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._body


class _Client:
    body: dict = {}
    last_json: dict | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, _url: str, *, json: dict, headers: dict):
        type(self).last_json = json
        return _Response(type(self).body)


def _manifest(observation_type: str) -> RawSnapshotManifest:
    return RawSnapshotManifest(
        path="/tmp/raw.json.gz",
        sha256="a" * 64,
        bytes=100,
        compressed_bytes=50,
        observed_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
        source_id="aave:test",
        observation_type=observation_type,
    )


def test_aave_graphql_ingestion_keeps_only_preregistered_core_market(monkeypatch) -> None:
    _Client.body = {"data": {"markets": [_market(PRIME, "Prime"), _market(CORE, "Core")]}}
    _Client.last_json = None
    monkeypatch.setattr(aave.httpx, "AsyncClient", _Client)
    envelopes = asyncio.run(aave.AaveV3MarketProvider().collect())
    assert len(envelopes) == 1
    markets = envelopes[0].payload["data"]["markets"]
    assert len(markets) == 1
    assert markets[0]["address"].lower() == CORE.lower()
    query = _Client.last_json["query"]
    assert "chainIds: [1]" in query
    assert "variables" not in _Client.last_json


def test_aave_graphql_ingestion_fails_closed_when_core_market_missing(monkeypatch) -> None:
    _Client.body = {"data": {"markets": [_market(PRIME, "Prime")]}}
    monkeypatch.setattr(aave.httpx, "AsyncClient", _Client)
    with pytest.raises(ValueError, match="preregistered Ethereum Core market"):
        asyncio.run(aave.AaveV3MarketProvider().collect())


def test_parse_aave_market_fields_are_percent_and_usd_values() -> None:
    envelope = {
        "observed_at": "2026-09-05T00:00:00Z",
        "known_at": "2026-09-05T00:00:01Z",
        "metadata": {"chain_id": 1},
        "payload": {"data": {"markets": [_market(CORE, "Core")] }},
    }
    frame = parse_aave_markets(envelope, _manifest("markets_snapshot"))
    row = frame.iloc[0]
    assert row["market_address"] == CORE.lower()
    assert row["symbol"] == "USDC"
    assert row["supply_apy_pct"] == pytest.approx(3.25)
    assert row["borrow_apy_pct"] == pytest.approx(4.50)
    assert row["available_liquidity_usd"] == pytest.approx(123_500_000.0)


def test_zero_liquidation_scan_preserves_fixed_schema() -> None:
    envelope = {
        "observed_at": "2026-09-05T00:00:00Z",
        "known_at": "2026-09-05T00:00:01Z",
        "payload": [],
    }
    frame = parse_aave_liquidations(envelope, _manifest("liquidation_logs"))
    assert frame.empty
    assert tuple(frame.columns) == LIQUIDATION_COLUMNS


def _topic(address: str) -> str:
    return "0x" + "0" * 24 + address.lower().removeprefix("0x")


def _word_int(value: int) -> str:
    return f"{value:064x}"


def _word_address(address: str) -> str:
    return "0" * 24 + address.lower().removeprefix("0x")


def test_liquidation_event_parser_preserves_event_time_and_identity() -> None:
    collateral = "0x1111111111111111111111111111111111111111"
    debt = "0x2222222222222222222222222222222222222222"
    user = "0x3333333333333333333333333333333333333333"
    liquidator = "0x4444444444444444444444444444444444444444"
    envelope = {
        "observed_at": "2026-09-05T00:10:00Z",
        "known_at": "2026-09-05T00:10:00Z",
        "payload": [
            {
                "blockTimestamp": "2026-09-05T00:09:45+00:00",
                "blockNumber": "0x10",
                "transactionHash": "0xabc",
                "transactionIndex": "0x2",
                "logIndex": "0x3",
                "blockHash": "0xdef",
                "removed": False,
                "topics": [aave.LIQUIDATION_CALL_TOPIC, _topic(collateral), _topic(debt), _topic(user)],
                "data": "0x" + _word_int(123) + _word_int(456) + _word_address(liquidator) + _word_int(1),
            }
        ],
    }
    row = parse_aave_liquidations(envelope, _manifest("liquidation_logs")).iloc[0]
    assert row["event_time"] == "2026-09-05T00:09:45+00:00"
    assert row["block_number"] == 16
    assert row["transaction_hash"] == "0xabc"
    assert row["log_index"] == 3
    assert row["collateral_asset"] == collateral
    assert row["debt_asset"] == debt
    assert row["user"] == user
    assert row["debt_to_cover_raw"] == 123
    assert row["liquidated_collateral_amount_raw"] == 456
    assert row["liquidator"] == liquidator
    assert row["receive_atoken"] is True
