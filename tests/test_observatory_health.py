from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from crossalpha.domain.models import ObservationEnvelope, SourceType
from crossalpha.observatory.health import observatory_health, verify_snapshot
from crossalpha.storage.raw import RawSnapshotStore


def _env(source_id: str, observation_type: str, observed_at: datetime) -> ObservationEnvelope:
    return ObservationEnvelope(
        source_type=SourceType.AGGREGATOR,
        source_id=source_id,
        observation_type=observation_type,
        observed_at=observed_at,
        known_at=observed_at,
        payload={"x": 1},
    )


def test_health_verifies_latest_snapshots_and_freshness(tmp_path: Path) -> None:
    store = RawSnapshotStore(tmp_path)
    now = datetime(2026, 9, 4, 12, 10, tzinfo=timezone.utc)
    expected = (
        ("hyperliquid", "metaAndAssetCtxs"),
        ("hyperliquid", "allMids"),
        ("defillama", "stablecoins_snapshot"),
    )
    manifests = []
    for source_id, observation_type in expected:
        manifests.append(store.write(_env(source_id, observation_type, now - timedelta(seconds=30))))

    report = observatory_health(tmp_path, now=now)
    assert report["ok"] is True
    assert report["manifest_records"] == 3
    assert report["compression_sample_records"] == 3
    assert report["compression_ratio"] is not None
    for manifest in manifests:
        assert verify_snapshot(manifest)["ok"] is True


def test_health_reports_stale_and_gap_without_hiding_history(tmp_path: Path) -> None:
    store = RawSnapshotStore(tmp_path)
    t0 = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
    for source_id, observation_type in (
        ("hyperliquid", "metaAndAssetCtxs"),
        ("hyperliquid", "allMids"),
        ("defillama", "stablecoins_snapshot"),
    ):
        store.write(_env(source_id, observation_type, t0))
        store.write(_env(source_id, observation_type, t0 + timedelta(seconds=1200)))

    report = observatory_health(
        tmp_path,
        expected_interval_seconds=300,
        stale_after_seconds=900,
        now=t0 + timedelta(seconds=2400),
    )
    assert report["ok"] is False
    for series in report["series"].values():
        assert series["stale"] is True
        assert series["gap_count"] == 1
        assert series["max_gap_seconds"] == 1200.0
