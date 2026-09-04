import json
from datetime import timedelta
from pathlib import Path

from crossalpha.domain.models import ObservationEnvelope, SourceType
from crossalpha.storage.raw import RawSnapshotStore


def test_raw_snapshot_store_is_append_only_and_indexed(tmp_path: Path) -> None:
    store = RawSnapshotStore(tmp_path)
    env = ObservationEnvelope(
        source_type=SourceType.AGGREGATOR,
        source_id="test",
        observation_type="snapshot",
        payload={"x": 1},
    )
    second_time = env.observed_at + timedelta(seconds=300)
    first = store.write(env)
    second = store.write(env.model_copy(update={"observed_at": second_time, "known_at": second_time}))

    assert Path(first.path).exists()
    assert Path(second.path).exists()
    assert first.path != second.path
    assert first.compressed_bytes == Path(first.path).stat().st_size
    assert second.compressed_bytes == Path(second.path).stat().st_size

    audit = tmp_path / "manifests" / "raw_snapshots.jsonl"
    assert len(audit.read_text(encoding="utf-8").splitlines()) == 2

    daily = (
        tmp_path
        / "manifests"
        / "daily"
        / f"year={env.observed_at:%Y}"
        / f"month={env.observed_at:%m}"
        / f"day={env.observed_at:%d}"
        / "raw_snapshots.jsonl"
    )
    assert len(daily.read_text(encoding="utf-8").splitlines()) == 2

    state_path = tmp_path / "manifests" / "series" / "test" / "snapshot.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["count"] == 2
    assert state["last_interval_seconds"] == 300.0
    assert state["latest_manifest"]["sha256"] == second.sha256
