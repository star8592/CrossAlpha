from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from crossalpha.domain.models import ObservationEnvelope, SourceType
from crossalpha.storage.indexes import load_recent_daily_manifests
from crossalpha.storage.raw import RawSnapshotStore


def test_recent_daily_manifest_reader_bounds_online_history(tmp_path: Path) -> None:
    store = RawSnapshotStore(tmp_path)
    start = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    for day in range(3):
        observed = start + timedelta(days=day)
        store.write(
            ObservationEnvelope(
                source_type=SourceType.EXCHANGE,
                source_id="hyperliquid",
                observation_type="metaAndAssetCtxs",
                observed_at=observed,
                known_at=observed,
                payload={"day": day},
            )
        )

    records, errors = load_recent_daily_manifests(tmp_path, days=2)
    assert errors == []
    assert len(records) == 2
    assert [record.observed_at.date().isoformat() for record in records] == ["2026-09-02", "2026-09-03"]
