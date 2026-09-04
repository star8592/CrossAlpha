from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import fcntl

from crossalpha.domain.models import RawSnapshotManifest


def _safe_component(value: str) -> str:
    return value.replace("/", "_").replace("..", "_")


@contextmanager
def manifest_lock(data_root: Path) -> Iterator[None]:
    manifest_dir = data_root / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    lock_path = manifest_dir / ".manifest.lock"
    with lock_path.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def append_daily_manifest(data_root: Path, record: RawSnapshotManifest) -> Path:
    observed = record.observed_at
    path = (
        data_root
        / "manifests"
        / "daily"
        / f"year={observed:%Y}"
        / f"month={observed:%m}"
        / f"day={observed:%d}"
        / "raw_snapshots.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False) + "\n")
    return path


def update_series_state(data_root: Path, record: RawSnapshotManifest) -> Path:
    source = _safe_component(record.source_id)
    observation = _safe_component(record.observation_type)
    path = data_root / "manifests" / "series" / source / f"{observation}.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    state: dict[str, object]
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
    else:
        state = {
            "schema_version": 1,
            "source_id": record.source_id,
            "observation_type": record.observation_type,
            "count": 0,
            "first_observed_at": None,
            "latest_observed_at": None,
            "previous_observed_at": None,
            "last_interval_seconds": None,
            "max_interval_seconds": None,
            "raw_bytes_total": 0,
            "compressed_bytes_total": 0,
            "compression_sample_records": 0,
            "compression_sample_raw_bytes": 0,
        }

    previous = state.get("latest_observed_at")
    interval_seconds: float | None = None
    if isinstance(previous, str):
        previous_dt = datetime.fromisoformat(previous)
        if previous_dt.tzinfo is None:
            previous_dt = previous_dt.replace(tzinfo=timezone.utc)
        interval_seconds = (record.observed_at - previous_dt).total_seconds()

    state["count"] = int(state.get("count", 0)) + 1
    if state.get("first_observed_at") is None:
        state["first_observed_at"] = record.observed_at.isoformat()
    state["previous_observed_at"] = previous
    state["latest_observed_at"] = record.observed_at.isoformat()
    state["last_interval_seconds"] = interval_seconds
    if interval_seconds is not None:
        old_max = state.get("max_interval_seconds")
        state["max_interval_seconds"] = max(float(old_max or 0.0), interval_seconds)
    state["raw_bytes_total"] = int(state.get("raw_bytes_total", 0)) + record.bytes
    if record.compressed_bytes is not None:
        state["compressed_bytes_total"] = int(state.get("compressed_bytes_total", 0)) + record.compressed_bytes
        state["compression_sample_records"] = int(state.get("compression_sample_records", 0)) + 1
        state["compression_sample_raw_bytes"] = int(state.get("compression_sample_raw_bytes", 0)) + record.bytes
    state["latest_manifest"] = record.model_dump(mode="json")
    state["updated_at"] = datetime.now(timezone.utc).isoformat()

    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def append_indexes_unlocked(data_root: Path, record: RawSnapshotManifest) -> None:
    append_daily_manifest(data_root, record)
    update_series_state(data_root, record)


def load_recent_daily_manifests(
    data_root: Path,
    *,
    days: int = 2,
) -> tuple[list[RawSnapshotManifest], list[str]]:
    """Read only the most recent daily manifest partitions for online materialization."""
    if days < 1:
        raise ValueError("days must be >= 1")
    daily_root = data_root / "manifests" / "daily"
    paths = sorted(daily_root.glob("year=*/month=*/day=*/raw_snapshots.jsonl")) if daily_root.exists() else []
    selected = paths[-days:]
    if not selected:
        return [], [f"daily manifests missing under: {daily_root}"]

    records: list[RawSnapshotManifest] = []
    errors: list[str] = []
    with manifest_lock(data_root):
        for path in selected:
            with path.open("r", encoding="utf-8") as fh:
                for line_no, line in enumerate(fh, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(RawSnapshotManifest.model_validate_json(line))
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{path}: line {line_no}: {exc}")
    return records, errors


def rebuild_manifest_indexes(data_root: Path) -> dict[str, object]:
    """Rebuild derived daily manifests and series state from the immutable audit ledger."""
    audit_path = data_root / "manifests" / "raw_snapshots.jsonl"
    if not audit_path.exists():
        raise FileNotFoundError(f"audit manifest missing: {audit_path}")

    with manifest_lock(data_root):
        records: list[RawSnapshotManifest] = []
        with audit_path.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(RawSnapshotManifest.model_validate_json(line))
                except Exception as exc:  # noqa: BLE001
                    raise ValueError(f"invalid audit manifest line {line_no}: {exc}") from exc

        daily_root = data_root / "manifests" / "daily"
        series_root = data_root / "manifests" / "series"
        shutil.rmtree(daily_root, ignore_errors=True)
        shutil.rmtree(series_root, ignore_errors=True)

        for record in sorted(records, key=lambda item: item.observed_at):
            append_indexes_unlocked(data_root, record)

    return {
        "records": len(records),
        "daily_root": str(daily_root),
        "series_root": str(series_root),
    }
