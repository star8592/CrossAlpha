from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from crossalpha.core.free_paper import _verify_sealed as verify_core_seal
from crossalpha.outcomes import prospective
from crossalpha.state.ab_paper import _verify_sealed as verify_ab_seal
from crossalpha.state import v02_prospective, v03_prospective, v04_prospective


SOURCE_V02 = "STATE_V02"
SOURCE_V03 = "STATE_V03"
SOURCE_V04 = "STATE_V04"


def _utc(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_source_records(data_root: Path) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {
        SOURCE_V02: [],
        SOURCE_V03: [],
        SOURCE_V04: [],
    }
    v02_root = data_root / "research" / "state_v02" / "prospective"
    for path in sorted(v02_root.glob("year=*/month=*/day=*/state_at=*.json")) if v02_root.exists() else []:
        row = _read_json(path)
        if not v02_prospective._verify_sealed(row):
            raise ValueError(f"State V0.2 source seal failed: {path}")
        rows[SOURCE_V02].append({**row, "__path": str(path)})

    v03_root = data_root / "research" / "state_v03" / "prospective"
    for path in sorted(v03_root.glob("block=*.json")) if v03_root.exists() else []:
        row = _read_json(path)
        if not v03_prospective.verify_seal(row):
            raise ValueError(f"State V0.3 source seal failed: {path}")
        rows[SOURCE_V03].append({**row, "__path": str(path)})

    v04_root = data_root / "research" / "state_v04" / "prospective"
    for path in sorted(v04_root.glob("year=*/month=*/day=*/state_at=*.json")) if v04_root.exists() else []:
        row = _read_json(path)
        if not v04_prospective.verify_seal(row):
            raise ValueError(f"State V0.4 source seal failed: {path}")
        rows[SOURCE_V04].append({**row, "__path": str(path)})
    return rows


def _source_known_at(row: dict[str, Any]) -> pd.Timestamp:
    return _utc(row["known_at"])


def select_daily_anchors(
    source_records: dict[str, list[dict[str, Any]]],
    *,
    not_before: Any,
) -> list[dict[str, Any]]:
    floor = _utc(not_before)
    selected: list[dict[str, Any]] = []
    for source_layer in prospective.SOURCE_LAYERS:
        by_day: dict[date, dict[str, Any]] = {}
        for row in source_records.get(source_layer, []):
            known = _source_known_at(row)
            if known < floor:
                continue
            day = known.date()
            previous = by_day.get(day)
            if previous is None or known > _source_known_at(previous):
                by_day[day] = row
        for day in sorted(by_day):
            selected.append({"source_layer": source_layer, **by_day[day]})
    return sorted(selected, key=lambda row: (_source_known_at(row), row["source_layer"]))


def _mark_paths(data_root: Path) -> tuple[Path, Path]:
    return (
        data_root / "research" / "free_v01" / "paper" / "marks",
        data_root / "research" / "free_v01" / "state_ab_v01" / "marks",
    )


def _load_marks(data_root: Path) -> tuple[dict[date, dict[str, Any]], dict[date, dict[str, Any]]]:
    a_root, b_root = _mark_paths(data_root)
    a: dict[date, dict[str, Any]] = {}
    b: dict[date, dict[str, Any]] = {}
    if a_root.exists():
        for path in sorted(a_root.glob("year=*/month=*/date=*.json")):
            row = _read_json(path)
            if not verify_core_seal(row):
                raise ValueError(f"Frozen B3 outcome mark seal failed: {path}")
            key = date.fromisoformat(str(row["date"]))
            a[key] = {**row, "__path": str(path)}
    if b_root.exists():
        for path in sorted(b_root.glob("year=*/month=*/date=*.json")):
            row = _read_json(path)
            if not verify_ab_seal(row):
                raise ValueError(f"State A/B outcome mark seal failed: {path}")
            key = date.fromisoformat(str(row["date"]))
            b[key] = {**row, "__path": str(path)}
    return a, b


def _expected_dates(anchor_known_at: pd.Timestamp, horizon_days: int) -> list[date]:
    start = anchor_known_at.date() + timedelta(days=1)
    return [start + timedelta(days=i) for i in range(horizon_days)]


def _max_drawdown(returns: list[float]) -> float:
    equity = np.concatenate([[1.0], np.cumprod(1.0 + np.asarray(returns, dtype=float))])
    peaks = np.maximum.accumulate(equity)
    return float(np.min(equity / peaks - 1.0))


def _cum_return(returns: list[float]) -> float:
    return float(np.prod(1.0 + np.asarray(returns, dtype=float)) - 1.0)


def _source_features(source_layer: str, row: dict[str, Any]) -> dict[str, Any]:
    if source_layer == SOURCE_V02:
        keys = (
            "data_confidence",
            "descriptive_stress_score",
            "aave_market_pressure",
            "stablecoin_flow_pressure",
            "stablecoin_net_change_ratio",
            "stablecoin_migration_ratio",
            "basis_dispersion_pressure",
            "contagion_pressure",
            "aave_liquidation_events_24h",
            "aave_liquidation_events_7d",
            "deployment_activation_proxy",
        )
        return {key: row.get(key) for key in keys}
    if source_layer == SOURCE_V03:
        keys = (
            "account_call_coverage_ratio",
            "active_borrower_count",
            "total_active_debt_usd",
            "debt_weighted_hf_p10",
            "debt_weighted_hf_p25",
            "debt_weighted_hf_p50",
            "liquidatable_debt_share",
            "critical_hf_le_1_05_debt_share",
            "near_cliff_hf_le_1_20_debt_share",
            "watchlist_count",
        )
        return {key: row.get(key) for key in keys}
    if source_layer == SOURCE_V04:
        result: dict[str, Any] = {
            "data_confidence": row.get("data_confidence"),
            "funding_semantics": row.get("funding_semantics"),
        }
        for asset in ("BTC", "ETH"):
            metrics = row.get("assets", {}).get(asset, {})
            for key in (
                "valid_venue_count",
                "funding_comparable_venue_count",
                "spot_cross_venue_range_bps",
                "basis_median_bps",
                "basis_range_bps",
                "basis_std_bps",
                "funding_8h_median",
                "funding_8h_range",
                "perp_spread_median_bps",
                "perp_spread_max_bps",
                "total_open_interest_usd",
                "open_interest_hhi",
            ):
                result[f"{asset}_{key}"] = metrics.get(key)
        return result
    raise ValueError(f"unsupported outcome source layer: {source_layer}")


def _outcome_metrics(
    dates: list[date],
    a_marks: dict[date, dict[str, Any]],
    b_marks: dict[date, dict[str, Any]],
) -> dict[str, Any]:
    a_returns: list[float] = []
    b_returns: list[float] = []
    cash_returns: list[float] = []
    multipliers: list[float] = []
    a_links: list[dict[str, str]] = []
    b_links: list[dict[str, str]] = []
    for day in dates:
        a = a_marks[day]
        b = b_marks[day]
        if b.get("a_mark_record_sha256") != a.get("record_sha256"):
            raise RuntimeError(f"Outcome A/B hash mismatch on {day.isoformat()}")
        if str(a.get("date")) != str(b.get("date")):
            raise RuntimeError(f"Outcome A/B date mismatch on {day.isoformat()}")
        a_returns.append(float(a["net_return"]))
        b_returns.append(float(b["net_return"]))
        cash_returns.append(float(a["cash_return"]))
        multipliers.append(float(b["shadow_risk_multiplier"]))
        a_links.append(
            {
                "date": day.isoformat(),
                "record_sha256": str(a["record_sha256"]),
                "path": str(a["__path"]),
            }
        )
        b_links.append(
            {
                "date": day.isoformat(),
                "record_sha256": str(b["record_sha256"]),
                "path": str(b["__path"]),
                "a_mark_record_sha256": str(b["a_mark_record_sha256"]),
            }
        )
    return {
        "A_cumulative_net_return": _cum_return(a_returns),
        "B_cumulative_net_return": _cum_return(b_returns),
        "B_minus_A_cumulative_return": _cum_return(b_returns) - _cum_return(a_returns),
        "cash_cumulative_return": _cum_return(cash_returns),
        "A_max_drawdown": _max_drawdown(a_returns),
        "B_max_drawdown": _max_drawdown(b_returns),
        "A_worst_daily_return": min(a_returns),
        "B_worst_daily_return": min(b_returns),
        "A_negative_day_count": sum(value < 0 for value in a_returns),
        "B_negative_day_count": sum(value < 0 for value in b_returns),
        "intervention_day_count": sum(value < 1.0 for value in multipliers),
        "average_B_multiplier": float(np.mean(multipliers)),
        "A_mark_links": a_links,
        "B_mark_links": b_links,
    }


def _link_path(data_root: Path, source_layer: str, anchor_day: date, horizon_days: int) -> Path:
    return (
        data_root
        / "research"
        / "outcome_linkage_v01"
        / "links"
        / f"source={source_layer}"
        / f"year={anchor_day.year:04d}"
        / f"month={anchor_day.month:02d}"
        / f"anchor_date={anchor_day.isoformat()}"
        / f"horizon={horizon_days:02d}d.json"
    )


def _write_link(path: Path, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if path.exists():
        existing = _read_json(path)
        if not prospective.verify_seal(existing):
            raise ValueError(f"existing outcome link seal failed: {path}")
        stable_fields = (
            "source_record_sha256",
            "source_layer",
            "anchor_date",
            "horizon_days",
            "outcome_start_date",
            "outcome_end_date",
        )
        if any(existing.get(field) != payload.get(field) for field in stable_fields):
            raise RuntimeError(f"OUTCOME_LINK_COLLISION: {path}")
        return existing, "already_exists"
    sealed = prospective.seal(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sealed, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    return sealed, "written"


def materialize_outcome_links(
    data_root: Path,
    *,
    now: Any | None = None,
) -> dict[str, Any]:
    freeze = prospective.load_freeze(data_root)
    hashes_ok, hash_graph = prospective.hashes_unchanged(data_root, freeze)
    if not hashes_ok:
        raise RuntimeError(f"OUTCOME_LINKAGE_HASH_GRAPH_MUTATED: {hash_graph}")
    current = _utc(now or datetime.now(timezone.utc))
    sources = _load_source_records(data_root)
    anchors = select_daily_anchors(sources, not_before=freeze["frozen_at"])
    a_marks, b_marks = _load_marks(data_root)
    written = 0
    existing = 0
    pending = 0
    eligible_anchor_count = 0
    for anchor in anchors:
        source_layer = str(anchor["source_layer"])
        known = _source_known_at(anchor)
        anchor_day = known.date()
        source_path = Path(str(anchor["__path"]))
        source_sha = str(anchor.get("record_sha256", ""))
        if not source_sha:
            raise ValueError(f"source record missing record_sha256: {source_path}")
        eligible_anchor_count += 1
        for horizon in prospective.HORIZONS_DAYS:
            dates = _expected_dates(known, horizon)
            if any(day not in a_marks or day not in b_marks for day in dates):
                pending += 1
                continue
            metrics = _outcome_metrics(dates, a_marks, b_marks)
            latest_mark_known = max(
                _utc(a_marks[day]["known_at"]) for day in dates
            )
            if latest_mark_known > current:
                pending += 1
                continue
            payload = {
                "schema_version": prospective.SCHEMA_VERSION,
                "protocol": prospective.PROTOCOL,
                "mode": prospective.MODE,
                "freeze_record_sha256": freeze["record_sha256"],
                "materialized_at": current.isoformat(),
                "source_layer": source_layer,
                "source_record_path": str(source_path),
                "source_record_file_sha256": prospective.sha256_file(source_path),
                "source_record_sha256": source_sha,
                "source_known_at": known.isoformat(),
                "anchor_date": anchor_day.isoformat(),
                "anchor_selection": "latest_known_at_within_source_utc_day",
                "source_features": _source_features(source_layer, anchor),
                "horizon_days": horizon,
                "outcome_start_date": dates[0].isoformat(),
                "outcome_end_date": dates[-1].isoformat(),
                "same_day_outcome_included": False,
                "complete_daily_marks_required": True,
                **metrics,
                "actionability": "NONE",
                "risk_multiplier": None,
                "selective_linking_allowed": False,
            }
            path = _link_path(data_root, source_layer, anchor_day, horizon)
            _, status = _write_link(path, payload)
            if status == "written":
                written += 1
            else:
                existing += 1
    return {
        "protocol": prospective.PROTOCOL,
        "status": "materialized",
        "anchor_count": eligible_anchor_count,
        "written_links": written,
        "existing_links": existing,
        "pending_incomplete_horizons": pending,
        "horizons_days": list(prospective.HORIZONS_DAYS),
        "same_day_outcome_allowed": False,
        "selective_linking_allowed": False,
        "deterministic_late_materialization_allowed": True,
    }
