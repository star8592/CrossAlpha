from __future__ import annotations

from datetime import datetime, timezone

from crossalpha.domain.models import RawSnapshotManifest
from crossalpha.observatory.canonical.aave import parse_aave_liquidations
from crossalpha.observatory.providers.aave import LIQUIDATION_CALL_TOPIC


def _manifest() -> RawSnapshotManifest:
    return RawSnapshotManifest(
        path="/tmp/raw.json.gz",
        sha256="b" * 64,
        bytes=100,
        compressed_bytes=50,
        observed_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
        source_id="aave:v3:ethereum",
        observation_type="liquidation_logs",
    )


def _topic(address: str) -> str:
    return "0x" + "0" * 24 + address.removeprefix("0x")


def test_removed_liquidation_keeps_audit_record_but_has_no_event_time() -> None:
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
                "removed": True,
                "topics": [
                    LIQUIDATION_CALL_TOPIC,
                    _topic("0x1111111111111111111111111111111111111111"),
                    _topic("0x2222222222222222222222222222222222222222"),
                    _topic("0x3333333333333333333333333333333333333333"),
                ],
                "data": "0x" + "0" * 256,
            }
        ],
    }
    frame = parse_aave_liquidations(envelope, _manifest())
    assert len(frame) == 1
    assert bool(frame.iloc[0]["removed"]) is True
    assert frame.iloc[0]["event_time"] is None
