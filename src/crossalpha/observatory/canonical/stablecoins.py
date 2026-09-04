from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pandas as pd

from crossalpha.domain.models import RawSnapshotManifest
from crossalpha.observatory.health import load_manifest
from crossalpha.storage.indexes import load_recent_daily_manifests


CANONICAL_SCHEMA_VERSION = 3


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _peg_amount(value: Any, peg_type: str | None) -> float | None:
    """Extract the native peg-unit amount without pretending all pegs are USD."""
    if isinstance(value, dict):
        if peg_type and peg_type in value:
            return _to_float(value.get(peg_type))
        numeric = [_to_float(item) for item in value.values()]
        numeric = [item for item in numeric if item is not None]
        if len(numeric) == 1:
            return numeric[0]
        return None
    return _to_float(value)


def _chain_measure(chain_value: Any, field: str, peg_type: str | None) -> float | None:
    """Extract a DefiLlama per-chain supply measure.

    The current public payload uses::

        chainCirculating[chain]["current"][pegType]
        chainCirculating[chain]["circulatingPrevDay"][pegType]
        chainCirculating[chain]["circulatingPrevWeek"][pegType]
        chainCirculating[chain]["circulatingPrevMonth"][pegType]

    Older fixtures/wrappers have also exposed the current amount under
    ``circulating`` or directly under the peg-type key. CrossAlpha accepts all
    three shapes but always writes one stable canonical schema.
    """
    if not isinstance(chain_value, dict):
        return None

    # DefiLlama calls the per-chain current field `current`, while the asset-level
    # equivalent is named `circulating`. Keep the canonical name circulating_native.
    upstream_field = "current" if field == "circulating" else field
    if upstream_field in chain_value:
        return _peg_amount(chain_value.get(upstream_field), peg_type)

    # Backward compatibility with older nested wrappers/fixtures.
    if field in chain_value:
        return _peg_amount(chain_value.get(field), peg_type)

    # Legacy flat current shape: {"peggedUSD": 123.0}.
    if field == "circulating":
        return _peg_amount(chain_value, peg_type)
    return None


def _canonical_file_version(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        frame = pd.read_parquet(path, columns=["canonical_schema_version"])
    except Exception:  # noqa: BLE001 - old schema intentionally falls back to zero.
        return 0
    if frame.empty:
        return 0
    value = pd.to_numeric(frame["canonical_schema_version"], errors="coerce").dropna()
    return int(value.iloc[0]) if not value.empty else 0


def parse_stablecoin_snapshot(
    envelope: dict[str, Any],
    raw_record: RawSnapshotManifest,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("DefiLlama stablecoin payload must be an object")
    assets = payload.get("peggedAssets")
    if not isinstance(assets, list):
        raise ValueError("DefiLlama stablecoin payload.peggedAssets is missing")

    observed_at = envelope.get("observed_at")
    known_at = envelope.get("known_at")
    asset_rows: list[dict[str, Any]] = []
    chain_rows: list[dict[str, Any]] = []

    for item in assets:
        if not isinstance(item, dict):
            continue
        asset_id = item.get("id")
        symbol = item.get("symbol")
        peg_type = item.get("pegType")
        price_usd = _to_float(item.get("price"))

        current = _peg_amount(item.get("circulating"), peg_type)
        prev_day = _peg_amount(item.get("circulatingPrevDay"), peg_type)
        prev_week = _peg_amount(item.get("circulatingPrevWeek"), peg_type)
        prev_month = _peg_amount(item.get("circulatingPrevMonth"), peg_type)

        market_value_usd = current * price_usd if current is not None and price_usd is not None else None
        peg_deviation_bps = None
        if peg_type == "peggedUSD" and price_usd is not None:
            peg_deviation_bps = (price_usd - 1.0) * 10_000.0

        chain_map = item.get("chainCirculating")
        if not isinstance(chain_map, dict):
            chain_map = {}

        asset_rows.append(
            {
                "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
                "observed_at": observed_at,
                "known_at": known_at,
                "stablecoin_id": asset_id,
                "name": item.get("name"),
                "symbol": symbol,
                "peg_type": peg_type,
                "peg_mechanism": item.get("pegMechanism"),
                "price_source": item.get("priceSource"),
                "price_usd": price_usd,
                "circulating_native": current,
                "circulating_prev_day_native": prev_day,
                "circulating_prev_week_native": prev_week,
                "circulating_prev_month_native": prev_month,
                "delta_1d_native": current - prev_day if current is not None and prev_day is not None else None,
                "delta_7d_native": current - prev_week if current is not None and prev_week is not None else None,
                "delta_30d_native": current - prev_month if current is not None and prev_month is not None else None,
                "market_value_usd": market_value_usd,
                "peg_deviation_bps": peg_deviation_bps,
                "chain_count": len(chain_map),
                "raw_sha256": raw_record.sha256,
                "raw_path": raw_record.path,
            }
        )

        for chain, chain_value in chain_map.items():
            chain_current = _chain_measure(chain_value, "circulating", peg_type)
            chain_prev_day = _chain_measure(chain_value, "circulatingPrevDay", peg_type)
            chain_prev_week = _chain_measure(chain_value, "circulatingPrevWeek", peg_type)
            chain_prev_month = _chain_measure(chain_value, "circulatingPrevMonth", peg_type)
            chain_rows.append(
                {
                    "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
                    "observed_at": observed_at,
                    "known_at": known_at,
                    "stablecoin_id": asset_id,
                    "name": item.get("name"),
                    "symbol": symbol,
                    "peg_type": peg_type,
                    "chain": chain,
                    "circulating_native": chain_current,
                    "circulating_prev_day_native": chain_prev_day,
                    "circulating_prev_week_native": chain_prev_week,
                    "circulating_prev_month_native": chain_prev_month,
                    "delta_1d_native": (
                        chain_current - chain_prev_day
                        if chain_current is not None and chain_prev_day is not None
                        else None
                    ),
                    "delta_7d_native": (
                        chain_current - chain_prev_week
                        if chain_current is not None and chain_prev_week is not None
                        else None
                    ),
                    "delta_30d_native": (
                        chain_current - chain_prev_month
                        if chain_current is not None and chain_prev_month is not None
                        else None
                    ),
                    "market_value_usd": (
                        chain_current * price_usd
                        if chain_current is not None and price_usd is not None
                        else None
                    ),
                    "price_usd": price_usd,
                    "raw_sha256": raw_record.sha256,
                    "raw_path": raw_record.path,
                }
            )

    asset_frame = pd.DataFrame(asset_rows)
    chain_frame = pd.DataFrame(chain_rows)
    if asset_frame.empty:
        raise ValueError("stablecoin canonicalization produced zero assets")
    if asset_frame["stablecoin_id"].isna().any() or asset_frame["stablecoin_id"].duplicated().any():
        raise ValueError("stablecoin ids are missing or duplicated")
    return asset_frame, chain_frame


def canonicalize_stablecoins(data_root: Path, *, recent_days: int | None = None) -> dict[str, int | str]:
    if recent_days is None:
        records, errors = load_manifest(data_root)
        mode = "full"
    else:
        records, errors = load_recent_daily_manifests(data_root, days=recent_days)
        mode = f"recent_{recent_days}d"
    if errors:
        raise ValueError(f"raw manifest contains errors: {errors}")

    selected = [
        record
        for record in records
        if record.source_id == "defillama" and record.observation_type == "stablecoins_snapshot"
    ]
    written = 0
    rewritten = 0
    skipped = 0
    asset_rows = 0
    chain_rows = 0

    for record in selected:
        observed = record.observed_at
        base = (
            f"year={observed:%Y}/month={observed:%m}/day={observed:%d}/"
            f"{observed:%Y%m%dT%H%M%S.%fZ}_{record.sha256[:12]}.parquet"
        )
        asset_path = data_root / "canonical" / "defillama" / "stablecoin_assets" / base
        chain_path = data_root / "canonical" / "defillama" / "stablecoin_chain_supply" / base
        asset_version = _canonical_file_version(asset_path)
        chain_version = _canonical_file_version(chain_path)
        if asset_version >= CANONICAL_SCHEMA_VERSION and chain_version >= CANONICAL_SCHEMA_VERSION:
            skipped += 1
            continue
        replacing_old = asset_path.exists() or chain_path.exists()

        with gzip.open(record.path, "rt", encoding="utf-8") as fh:
            envelope = json.load(fh)
        assets, chains = parse_stablecoin_snapshot(envelope, record)

        asset_path.parent.mkdir(parents=True, exist_ok=True)
        chain_path.parent.mkdir(parents=True, exist_ok=True)
        asset_tmp = asset_path.with_suffix(".parquet.tmp")
        chain_tmp = chain_path.with_suffix(".parquet.tmp")
        assets.to_parquet(asset_tmp, index=False)
        chains.to_parquet(chain_tmp, index=False)
        asset_tmp.replace(asset_path)
        chain_tmp.replace(chain_path)
        written += 1
        if replacing_old:
            rewritten += 1
        asset_rows += len(assets)
        chain_rows += len(chains)

    return {
        "mode": mode,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "snapshots": len(selected),
        "written": written,
        "rewritten": rewritten,
        "skipped": skipped,
        "asset_rows_written": asset_rows,
        "chain_rows_written": chain_rows,
    }
