import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crossalpha.domain.models import RawSnapshotManifest
from crossalpha.storage.indexes import rebuild_manifest_indexes


def test_rebuild_manifest_indexes_from_audit_ledger(tmp_path: Path) -> None:
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir(parents=True)
    audit = manifest_dir / "raw_snapshots.jsonl"
    t0 = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    records = [
        RawSnapshotManifest(
            path=f"/tmp/{i}.json.gz",
            sha256=str(i) * 64,
            bytes=100 + i,
            compressed_bytes=50 + i,
            observed_at=t0 + timedelta(minutes=5 * i),
            source_id="hyperliquid",
            observation_type="allMids",
        )
        for i in range(2)
    ]
    audit.write_text(
        "".join(json.dumps(record.model_dump(mode="json")) + "\n" for record in records),
        encoding="utf-8",
    )

    result = rebuild_manifest_indexes(tmp_path)
    assert result["records"] == 2

    state_path = tmp_path / "manifests" / "series" / "hyperliquid" / "allMids.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["count"] == 2
    assert state["last_interval_seconds"] == 300.0

    daily_files = list((tmp_path / "manifests" / "daily").glob("**/raw_snapshots.jsonl"))
    assert len(daily_files) == 1
    assert len(daily_files[0].read_text(encoding="utf-8").splitlines()) == 2
