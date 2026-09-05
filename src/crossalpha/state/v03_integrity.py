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


def _utc(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _numeric_equal(left: Any, right: Any, *, tolerance: float = 1e-12) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        a = float(left)
        b = float(right)
    except (TypeError, ValueError):
        return left == right
    return math.isfinite(a) and math.isfinite(b) and math.isclose(
        a, b, rel_tol=tolerance, abs_tol=tolerance
    )


def _weighted_quantile_from_detail(active: pd.DataFrame, q: float) -> float | None:
    if active.empty:
        return None
    frame = active[["health_factor", "total_debt_usd"]].copy()
    frame["health_factor"] = pd.to_numeric(frame["health_factor"], errors="coerce")
    frame["total_debt_usd"] = pd.to_numeric(frame["total_debt_usd"], errors="coerce")
    frame = frame.dropna()
    frame = frame.loc[frame["total_debt_usd"] > 0].sort_values("health_factor")
    if frame.empty:
        return None
    total = float(frame["total_debt_usd"].sum())
    if total <= 0:
        return None
    target = total * float(q)
    cumulative = frame["total_debt_usd"].cumsum()
    index = cumulative.searchsorted(target, side="left")
    index = min(int(index), len(frame) - 1)
    return float(frame.iloc[index]["health_factor"])


def _recompute_distribution(detail: pd.DataFrame) -> dict[str, Any] | None:
    required = {"success", "total_debt_usd", "health_factor"}
    if not required.issubset(detail.columns):
        return None
    success = detail.loc[detail["success"].astype(bool)].copy()
    success["total_debt_usd"] = pd.to_numeric(success["total_debt_usd"], errors="coerce")
    success["health_factor"] = pd.to_numeric(success["health_factor"], errors="coerce")
    active = success.loc[
        (success["total_debt_usd"].fillna(0.0) > 0) & success["health_factor"].notna()
    ].copy()
    total_debt = float(active["total_debt_usd"].fillna(0.0).sum())

    def debt_share(threshold: float) -> float:
        debt = float(
            active.loc[active["health_factor"] <= threshold, "total_debt_usd"]
            .fillna(0.0)
            .sum()
        )
        return debt / total_debt if total_debt > 0 else 0.0

    watchlist = active.loc[
        (active["health_factor"] <= v03.CensusPolicy().watchlist_health_factor_max)
        | (active["total_debt_usd"] >= v03.CensusPolicy().watchlist_debt_usd_min)
    ]
    return {
        "active_borrower_count": int(len(active)),
        "total_active_debt_usd": total_debt,
        "debt_weighted_hf_p10": _weighted_quantile_from_detail(active, 0.10),
        "debt_weighted_hf_p25": _weighted_quantile_from_detail(active, 0.25),
        "debt_weighted_hf_p50": _weighted_quantile_from_detail(active, 0.50),
        "liquidatable_debt_share": debt_share(1.00),
        "critical_hf_le_1_05_debt_share": debt_share(1.05),
        "near_cliff_hf_le_1_20_debt_share": debt_share(1.20),
        "watchlist_count": int(len(watchlist)),
    }


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
            qualifying.append(_utc(row["known_at"]))
    qualifying = sorted(qualifying)
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
    frozen_at = _utc(freeze["frozen_at"])
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
        "block_time_not_after_captured_at": True,
        "known_at_not_before_captured_at": True,
        "descriptive_only": True,
        "artifact_hash_links": True,
        "summary_semantics": True,
        "summary_event_time_link": True,
        "summary_metric_links": True,
        "detail_unique_addresses": True,
        "detail_candidate_count": True,
        "detail_coverage_recomputed": True,
        "detail_active_debt_recomputed": True,
        "detail_cliff_metrics_recomputed": True,
        "detail_hf_quantiles_recomputed": True,
        "unique_block_numbers": True,
        "block_numbers_monotonic": True,
        "block_times_monotonic": True,
    }

    blocks: list[int] = []
    block_times: list[pd.Timestamp] = []
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

        try:
            known = _utc(row["known_at"])
            captured = _utc(row["captured_at"])
            block_time = _utc(row["block_time"])
        except Exception:
            checks["block_time_not_after_captured_at"] = False
            checks["known_at_not_before_captured_at"] = False
            checks["summary_event_time_link"] = False
            continue
        known_times.append(known)
        block_times.append(block_time)
        if known < frozen_at:
            checks["known_at_not_before_freeze"] = False
        if captured < frozen_at:
            checks["captured_at_not_before_freeze"] = False
        if block_time > captured:
            checks["block_time_not_after_captured_at"] = False
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
            for name in (
                "summary_semantics",
                "summary_event_time_link",
                "summary_metric_links",
                "detail_unique_addresses",
                "detail_candidate_count",
                "detail_coverage_recomputed",
                "detail_active_debt_recomputed",
                "detail_cliff_metrics_recomputed",
                "detail_hf_quantiles_recomputed",
            ):
                checks[name] = False
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
        try:
            if _utc(summary.get("block_time")) != block_time:
                checks["summary_event_time_link"] = False
        except Exception:
            checks["summary_event_time_link"] = False

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
            recomputed = _recompute_distribution(detail)
            if recomputed is None:
                checks["detail_active_debt_recomputed"] = False
                checks["detail_cliff_metrics_recomputed"] = False
                checks["detail_hf_quantiles_recomputed"] = False
            else:
                for metric in ("active_borrower_count", "total_active_debt_usd"):
                    if not _numeric_equal(recomputed.get(metric), summary.get(metric)):
                        checks["detail_active_debt_recomputed"] = False
                for metric in (
                    "liquidatable_debt_share",
                    "critical_hf_le_1_05_debt_share",
                    "near_cliff_hf_le_1_20_debt_share",
                    "watchlist_count",
                ):
                    if not _numeric_equal(recomputed.get(metric), summary.get(metric)):
                        checks["detail_cliff_metrics_recomputed"] = False
                for metric in (
                    "debt_weighted_hf_p10",
                    "debt_weighted_hf_p25",
                    "debt_weighted_hf_p50",
                ):
                    if not _numeric_equal(recomputed.get(metric), summary.get(metric)):
                        checks["detail_hf_quantiles_recomputed"] = False
        else:
            checks["detail_coverage_recomputed"] = False
            checks["detail_active_debt_recomputed"] = False
            checks["detail_cliff_metrics_recomputed"] = False
            checks["detail_hf_quantiles_recomputed"] = False

    if blocks != sorted(blocks):
        checks["block_numbers_monotonic"] = False
    if block_times != sorted(block_times):
        checks["block_times_monotonic"] = False

    gap_count = 0
    max_gap_hours = 0.0
    for left, right in zip(sorted(known_times), sorted(known_times)[1:]):
        gap_hours = float((right - left) / pd.Timedelta(hours=1))
        max_gap_hours = max(max_gap_hours, gap_hours)
        if gap_hours > 9.0:
            gap_count += 1

    return {
        "protocol": prospective.PROSPECTIVE_PROTOCOL,
        "audit_level": "STRICT_HASH_GRAPH_EVENT_TIME_AND_DETAIL_RECOMPUTE",
        "frozen": True,
        "ok": all(checks.values()),
        "record_count": len(records),
        "minimum_eligible_block": minimum_block,
        "first_block": min(blocks) if blocks else None,
        "last_block": max(blocks) if blocks else None,
        "first_block_time": min(block_times).isoformat() if block_times else None,
        "last_block_time": max(block_times).isoformat() if block_times else None,
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
        times = [_utc(row["known_at"]) for row in records]
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
        "pending_new_borrower_count": len(bootstrap.get("pending_new_borrowers_since_full", [])),
        "valid_full_census_count": len(records),
        "prospective_calendar_days": calendar_days,
        "cliff_stress_episode_count": episodes,
        "latest_block": latest.get("block_number") if latest else None,
        "latest_block_time": latest.get("block_time") if latest else None,
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
