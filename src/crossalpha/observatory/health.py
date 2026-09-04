from __future__ import annotations

import gzip
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from crossalpha.domain.models import RawSnapshotManifest


DEFAULT_EXPECTED_SERIES = (
    ("hyperliquid", "metaAndAssetCtxs"),
    ("hyperliquid", "allMids"),
    ("defillama", "stablecoins_snapshot"),
)


def load_manifest(data_root: Path) -> tuple[list[RawSnapshotManifest], list[str]]:
    path = data_root / "manifests" / "raw_snapshots.jsonl"
    if not path.exists():
        return [], [f"manifest missing: {path}"]

    records: list[RawSnapshotManifest] = []
    errors: list[str] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(RawSnapshotManifest.model_validate_json(line))
            except Exception as exc:  # noqa: BLE001 - manifest corruption must be reported, not hidden.
                errors.append(f"line {line_no}: {exc}")
    return records, errors


def verify_snapshot(record: RawSnapshotManifest) -> dict[str, Any]:
    path = Path(record.path)
    if not path.exists():
        return {"ok": False, "path": str(path), "error": "file missing"}

    try:
        with gzip.open(path, "rb") as fh:
            payload = fh.read()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "path": str(path), "error": f"gzip read failed: {exc}"}

    digest = hashlib.sha256(payload).hexdigest()
    compressed_bytes = path.stat().st_size
    errors: list[str] = []
    if digest != record.sha256:
        errors.append("sha256 mismatch")
    if len(payload) != record.bytes:
        errors.append("uncompressed byte count mismatch")
    if record.compressed_bytes is not None and compressed_bytes != record.compressed_bytes:
        errors.append("compressed byte count mismatch")

    return {
        "ok": not errors,
        "path": str(path),
        "sha256": digest,
        "bytes": len(payload),
        "compressed_bytes": compressed_bytes,
        "errors": errors,
    }


def observatory_health(
    data_root: Path,
    *,
    expected_interval_seconds: int = 300,
    stale_after_seconds: int = 900,
    expected_series: tuple[tuple[str, str], ...] = DEFAULT_EXPECTED_SERIES,
    verify_latest: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    records, manifest_errors = load_manifest(data_root)
    grouped: dict[tuple[str, str], list[RawSnapshotManifest]] = defaultdict(list)
    for record in records:
        grouped[(record.source_id, record.observation_type)].append(record)

    series_report: dict[str, Any] = {}
    current_ok = not manifest_errors
    gap_threshold = max(expected_interval_seconds * 2.5, expected_interval_seconds + 1)

    for key in expected_series:
        source_id, observation_type = key
        label = f"{source_id}/{observation_type}"
        items = sorted(grouped.get(key, []), key=lambda x: x.observed_at)
        if not items:
            series_report[label] = {
                "ok": False,
                "count": 0,
                "latest_observed_at": None,
                "age_seconds": None,
                "stale": True,
                "gap_count": 0,
                "max_gap_seconds": None,
                "latest_integrity": None,
            }
            current_ok = False
            continue

        latest = items[-1]
        latest_observed = latest.observed_at
        if latest_observed.tzinfo is None:
            latest_observed = latest_observed.replace(tzinfo=timezone.utc)
        age_seconds = max((now - latest_observed).total_seconds(), 0.0)
        stale = age_seconds > stale_after_seconds

        gaps: list[float] = []
        duplicate_timestamps = 0
        for prev, curr in zip(items, items[1:], strict=False):
            delta = (curr.observed_at - prev.observed_at).total_seconds()
            if delta == 0:
                duplicate_timestamps += 1
            if delta > gap_threshold:
                gaps.append(delta)

        integrity = verify_snapshot(latest) if verify_latest else None
        series_ok = not stale and (integrity is None or integrity["ok"])
        current_ok = current_ok and series_ok
        series_report[label] = {
            "ok": series_ok,
            "count": len(items),
            "latest_observed_at": latest.observed_at.isoformat(),
            "age_seconds": round(age_seconds, 3),
            "stale": stale,
            "gap_count": len(gaps),
            "max_gap_seconds": round(max(gaps), 3) if gaps else None,
            "duplicate_timestamps": duplicate_timestamps,
            "latest_integrity": integrity,
        }

    total_raw_bytes = sum(record.bytes for record in records)
    compression_sample = [record for record in records if record.compressed_bytes is not None]
    compression_sample_raw_bytes = sum(record.bytes for record in compression_sample)
    total_compressed_bytes = sum(record.compressed_bytes or 0 for record in compression_sample)
    compression_ratio = None
    if compression_sample_raw_bytes and total_compressed_bytes:
        compression_ratio = compression_sample_raw_bytes / total_compressed_bytes

    report = {
        "ok": current_ok,
        "checked_at": now.isoformat(),
        "manifest_records": len(records),
        "manifest_errors": manifest_errors,
        "expected_interval_seconds": expected_interval_seconds,
        "stale_after_seconds": stale_after_seconds,
        "raw_bytes_manifested": total_raw_bytes,
        "compression_sample_records": len(compression_sample),
        "compression_sample_raw_bytes": compression_sample_raw_bytes,
        "compressed_bytes_manifested": total_compressed_bytes,
        "compression_ratio": round(compression_ratio, 3) if compression_ratio else None,
        "series": series_report,
    }
    return report


def write_health_report(data_root: Path, report: dict[str, Any]) -> Path:
    path = data_root / "manifests" / "observatory_health.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path
