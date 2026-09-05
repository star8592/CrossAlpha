from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from crossalpha.state import v04
from crossalpha.state import v04_prospective as prospective


ASSET_METRICS = (
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
)


def _utc(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _numeric_equal(left: Any, right: Any, tolerance: float = 1e-10) -> bool:
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


def _load_records(data_root: Path) -> list[dict[str, Any]]:
    root = data_root / "research" / "state_v04" / "prospective"
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("year=*/month=*/day=*/state_at=*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        row["__path"] = str(path)
        rows.append(row)
    return sorted(rows, key=lambda row: _utc(row.get("generated_at")))


def _asset_metrics_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left.get("data_confidence") != right.get("data_confidence"):
        return False
    if sorted(left.get("valid_venues", [])) != sorted(right.get("valid_venues", [])):
        return False
    for metric in ASSET_METRICS:
        if not _numeric_equal(left.get(metric), right.get(metric)):
            return False
    return True


def strict_state_v04_integrity_report(data_root: Path) -> dict[str, Any]:
    try:
        freeze = prospective.load_freeze(data_root)
    except FileNotFoundError:
        return {
            "protocol": prospective.PROSPECTIVE_PROTOCOL,
            "audit_level": "STRICT_RAW_TO_VECTOR_RECOMPUTE",
            "frozen": False,
            "ok": False,
            "error": "not_frozen",
        }
    records = _load_records(data_root)
    hashes_ok, hash_graph = prospective.hashes_unchanged(data_root, freeze)
    frozen_at = _utc(freeze["frozen_at"])
    checks = {
        "freeze_seal": prospective.verify_seal(freeze),
        "implementation_and_reference_hashes_unchanged": hashes_ok,
        "record_seals": True,
        "freeze_links": True,
        "generated_at_not_before_freeze": True,
        "known_at_not_before_generated_at": True,
        "descriptive_only": True,
        "artifact_hash_links": True,
        "raw_payload_hash_links": True,
        "raw_compressed_file_hash_links": True,
        "venue_universe_exact": True,
        "venue_pti_ordering": True,
        "settled_funding_semantics": True,
        "mechanics_recomputed": True,
        "generated_at_unique": True,
        "generated_at_monotonic": True,
    }
    generated_times: list[pd.Timestamp] = []
    seen: set[str] = set()
    valid_venue_slots = 0
    total_venue_slots = 0

    for raw_record in records:
        row = dict(raw_record)
        row.pop("__path", None)
        if not prospective.verify_seal(row):
            checks["record_seals"] = False
        if row.get("freeze_record_sha256") != freeze.get("record_sha256"):
            checks["freeze_links"] = False
        try:
            generated = _utc(row["generated_at"])
            known = _utc(row["known_at"])
        except Exception:
            checks["generated_at_not_before_freeze"] = False
            checks["known_at_not_before_generated_at"] = False
            continue
        generated_times.append(generated)
        key = generated.isoformat()
        if key in seen:
            checks["generated_at_unique"] = False
        seen.add(key)
        if generated < frozen_at:
            checks["generated_at_not_before_freeze"] = False
        if known < generated:
            checks["known_at_not_before_generated_at"] = False
        if (
            row.get("actionability") != v04.ACTIONABILITY
            or row.get("risk_multiplier") is not None
            or row.get("no_composite_stress_score") is not True
        ):
            checks["descriptive_only"] = False

        mechanics_path = Path(str(row.get("mechanics_path", "")))
        venue_path = Path(str(row.get("venue_snapshot_path", "")))
        if (
            not mechanics_path.exists()
            or not venue_path.exists()
            or row.get("mechanics_sha256") != prospective.sha256_file(mechanics_path)
            or row.get("venue_snapshot_sha256") != prospective.sha256_file(venue_path)
        ):
            checks["artifact_hash_links"] = False
            continue
        raw_links = row.get("raw_links")
        if not isinstance(raw_links, list) or len(raw_links) != 6:
            checks["raw_payload_hash_links"] = False
            checks["raw_compressed_file_hash_links"] = False
        else:
            for link in raw_links:
                path = Path(str(link.get("raw_path", "")))
                if not path.exists():
                    checks["raw_payload_hash_links"] = False
                    checks["raw_compressed_file_hash_links"] = False
                    continue
                if link.get("raw_sha256") != prospective.sha256_gzip_payload(path):
                    checks["raw_payload_hash_links"] = False
                if (
                    link.get("raw_compressed_file_sha256")
                    != prospective.sha256_file(path)
                ):
                    checks["raw_compressed_file_hash_links"] = False

        try:
            mechanics = json.loads(mechanics_path.read_text(encoding="utf-8"))
            venues = pd.read_parquet(venue_path)
        except Exception:
            checks["mechanics_recomputed"] = False
            checks["venue_universe_exact"] = False
            continue
        pairs = set(zip(venues.get("asset", []), venues.get("venue", [])))
        expected_pairs = {(asset, venue) for asset in v04.ASSETS for venue in v04.VENUES}
        if len(venues) != 6 or pairs != expected_pairs or venues.duplicated(["asset", "venue"]).any():
            checks["venue_universe_exact"] = False
        try:
            observed = pd.to_datetime(venues["observed_at"], utc=True, errors="coerce")
            source_known = pd.to_datetime(venues["known_at"], utc=True, errors="coerce")
            if observed.isna().any() or source_known.isna().any():
                checks["venue_pti_ordering"] = False
            elif (observed > source_known).any() or (source_known > generated).any():
                checks["venue_pti_ordering"] = False
        except Exception:
            checks["venue_pti_ordering"] = False

        required_funding_columns = {
            "funding_semantics",
            "funding_rate_settled_raw",
            "funding_settlement_time",
            "funding_interval_hours",
            "funding_rate_8h",
        }
        if not required_funding_columns.issubset(venues.columns):
            checks["settled_funding_semantics"] = False
        else:
            semantics = venues["funding_semantics"].astype(str)
            if not semantics.eq(v04.FUNDING_SEMANTICS).all():
                checks["settled_funding_semantics"] = False
            intervals = pd.to_numeric(venues["funding_interval_hours"], errors="coerce")
            normalized = pd.to_numeric(venues["funding_rate_8h"], errors="coerce")
            settled_raw = pd.to_numeric(venues["funding_rate_settled_raw"], errors="coerce")
            comparable = normalized.notna()
            if (
                (comparable & ~intervals.gt(0)).any()
                or (comparable & settled_raw.isna()).any()
                or (comparable & venues["funding_settlement_time"].isna()).any()
            ):
                checks["settled_funding_semantics"] = False

        recomputed = v04.compute_market_mechanics(venues, generated_at=generated)
        if (
            mechanics.get("protocol") != v04.PROTOCOL
            or mechanics.get("generated_at") != generated.isoformat()
            or mechanics.get("funding_semantics") != v04.FUNDING_SEMANTICS
            or mechanics.get("no_composite_stress_score") is not True
            or mechanics.get("data_confidence") != recomputed.get("data_confidence")
        ):
            checks["mechanics_recomputed"] = False
        for asset in v04.ASSETS:
            left = mechanics.get("assets", {}).get(asset, {})
            right = recomputed.get("assets", {}).get(asset, {})
            if not _asset_metrics_match(left, right):
                checks["mechanics_recomputed"] = False
            count = int(right.get("valid_venue_count", 0) or 0)
            valid_venue_slots += min(max(count, 0), len(v04.VENUES))
            total_venue_slots += len(v04.VENUES)
        if row.get("data_confidence") != mechanics.get("data_confidence"):
            checks["mechanics_recomputed"] = False
        for asset in v04.ASSETS:
            if not _asset_metrics_match(
                row.get("assets", {}).get(asset, {}),
                mechanics.get("assets", {}).get(asset, {}),
            ):
                checks["mechanics_recomputed"] = False

    if generated_times != sorted(generated_times):
        checks["generated_at_monotonic"] = False
    venue_share = valid_venue_slots / total_venue_slots if total_venue_slots else 0.0
    gap_count = 0
    max_gap_minutes = 0.0
    for left, right in zip(sorted(generated_times), sorted(generated_times)[1:]):
        gap = float((right - left) / pd.Timedelta(minutes=1))
        max_gap_minutes = max(max_gap_minutes, gap)
        if gap > 11.25:
            gap_count += 1

    return {
        "protocol": prospective.PROSPECTIVE_PROTOCOL,
        "audit_level": "STRICT_RAW_PAYLOAD_AND_COMPRESSED_TO_VECTOR_RECOMPUTE",
        "frozen": True,
        "ok": all(checks.values()),
        "observation_count": len(records),
        "first_generated_at": min(generated_times).isoformat() if generated_times else None,
        "last_generated_at": max(generated_times).isoformat() if generated_times else None,
        "valid_venue_share": venue_share,
        "gap_count": gap_count,
        "max_gap_minutes": max_gap_minutes,
        "gaps_are_visible_not_backfilled": True,
        "checks": checks,
        "hash_graph": hash_graph,
    }


def strict_state_v04_status(data_root: Path) -> dict[str, Any]:
    integrity = strict_state_v04_integrity_report(data_root)
    if not integrity.get("frozen"):
        return {
            "protocol": prospective.PROSPECTIVE_PROTOCOL,
            "state": "NOT_FROZEN",
            "frozen": False,
            "integrity_ok": False,
        }
    freeze = prospective.load_freeze(data_root)
    records = _load_records(data_root)
    gate = freeze["promotion_gate"]
    if records:
        times = [_utc(row["generated_at"]) for row in records]
        days = int((max(times) - min(times)) / pd.Timedelta(days=1)) + 1
    else:
        days = 0
    volume_checks = {
        "minimum_calendar_days": days >= int(gate["minimum_calendar_days_before_O2_candidate"]),
        "minimum_observations": len(records) >= int(gate["minimum_observations"]),
        "minimum_valid_venue_share": float(integrity.get("valid_venue_share", 0.0))
        >= float(gate["minimum_valid_venue_share"]),
        "ledger_integrity": bool(integrity.get("ok")),
    }
    eligible = all(volume_checks.values())
    if not records:
        state = "FROZEN_AWAITING_FIRST_LIVE_OBSERVATION"
    elif eligible:
        state = "ELIGIBLE_FOR_O2_PROTOCOL_DESIGN"
    else:
        state = "O1_PROSPECTIVE_MARKET_MECHANICS_EVIDENCE_ACCUMULATING"
    latest = records[-1] if records else None
    return {
        "protocol": prospective.PROSPECTIVE_PROTOCOL,
        "state_protocol": v04.PROTOCOL,
        "state": state,
        "frozen": True,
        "frozen_at": freeze["frozen_at"],
        "actionability": v04.ACTIONABILITY,
        "risk_multiplier": None,
        "funding_semantics": v04.FUNDING_SEMANTICS,
        "no_composite_stress_score": True,
        "observation_count": len(records),
        "prospective_calendar_days": days,
        "valid_venue_share": integrity.get("valid_venue_share"),
        "latest_generated_at": latest.get("generated_at") if latest else None,
        "latest_data_confidence": latest.get("data_confidence") if latest else None,
        "promotion_gate": gate,
        "promotion_volume_checks": volume_checks,
        "outcome_linkage_test_completed": False,
        "predeclared_O2_rule_exists": False,
        "automatic_promotion_to_actionable_modifier_allowed": False,
        "retrospective_backfill_allowed": False,
        "parameter_optimization_allowed": False,
        "integrity_ok": bool(integrity.get("ok")),
        "integrity_audit_level": integrity.get("audit_level"),
        "gap_count": integrity.get("gap_count", 0),
    }
