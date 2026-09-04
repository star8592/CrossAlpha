from datetime import datetime, timezone

from crossalpha.domain.models import RawSnapshotManifest
from crossalpha.observatory.canonical.hyperliquid import parse_meta_and_asset_contexts


def test_parse_hyperliquid_meta_and_asset_contexts() -> None:
    observed = datetime(2026, 9, 4, 12, 35, tzinfo=timezone.utc)
    record = RawSnapshotManifest(
        path="/tmp/raw.json.gz",
        sha256="a" * 64,
        bytes=100,
        compressed_bytes=50,
        observed_at=observed,
        source_id="hyperliquid",
        observation_type="metaAndAssetCtxs",
    )
    envelope = {
        "observed_at": observed.isoformat(),
        "known_at": observed.isoformat(),
        "payload": [
            {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]},
            [
                {
                    "markPx": "100.0",
                    "oraclePx": "99.0",
                    "midPx": "100.5",
                    "prevDayPx": "95.0",
                    "premium": "0.01",
                    "funding": "0.0001",
                    "openInterest": "123.0",
                    "dayNtlVlm": "1000000",
                    "dayBaseVlm": "10",
                    "impactPxs": ["99.9", "100.1"],
                }
            ],
        ],
    }

    frame = parse_meta_and_asset_contexts(envelope, record)
    row = frame.iloc[0]
    assert row["asset"] == "BTC"
    assert row["mark_price"] == 100.0
    assert row["funding_rate"] == 0.0001
    assert row["open_interest"] == 123.0
    assert row["impact_bid"] == 99.9
    assert row["impact_ask"] == 100.1
    assert row["raw_sha256"] == "a" * 64
