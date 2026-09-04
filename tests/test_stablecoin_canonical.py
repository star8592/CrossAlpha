from __future__ import annotations

from datetime import datetime, timezone

import pytest

from crossalpha.domain.models import RawSnapshotManifest
from crossalpha.observatory.canonical.stablecoins import parse_stablecoin_snapshot


def _manifest() -> RawSnapshotManifest:
    return RawSnapshotManifest(
        path="/tmp/stablecoins.json.gz",
        sha256="a" * 64,
        bytes=100,
        compressed_bytes=50,
        observed_at=datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc),
        source_id="defillama",
        observation_type="stablecoins_snapshot",
    )


def test_parse_stablecoin_snapshot_preserves_native_peg_units() -> None:
    envelope = {
        "observed_at": "2026-09-04T12:00:00Z",
        "known_at": "2026-09-04T12:00:00Z",
        "payload": {
            "peggedAssets": [
                {
                    "id": "usdc",
                    "name": "USD Coin",
                    "symbol": "USDC",
                    "pegType": "peggedUSD",
                    "pegMechanism": "fiat-backed",
                    "price": 0.9998,
                    "circulating": {"peggedUSD": 1000.0},
                    "circulatingPrevDay": {"peggedUSD": 980.0},
                    "circulatingPrevWeek": {"peggedUSD": 900.0},
                    "circulatingPrevMonth": {"peggedUSD": 800.0},
                    "chainCirculating": {
                        "Ethereum": {"peggedUSD": 600.0},
                        "Solana": {"peggedUSD": 400.0},
                    },
                },
                {
                    "id": "eurt",
                    "name": "Euro Token",
                    "symbol": "EURT",
                    "pegType": "peggedEUR",
                    "price": 1.08,
                    "circulating": {"peggedEUR": 100.0},
                    "circulatingPrevDay": {"peggedEUR": 95.0},
                    "chainCirculating": {"Ethereum": {"peggedEUR": 100.0}},
                },
            ]
        },
    }

    assets, chains = parse_stablecoin_snapshot(envelope, _manifest())

    usdc = assets.loc[assets["symbol"] == "USDC"].iloc[0]
    assert usdc["circulating_native"] == 1000.0
    assert usdc["delta_1d_native"] == 20.0
    assert usdc["market_value_usd"] == pytest.approx(999.8)
    assert usdc["peg_deviation_bps"] == pytest.approx(-2.0)
    assert usdc["chain_count"] == 2

    eurt = assets.loc[assets["symbol"] == "EURT"].iloc[0]
    assert eurt["circulating_native"] == 100.0
    assert eurt["market_value_usd"] == pytest.approx(108.0)
    assert eurt["peg_deviation_bps"] != eurt["peg_deviation_bps"]  # NaN: no fake USD peg target.

    eth_usdc = chains[(chains["symbol"] == "USDC") & (chains["chain"] == "Ethereum")].iloc[0]
    assert eth_usdc["circulating_native"] == 600.0
    assert eth_usdc["market_value_usd"] == pytest.approx(599.88)


def test_parse_stablecoin_snapshot_rejects_duplicate_ids() -> None:
    item = {
        "id": "dup",
        "symbol": "DUP",
        "pegType": "peggedUSD",
        "price": 1.0,
        "circulating": {"peggedUSD": 1.0},
        "chainCirculating": {},
    }
    envelope = {
        "observed_at": "2026-09-04T12:00:00Z",
        "known_at": "2026-09-04T12:00:00Z",
        "payload": {"peggedAssets": [item, item]},
    }
    with pytest.raises(ValueError, match="duplicated"):
        parse_stablecoin_snapshot(envelope, _manifest())
