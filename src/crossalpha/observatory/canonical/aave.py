from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pandas as pd

from crossalpha.domain.models import RawSnapshotManifest
from crossalpha.observatory.health import load_manifest
from crossalpha.storage.indexes import load_recent_daily_manifests


LIQUIDATION_COLUMNS = (
    "event_time",
    "observed_at",
    "known_at",
    "block_number",
    "transaction_hash",
    "transaction_index",
    "log_index",
    "block_hash",
    "removed",
    "collateral_asset",
    "debt_asset",
    "user",
    "debt_to_cover_raw",
    "liquidated_collateral_amount_raw",
    "liquidator",
    "receive_atoken",
    "raw_sha256",
    "raw_path",
)


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = value.strip().replace("%", "").replace(",", "")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if pd.notna(number) else None


def _to_int_hex(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def _topic_address(value: Any) -> str | None:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) < 42:
        return None
    return "0x" + value[-40:].lower()


def parse_aave_markets(
    envelope: dict[str, Any], raw_record: RawSnapshotManifest
) -> pd.DataFrame:
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Aave markets payload must be an object")
    markets = payload.get("data", {}).get("markets")
    if not isinstance(markets, list) or not markets:
        raise ValueError("Aave markets payload has no markets")

    observed_at = envelope.get("observed_at")
    known_at = envelope.get("known_at")
    metadata = envelope.get("metadata") if isinstance(envelope.get("metadata"), dict) else {}
    chain_id = metadata.get("chain_id")
    rows: list[dict[str, Any]] = []
    for market in markets:
        if not isinstance(market, dict):
            continue
        market_address = market.get("address")
        market_name = market.get("name")
        reserves = market.get("reserves")
        if not isinstance(reserves, list):
            continue
        for reserve in reserves:
            if not isinstance(reserve, dict):
                continue
            token = reserve.get("underlyingToken") or {}
            supply_info = reserve.get("supplyInfo") or {}
            borrow_info = reserve.get("borrowInfo") or {}
            supply_apy = supply_info.get("apy") or {}
            borrow_apy = borrow_info.get("apy") or {}
            available = borrow_info.get("availableLiquidity") or {}
            amount = available.get("amount") or {}
            rows.append(
                {
                    "observed_at": observed_at,
                    "known_at": known_at,
                    "chain_id": chain_id,
                    "market_address": str(market_address).lower() if market_address else None,
                    "market_name": market_name,
                    "reserve_address": str(token.get("address")).lower() if token.get("address") else None,
                    "symbol": token.get("symbol"),
                    "decimals": int(token["decimals"]) if str(token.get("decimals", "")).isdigit() else None,
                    "supply_apy_pct": _to_float(supply_apy.get("formatted")),
                    "borrow_apy_pct": _to_float(borrow_apy.get("formatted")),
                    "available_liquidity_native": _to_float(amount.get("value")),
                    "available_liquidity_usd": _to_float(available.get("usd")),
                    "borrow_cap_reached": bool(borrow_info.get("borrowCapReached", False)),
                    "is_frozen": bool(reserve.get("isFrozen", False)),
                    "is_paused": bool(reserve.get("isPaused", False)),
                    "raw_sha256": raw_record.sha256,
                    "raw_path": raw_record.path,
                }
            )
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise ValueError("Aave markets canonicalization produced zero reserves")
    if frame["symbol"].isna().all():
        raise ValueError("Aave markets canonicalization produced no reserve symbols")
    return frame


def parse_aave_liquidations(
    envelope: dict[str, Any], raw_record: RawSnapshotManifest
) -> pd.DataFrame:
    payload = envelope.get("payload")
    if not isinstance(payload, list):
        raise ValueError("Aave liquidation payload must be a list")
    observed_at = envelope.get("observed_at")
    known_at = envelope.get("known_at")
    rows: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        topics = item.get("topics")
        if not isinstance(topics, list) or len(topics) < 4:
            continue
        data = item.get("data")
        words: list[str] = []
        if isinstance(data, str) and data.startswith("0x"):
            raw = data[2:]
            words = [
                raw[i : i + 64]
                for i in range(0, len(raw), 64)
                if len(raw[i : i + 64]) == 64
            ]
        debt_to_cover_raw = int(words[0], 16) if len(words) > 0 else None
        collateral_amount_raw = int(words[1], 16) if len(words) > 1 else None
        liquidator = "0x" + words[2][-40:].lower() if len(words) > 2 else None
        receive_atoken = bool(int(words[3], 16)) if len(words) > 3 else None
        removed = bool(item.get("removed", False))
        rows.append(
            {
                # Preserve the removed record itself, but remove it from event-time
                # counts after point-in-time deduplication.
                "event_time": None if removed else item.get("blockTimestamp"),
                "observed_at": observed_at,
                "known_at": known_at,
                "block_number": _to_int_hex(item.get("blockNumber")),
                "transaction_hash": item.get("transactionHash"),
                "transaction_index": _to_int_hex(item.get("transactionIndex")),
                "log_index": _to_int_hex(item.get("logIndex")),
                "block_hash": item.get("blockHash"),
                "removed": removed,
                "collateral_asset": _topic_address(topics[1]),
                "debt_asset": _topic_address(topics[2]),
                "user": _topic_address(topics[3]),
                "debt_to_cover_raw": debt_to_cover_raw,
                "liquidated_collateral_amount_raw": collateral_amount_raw,
                "liquidator": liquidator,
                "receive_atoken": receive_atoken,
                "raw_sha256": raw_record.sha256,
                "raw_path": raw_record.path,
            }
        )
    # An empty liquidation scan is valid evidence (zero observed events), not an
    # absent schema. Keeping fixed columns lets parquet/catalog readers distinguish
    # a successful zero-event scan from a missing collector.
    return pd.DataFrame(rows, columns=LIQUIDATION_COLUMNS)


def _records(data_root: Path, recent_days: int | None) -> tuple[list[RawSnapshotManifest], str]:
    if recent_days is None:
        records, errors = load_manifest(data_root)
        mode = "full"
    else:
        records, errors = load_recent_daily_manifests(data_root, days=recent_days)
        mode = f"recent_{recent_days}d"
    if errors:
        raise ValueError(f"raw manifest contains errors: {errors}")
    return records, mode


def canonicalize_aave(
    data_root: Path,
    *,
    recent_days: int | None = None,
) -> dict[str, int | str]:
    records, mode = _records(data_root, recent_days)
    market_records = [
        row
        for row in records
        if row.source_id == "aave:v3:graphql" and row.observation_type == "markets_snapshot"
    ]
    liquidation_records = [
        row
        for row in records
        if row.source_id == "aave:v3:ethereum" and row.observation_type == "liquidation_logs"
    ]

    market_written = market_skipped = market_rows = 0
    liquidation_written = liquidation_skipped = liquidation_rows = 0

    for record in sorted(market_records, key=lambda item: item.observed_at):
        observed = record.observed_at
        out_dir = (
            data_root
            / "canonical"
            / "aave"
            / "markets"
            / f"year={observed:%Y}"
            / f"month={observed:%m}"
            / f"day={observed:%d}"
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{observed:%Y%m%dT%H%M%S.%fZ}_{record.sha256[:12]}.parquet"
        if out_path.exists():
            market_skipped += 1
            continue
        with gzip.open(record.path, "rt", encoding="utf-8") as fh:
            envelope = json.load(fh)
        frame = parse_aave_markets(envelope, record)
        tmp = out_path.with_suffix(".parquet.tmp")
        frame.to_parquet(tmp, index=False)
        tmp.replace(out_path)
        market_written += 1
        market_rows += len(frame)

    for record in sorted(liquidation_records, key=lambda item: item.observed_at):
        observed = record.observed_at
        out_dir = (
            data_root
            / "canonical"
            / "aave"
            / "liquidations"
            / f"year={observed:%Y}"
            / f"month={observed:%m}"
            / f"day={observed:%d}"
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{observed:%Y%m%dT%H%M%S.%fZ}_{record.sha256[:12]}.parquet"
        if out_path.exists():
            liquidation_skipped += 1
            continue
        with gzip.open(record.path, "rt", encoding="utf-8") as fh:
            envelope = json.load(fh)
        frame = parse_aave_liquidations(envelope, record)
        tmp = out_path.with_suffix(".parquet.tmp")
        frame.to_parquet(tmp, index=False)
        tmp.replace(out_path)
        liquidation_written += 1
        liquidation_rows += len(frame)

    return {
        "mode": mode,
        "market_snapshots": len(market_records),
        "market_written": market_written,
        "market_skipped": market_skipped,
        "market_rows_written": market_rows,
        "liquidation_snapshots": len(liquidation_records),
        "liquidation_written": liquidation_written,
        "liquidation_skipped": liquidation_skipped,
        "liquidation_rows_written": liquidation_rows,
    }
