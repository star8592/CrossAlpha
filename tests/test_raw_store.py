from datetime import timedelta
from pathlib import Path

from crossalpha.domain.models import ObservationEnvelope, SourceType
from crossalpha.storage.raw import RawSnapshotStore


def test_raw_snapshot_store_is_append_only(tmp_path: Path) -> None:
    store = RawSnapshotStore(tmp_path)
    env = ObservationEnvelope(
        source_type=SourceType.AGGREGATOR,
        source_id="test",
        observation_type="snapshot",
        payload={"x": 1},
    )
    first = store.write(env)
    second = store.write(env.model_copy(update={"observed_at": env.observed_at + timedelta(microseconds=1)}))
    assert Path(first.path).exists()
    assert Path(second.path).exists()
    assert first.path != second.path
    assert first.compressed_bytes == Path(first.path).stat().st_size
    assert second.compressed_bytes == Path(second.path).stat().st_size
