from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from crossalpha.domain.models import ObservationEnvelope, SourceType
from crossalpha.settings import Settings
from crossalpha.state.v04 import compute_market_mechanics
from crossalpha.state.v04_prospective import freeze_path, write_live_observation
from crossalpha.state.v04_provider import parse_venue_snapshot
from crossalpha.state.v04_safe_provider import FaultIsolatedMultiVenueCollector
from crossalpha.storage.raw import RawSnapshotStore


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_paths(data_root: Path, generated: pd.Timestamp) -> tuple[Path, Path]:
    root = (
        data_root
        / "derived"
        / "state"
        / "v04"
        / f"year={generated:%Y}"
        / f"month={generated:%m}"
        / f"day={generated:%d}"
    )
    stamp = generated.strftime("%H%M%S%f")
    return root / f"venues_at={stamp}.parquet", root / f"mechanics_at={stamp}.json"


def _latest_observed(payload: dict[str, Any], fallback: pd.Timestamp) -> pd.Timestamp:
    try:
        row = parse_venue_snapshot(payload, known_at=fallback)
        ts = pd.Timestamp(row["observed_at"])
        return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    except Exception:
        return fallback


async def run_state_v04_cycle(settings: Settings, *, write: bool = True) -> dict[str, Any]:
    """Collect and materialize one zero-cost multi-venue mechanics observation."""
    settings.ensure_dirs()
    data_root = settings.crossalpha_data_dir
    collected_at = pd.Timestamp(datetime.now(timezone.utc))
    collector = FaultIsolatedMultiVenueCollector(timeout=settings.crossalpha_http_timeout)
    payloads = await collector.collect()
    if len(payloads) != 6:
        raise RuntimeError(f"State V0.4 expected 6 venue/asset slots, got {len(payloads)}")

    store = RawSnapshotStore(data_root)
    normalized: list[dict[str, Any]] = []
    raw_records: list[dict[str, Any]] = []
    for payload in payloads:
        observed = _latest_observed(payload, collected_at)
        envelope = ObservationEnvelope(
            event_time=observed.to_pydatetime(),
            observed_at=observed.to_pydatetime(),
            known_at=collected_at.to_pydatetime(),
            source_type=SourceType.EXCHANGE,
            source_id=f"{payload['venue']}:public",
            observation_type="multi_venue_market_mechanics_snapshot",
            payload=payload,
            metadata={
                "asset": payload["asset"],
                "venue": payload["venue"],
                "data_cost_usd": 0,
                "authentication_required": False,
                "collection_error": payload.get("collection_error"),
            },
        )
        manifest = store.write(envelope)
        raw_path = Path(manifest.path)
        row = parse_venue_snapshot(payload, known_at=collected_at)
        row["collection_error"] = payload.get("collection_error")
        row["raw_sha256"] = manifest.sha256
        row["raw_compressed_file_sha256"] = _sha256_file(raw_path)
        row["raw_path"] = manifest.path
        normalized.append(row)
        raw_records.append(
            {
                "venue": payload["venue"],
                "asset": payload["asset"],
                "collection_error": payload.get("collection_error"),
                "raw_sha256": manifest.sha256,
                "raw_compressed_file_sha256": _sha256_file(raw_path),
                "raw_path": manifest.path,
            }
        )

    frame = pd.DataFrame(normalized).sort_values(["asset", "venue"]).reset_index(drop=True)
    generated = pd.Timestamp(datetime.now(timezone.utc))
    report = compute_market_mechanics(frame, generated_at=generated)
    if report.get("data_confidence") == "INSUFFICIENT":
        raise RuntimeError(f"State V0.4 insufficient multi-venue data: {report}")

    venue_path, report_path = _snapshot_paths(data_root, generated)
    prospective: dict[str, Any] = {"status": "not_frozen_no_prospective_write"}
    if write:
        venue_path.parent.mkdir(parents=True, exist_ok=True)
        venue_tmp = venue_path.with_suffix(".parquet.tmp")
        frame.to_parquet(venue_tmp, index=False)
        venue_tmp.replace(venue_path)
        report_payload = {
            **report,
            "venue_snapshot_path": str(venue_path),
            "raw_records": raw_records,
        }
        report_tmp = report_path.with_suffix(".json.tmp")
        report_tmp.write_text(
            json.dumps(report_payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        report_tmp.replace(report_path)
        if freeze_path(data_root).exists():
            prospective = write_live_observation(
                data_root,
                mechanics_path=report_path,
                venue_path=venue_path,
                now=pd.Timestamp(datetime.now(timezone.utc)),
            )
    else:
        report_payload = {**report, "raw_records": raw_records}

    return {
        "protocol": "CROSSALPHA_STATE_V0_4_CYCLE",
        "data_cost_usd": 0,
        "authentication_required": False,
        "actionability": "DESCRIPTIVE_ONLY",
        "risk_multiplier": None,
        "mutates_predecessors": False,
        "generated_at": generated.isoformat(),
        "collection_error_count": int(frame["collection_error"].notna().sum()),
        "state": report,
        "venue_rows": frame.to_dict(orient="records"),
        "venue_snapshot_path": str(venue_path) if write else None,
        "mechanics_snapshot_path": str(report_path) if write else None,
        "prospective": prospective,
        "written": bool(write),
    }
