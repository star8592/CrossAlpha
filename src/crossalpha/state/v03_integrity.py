from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from crossalpha.state import v03
from crossalpha.state import v03_prospective as prospective


LINKED_METRICS = (
    "candidate_address_count",
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


def _load_records(data_root: Path) -> list[dict[str, Any]]:
    root = data_root / "research" / "state_v03" / "prospective"
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in root.glob("block=*.json"):
        row = json.loads(path.read_text(encoding="utf-8"))
        row["__path"] = str(path)
        rows.append(row)
    return sorted(rows, key=lambda row: int(row.get("block_number", -1)))


def _bootstrap_state(data_root: Path) -> dict[str, Any]:
    path = data_root / "research" / "state_v03" / "bootstrap_state.json"
    if not path.exists():
        return {"bootstrap_complete": False}
    return json.loads(path.read_text(encoding="utf-8"))


def _numeric_equal(left: Any, right: Any, *, tolerance: float = 1e-12) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        a = float(left)
        b = float(right)
    except (TypeError, ValueError):
        return left == right
    return math.isfinite(a) and math.isfinite(b) and math.isclose(a, b, rel_tol=tolerance, abs_tol=tolerance)


def _count_stress_episodes(
    records: list[dict[str, Any]],
    *,
    threshold: float,
    cooldown_hours: int,
) -> int:
    qualifying = []
    for row in records:
        value = row.get("critical_hf_le_1_05_debt_share")
        if value is None:
            continue
        try:
            share = float(value)
        except (TypeError, ValueError):
            continue
        if share >= threshold:
            qualifying.append(pd.Timestamp(row["known_at"]))
    qualifying = sorted(
        ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        for ts in qualifying
    )
    count = 0
    last: pd.Timestamp | None = None
    cooldown = pd.Timedelta(hours=cooldown_hours)
    for timestamp in qualifying:
        if last is None or timestamp - last >= cooldown:
            count += 1
            last = timestamp
    return count


def strict_state_v03_integrity_report(data_root: Path) -> dict[str, Any]:
    try:
        freeze = prospective.load_freeze(data_root)
    except FileNotFoundError:
        return {
            "protocol": prospective.PROSPECTIVE_PROTOCOL,
            "audit_level": "STRICT_NON_MUTATING_HASH_GRAPH",
            "frozen": False,
            "ok": False,
            "error": "not_frozen",
        }

    records = _load_records(data_root)
    hashes_ok, hash_details = prospective.hashes_unchanged(data_root, freeze)
    frozen_at = pd.Timestamp(freeze["frozen_at"])
    frozen_at = frozen_at.tz_localize("UTC") if frozen_at.tzinfo is None else frozen_at.tz_convert("UTC")
    minimum_block = int(freeze["minimum_eligible_block"])

    checks = {
        "freeze_seal": prospective.verify_seal(freeze),
        "implementation_and_reference_hashes_unchanged": hashes_ok,
        "record_seals": True,
        "freeze_links": True,
        "record_filename_matches_block": True,
        "block_not_before_freeze_floor": True,
        "known_at_not_before_freeze": True,
        "captured_at_not_before_freeze": True,
        "known_at_not_before_captured_at": True,
        "descriptive_only": True,
        "artifact_hash_links": True,
        "summary_semantics": True,
        "summary_metric_links": True,
        "detail_unique_addresses": True,
        "detail_candidate_count": True,
        "detail_coverage_recomputed": True,
        "detail_active_debt_recomputed": True,
        "unique_block_numbers": True,
        "block_numbers_monotonic": True,
    }

    blocks: list[int] = []
    known_times: list[pd.Timestamp] = []
    seen_blocks: set[int] = set()
    for raw in records:
        record_path = Path(str(raw.get("__path", "")))
        row = dict(raw)
        row.pop("__path", None)
        if not prospective.verify_seal(row):
            checks["record_seals"] = False
        if row.get("freeze_record_sha256") != freeze.get("record_sha256"):
            checks["freeze_links"] = False
        block = int(row.get("block_number", -1))
        blocks.append(block)
        try:
            path_block = int(record_path.stem.split("=", 1)[1])
        except (IndexError, ValueError):
            path_block = -1
        if path_block != block:
            checks["record_filename_matches_block"] = False
        if block in seen_blocks:
            checks["unique_block_numbers"] = False
        seen_blocks.add(block)
        if block < minimum_block or int(row.get("minimum_eligible_block", -1)) != minimum_block:
            checks["block_not_before_freeze_floor"] = False

        known = pd.Timestamp(row["known_at"])
        captured = pd.Timestamp(row["captured_at"])
        known = known.tz_localize("UTC") if known.tzinfo is None else known.tz_convert("UTC")
        captured = captured.tz_localize("UTC") if captured.tzinfo is None else captured.tz_convert("UTC")
        known_times.append(known)
        if known < frozen_at:
            checks["known_at_not_before_freeze"] = False
        if captured < frozen_at:
            checks["captured_at_not_before_freeze"] = False
        if known < captured:
            checks["known_at_not_before_captured_at"] = False
        if row.get("actionability") != v03.ACTIONABILITY or row.get("risk_multiplier") is not None:
            checks["descriptive_only"] = False

        summary_path = Path(str(row.get("summary_path", "")))
        detail_path = Path(str(row.get("detail_path", "")))
        artifacts_ok = (
            summary_path.exists()
            and detail_path.exists()
            and row.get("summary_sha256") == prospective.sha256_file(summary_path)
            and row.get("detail_sha256") == prospective.sha256_file(detail_path)
        )
        if not artifacts_ok:
            checks["artifact_hash_links"] = False
            continue

        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            detail = pd.read_parquet(detail_path)
        except Exception:
            checks["summary_semantics"] = False
            checks["summary_metric_links"] = False
            checks["detail_unique_addresses"] = False
            checks["detail_candidate_count"] = False
            checks["detail_coverage_recomputed"] = False
            checks["detail_active_debt_recomputed"] = False
            continue

        if (
            summary.get("protocol") != v03.PROTOCOL
            or summary.get("actionability") != v03.ACTIONABILITY
            or summary.get("risk_multiplier") is not None
            or summary.get("bootstrap_complete") is not True
            or summary.get("valid_full_census") is not True
            or int(summary.get("block_number", -1)) != block
            or Path(str(summary.get("detail_path", ""))) != detail_path
            or summary.get("detail_sha256") != row.get("detail_sha256")
        ):
            checks["summary_semantics"] = False

        for metric in LINKED_METRICS:
            if not _numeric_equal(row.get(metric), summary.get(metric)):
                checks["summary_metric_links"] = False

        if "address" not in detail.columns or detail["address"].duplicated().any():
            checks["detail_unique_addresses"] = False
        candidate_count = int(summary.get("candidate_address_count", -1))
        if len(detail) != candidate_count:
            checks["detail_candidate_count"] = False

        if {"success", "total_debt_usd", "health_factor"}.issubset(detail.columns) and candidate_count >= 0:
            success_mask = detail["success"].astype(bool)
            coverage = float(success_mask.sum()) / candidate_count if candidate_count > 0 else 1.0
            if not _numeric_equal(coverage, summary.get("account_call_coverage_ratio")):
                checks["detail_coverage_recomputed"] = False
            successful = detail.loc[success_mask].copy()
            debt = pd.to_numeric(successful["total_debt_usd"], errors="coerce")
            hf = pd.to_numeric(successful["health_factor"], errors="coerce")
            active = successful.loc[(debt > 0) & hf.notna()].copy()
            active_debt = float(pd.to_numeric(active["total_debt_usd"], errors="coerce").fillna(0.0).sum())
            if (
                len(active) != int(summary.get("active_borrower_count", -1))
                or not _numeric_equal(active_debt, summary.get("total_active_debt_usd"))
            ):
                checks["detail_active_debt_recomputed"] = False
        else:
            checks["detail_coverage_recomputed"] = False
            checks["detail_active_debt_recomputed"] = False

    if blocks != sorted(blocks):
        checks["block_numbers_monotonic"] = False

    gap_count = 0
    max_gap_hours = 0.0
    for left, right in zip(sorted(known_times), sorted(known_times)[1:]):
        gap_hours = float((right - left) / pd.Timedelta(hours=1))
        max_gap_hours = max(max_gap_hours, gap_hours)
        if gap_hours > 9.0:
            gap_count += 1

    return {
        "protocol": prospective.PROSPECTIVE_PROTOCOL,
        "audit_level": "STRICT_NON_MUTATING_HASH_GRAPH_WITH_ARTIFACT_RECOMPUTE",
        "frozen": True,
        "ok": all(checks.values()),
        "record_count": len(records),
        "minimum_eligible_block": minimum_block,
        "first_block": min(blocks) if blocks else None,
        "last_block": max(blocks) if blocks else None,
        "first_known_at": min(known_times).isoformat() if known_times else None,
        "last_known_at": max(known_times).isoformat() if known_times else None,
        "gap_count": gap_count,
        "max_gap_hours": max_gap_hours,
        "gaps_are_visible_not_backfilled": True,
        "checks": checks,
        "hash_graph": hash_details,
    }


def strict_state_v03_status(data_root: Path) -> dict[str, Any]:
    integrity = strict_state_v03_integrity_report(data_root)
    if not integrity.get("frozen"):
        return {
            "protocol": prospective.PROSPECTIVE_PROTOCOL,
            "state": "NOT_FROZEN",
            "frozen": False,
            "integrity_ok": False,
        }

    freeze = prospective.load_freeze(data_root)
    records = _load_records(data_root)
    bootstrap = _bootstrap_state(data_root)
    gate = freeze["promotion_gate"]
    if records:
        times = []
        for row in records:
            ts = pd.Timestamp(row["known_at"])
            times.append(ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC"))
        calendar_days = int((max(times) - min(times)) / pd.Timedelta(days=1)) + 1
    else:
        calendar_days = 0
    episodes = _count_stress_episodes(
        records,
        threshold=float(gate["cliff_episode_critical_debt_share_threshold"]),
        cooldown_hours=int(gate["cliff_episode_cooldown_hours"]),
    )
    checks = {
        "minimum_calendar_days": calendar_days >= int(gate["minimum_calendar_days_before_O2_candidate"]),
        "minimum_valid_full_censuses": len(records) >= int(gate["minimum_valid_full_censuses"]),
        "minimum_distinct_cliff_stress_episodes": episodes
        >= int(gate["minimum_distinct_cliff_stress_episodes"]),
        "complete_borrower_bootstrap": bool(bootstrap.get("bootstrap_complete")),
        "ledger_integrity": bool(integrity.get("ok")),
    }
    eligible = all(checks.values())
    if not bootstrap.get("bootstrap_complete"):
        state = "FROZEN_BORROWER_UNIVERSE_BOOTSTRAPPING"
    elif not records:
        state = "FROZEN_AWAITING_FIRST_VALID_FULL_CENSUS"
    elif eligible:
        state = "ELIGIBLE_FOR_O2_PROTOCOL_DESIGN"
    else:
        state = "O1_PROSPECTIVE_BORROWER_EVIDENCE_ACCUMULATING"
    latest = records[-1] if records else None
    return {
        "protocol": prospective.PROSPECTIVE_PROTOCOL,
        "state_protocol": v03.PROTOCOL,
        "state": state,
        "frozen": True,
        "frozen_at": freeze["frozen_at"],
        "minimum_eligible_block": freeze["minimum_eligible_block"],
        "actionability": v03.ACTIONABILITY,
        "risk_multiplier": None,
        "bootstrap_complete": bool(bootstrap.get("bootstrap_complete")),
        "bootstrap_next_block": bootstrap.get("next_block"),
        "candidate_address_count": bootstrap.get("candidate_address_count"),
        "valid_full_census_count": len(records),
        "prospective_calendar_days": calendar_days,
        "cliff_stress_episode_count": episodes,
        "latest_block": latest.get("block_number") if latest else None,
        "latest_total_active_debt_usd": latest.get("total_active_debt_usd") if latest else None,
        "latest_critical_hf_le_1_05_debt_share": latest.get("critical_hf_le_1_05_debt_share") if latest else None,
        "latest_near_cliff_hf_le_1_20_debt_share": latest.get("near_cliff_hf_le_1_20_debt_share") if latest else None,
        "promotion_gate": gate,
        "promotion_volume_checks": checks,
        "outcome_linkage_test_completed": False,
        "predeclared_O2_rule_exists": False,
        "automatic_promotion_to_actionable_modifier_allowed": False,
        "retrospective_backfill_allowed": False,
        "parameter_optimization_allowed": False,
        "integrity_ok": bool(integrity.get("ok")),
        "integrity_audit_level": integrity.get("audit_level"),
        "gap_count": integrity.get("gap_count", 0),
    }
