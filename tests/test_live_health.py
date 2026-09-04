from datetime import datetime, timedelta, timezone
from pathlib import Path

from crossalpha.domain.models import ObservationEnvelope, SourceType
from crossalpha.observatory.live_health import observatory_live_health
from crossalpha.storage.raw import RawSnapshotStore


def test_live_health_uses_incremental_series_state(tmp_path: Path) -> None:
    store = RawSnapshotStore(tmp_path)
    now = datetime(2026, 9, 4, 12, 10, tzinfo=timezone.utc)
    for source_id, observation_type in (
        ("hyperliquid", "metaAndAssetCtxs"),
        ("hyperliquid", "allMids"),
        ("defillama", "stablecoins_snapshot"),
    ):
        observed = now - timedelta(seconds=30)
        store.write(
            ObservationEnvelope(
                source_type=SourceType.AGGREGATOR,
                source_id=source_id,
                observation_type=observation_type,
                observed_at=observed,
                known_at=observed,
                payload={"x": 1},
            )
        )

    report = observatory_live_health(tmp_path, now=now)
    assert report["ok"] is True
    assert report["mode"] == "series_state"
    assert report["manifest_records"] == 3
    assert report["compression_sample_records"] == 3
    for series in report["series"].values():
        assert series["stale"] is False
        assert series["latest_integrity"]["ok"] is True
