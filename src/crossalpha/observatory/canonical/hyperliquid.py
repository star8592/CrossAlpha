from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pandas as pd

from crossalpha.domain.models import RawSnapshotManifest
from crossalpha.observatory.health import load_manifest


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_meta_and_asset_contexts(envelope: dict[str, Any], raw_record: RawSnapshotManifest) -> pd.DataFrame:
    payload = envelope.get("payload")
    if not isinstance(payload, list) or len(payload) != 2:
        raise ValueError("Hyperliquid metaAndAssetCtxs payload must be [meta, asset_contexts]")

    meta, contexts = payload
    if not isinstance(meta, dict) or not isinstance(contexts, list):
        raise ValueError("Hyperliquid metaAndAssetCtxs payload shape is invalid")

    universe = meta.get("universe")
    if not isinstance(universe, list):
        raise ValueError("Hyperliquid meta.universe is missing")
    if len(universe) != len(contexts):
        raise ValueError(f"Hyperliquid universe/context length mismatch: {len(universe)} != {len(contexts)}")

    observed_at = envelope.get("observed_at")
    known_at = envelope.get("known_at")
    rows: list[dict[str, Any]] = []
    for spec, ctx in zip(universe, contexts, strict=True):
        if not isinstance(spec, dict) or not isinstance(ctx, dict):
            raise ValueError("Hyperliquid universe/context item is not an object")
        impact = ctx.get("impactPxs")
        impact_bid = _to_float(impact[0]) if isinstance(impact, list) and len(impact) > 0 else None
        impact_ask = _to_float(impact[1]) if isinstance(impact, list) and len(impact) > 1 else None
        rows.append(
            {
                "observed_at": observed_at,
                "known_at": known_at,
                "asset": spec.get("name"),
                "sz_decimals": spec.get("szDecimals"),
                "max_leverage": spec.get("maxLeverage"),
                "only_isolated": spec.get("onlyIsolated"),
                "mark_price": _to_float(ctx.get("markPx")),
                "oracle_price": _to_float(ctx.get("oraclePx")),
                "mid_price": _to_float(ctx.get("midPx")),
                "prev_day_price": _to_float(ctx.get("prevDayPx")),
                "premium": _to_float(ctx.get("premium")),
                "funding_rate": _to_float(ctx.get("funding")),
                "open_interest": _to_float(ctx.get("openInterest")),
                "day_notional_volume": _to_float(ctx.get("dayNtlVlm")),
                "day_base_volume": _to_float(ctx.get("dayBaseVlm")),
                "impact_bid": impact_bid,
                "impact_ask": impact_ask,
                "raw_sha256": raw_record.sha256,
                "raw_path": raw_record.path,
            }
        )

    frame = pd.DataFrame(rows)
    if frame.empty:
        raise ValueError("Hyperliquid canonicalization produced zero assets")
    if frame["asset"].isna().any() or frame["asset"].duplicated().any():
        raise ValueError("Hyperliquid canonical asset names are missing or duplicated")
    return frame


def canonicalize_hyperliquid(data_root: Path) -> dict[str, int]:
    records, errors = load_manifest(data_root)
    if errors:
        raise ValueError(f"raw manifest contains errors: {errors}")

    selected = [
        record
        for record in records
        if record.source_id == "hyperliquid" and record.observation_type == "metaAndAssetCtxs"
    ]
    written = 0
    skipped = 0
    rows = 0

    for record in selected:
        observed = record.observed_at
        out_dir = (
            data_root
            / "canonical"
            / "hyperliquid"
            / "asset_contexts"
            / f"year={observed:%Y}"
            / f"month={observed:%m}"
            / f"day={observed:%d}"
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{observed:%Y%m%dT%H%M%S.%fZ}_{record.sha256[:12]}.parquet"
        if out_path.exists():
            skipped += 1
            continue

        with gzip.open(record.path, "rt", encoding="utf-8") as fh:
            envelope = json.load(fh)
        frame = parse_meta_and_asset_contexts(envelope, record)
        frame.to_parquet(out_path, index=False)
        written += 1
        rows += len(frame)

    return {"snapshots": len(selected), "written": written, "skipped": skipped, "rows_written": rows}
