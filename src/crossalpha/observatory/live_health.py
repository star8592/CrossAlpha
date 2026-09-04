from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from crossalpha.domain.models import RawSnapshotManifest
from crossalpha.observatory.health import DEFAULT_EXPECTED_SERIES, observatory_health, verify_snapshot


def _state_path(data_root: Path, source_id: str, observation_type: str) -> Path:
    source = source_id.replace("/", "_").replace("..", "_")
    observation = observation_type.replace("/", "_").replace("..", "_")
    return data_root / "manifests" / "series" / source / f"{observation}.json"


def observatory_live_health(
    data_root: Path,
    *,
    stale_after_seconds: int = 900,
    expected_series: tuple[tuple[str, str], ...] = DEFAULT_EXPECTED_SERIES,
    verify_latest: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """O(1)-per-series health check using rebuildable incremental series state.

    Falls back to the full audit-ledger scan until O0.2 indexes have been built.
    Historical gap analysis deliberately stays in `observatory_health`; this path is
    for watchdog freshness/integrity checks and remains fast as the audit ledger grows.
    """
    now = now or datetime.now(timezone.utc)
    state_paths = [_state_path(data_root, source, observation) for source, observation in expected_series]
    if not all(path.exists() for path in state_paths):
        report = observatory_health(
            data_root,
            stale_after_seconds=stale_after_seconds,
            expected_series=expected_series,
            verify_latest=verify_latest,
            now=now,
        )
        report["mode"] = "full_manifest_fallback"
        return report

    current_ok = True
    series_report: dict[str, Any] = {}
    total_records = 0
    total_raw_bytes = 0
    compression_sample_records = 0
    compression_sample_raw_bytes = 0
    total_compressed_bytes = 0

    for (source_id, observation_type), state_path in zip(expected_series, state_paths, strict=True):
        state = json.loads(state_path.read_text(encoding="utf-8"))
        latest_manifest = RawSnapshotManifest.model_validate(state["latest_manifest"])
        latest_observed = latest_manifest.observed_at
        if latest_observed.tzinfo is None:
            latest_observed = latest_observed.replace(tzinfo=timezone.utc)
        age_seconds = max((now - latest_observed).total_seconds(), 0.0)
        stale = age_seconds > stale_after_seconds
        integrity = verify_snapshot(latest_manifest) if verify_latest else None
        series_ok = not stale and (integrity is None or integrity["ok"])
        current_ok = current_ok and series_ok

        count = int(state.get("count", 0))
        raw_bytes = int(state.get("raw_bytes_total", 0))
        sample_records = int(state.get("compression_sample_records", 0))
        sample_raw = int(state.get("compression_sample_raw_bytes", 0))
        compressed = int(state.get("compressed_bytes_total", 0))
        total_records += count
        total_raw_bytes += raw_bytes
        compression_sample_records += sample_records
        compression_sample_raw_bytes += sample_raw
        total_compressed_bytes += compressed

        label = f"{source_id}/{observation_type}"
        series_report[label] = {
            "ok": series_ok,
            "count": count,
            "latest_observed_at": latest_observed.isoformat(),
            "age_seconds": round(age_seconds, 3),
            "stale": stale,
            "last_interval_seconds": state.get("last_interval_seconds"),
            "max_interval_seconds": state.get("max_interval_seconds"),
            "latest_integrity": integrity,
        }

    compression_ratio = None
    if compression_sample_raw_bytes and total_compressed_bytes:
        compression_ratio = compression_sample_raw_bytes / total_compressed_bytes

    return {
        "ok": current_ok,
        "mode": "series_state",
        "checked_at": now.isoformat(),
        "manifest_records": total_records,
        "manifest_errors": [],
        "stale_after_seconds": stale_after_seconds,
        "raw_bytes_manifested": total_raw_bytes,
        "compression_sample_records": compression_sample_records,
        "compression_sample_raw_bytes": compression_sample_raw_bytes,
        "compressed_bytes_manifested": total_compressed_bytes,
        "compression_ratio": round(compression_ratio, 3) if compression_ratio else None,
        "series": series_report,
    }
