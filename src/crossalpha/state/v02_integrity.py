from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from crossalpha.state import v02, v02_prospective as prospective


def _load_clean_observations(data_root: Path) -> list[dict[str, Any]]:
    root = prospective._research_root(data_root) / "prospective"
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("year=*/month=*/day=*/state_at=*.json")):
        rows.append(json.loads(path.read_text(encoding="utf-8")))
    return rows


def strict_state_v02_integrity_report(data_root: Path) -> dict[str, Any]:
    try:
        freeze = prospective._load_freeze(data_root)
    except FileNotFoundError:
        return {
            "protocol": prospective.PROSPECTIVE_PROTOCOL,
            "frozen": False,
            "ok": False,
            "error": "not_frozen",
        }

    observations = _load_clean_observations(data_root)
    impl_ok, impl_details = prospective._hashes_unchanged(freeze)
    refs_ok, ref_details = prospective._reference_hashes_unchanged(data_root, freeze)
    first_eligible = prospective._utc(freeze["first_eligible_observed_at"])

    checks = {
        "freeze_seal": prospective._verify_sealed(freeze),
        "implementation_hashes_unchanged": impl_ok,
        "v01_reference_hashes_unchanged": refs_ok,
        "observation_seals": True,
        "freeze_links": True,
        "no_pre_freeze_observations": True,
        "descriptive_only": True,
        "derived_state_hash_links": True,
        "generated_at_unique": True,
        "generated_at_monotonic": True,
    }
    generated: list[pd.Timestamp] = []
    seen: set[str] = set()

    for row in observations:
        if not prospective._verify_sealed(row):
            checks["observation_seals"] = False
        if row.get("freeze_record_sha256") != freeze.get("record_sha256"):
            checks["freeze_links"] = False
        ts = prospective._utc(row["generated_at"])
        generated.append(ts)
        key = ts.isoformat()
        if key in seen:
            checks["generated_at_unique"] = False
        seen.add(key)
        if ts < first_eligible:
            checks["no_pre_freeze_observations"] = False
        if row.get("actionability") != v02.ACTIONABILITY or row.get("risk_multiplier") is not None:
            checks["descriptive_only"] = False
        derived = Path(str(row.get("derived_state_path", "")))
        expected_hash = row.get("derived_state_sha256")
        if (
            not derived.exists()
            or expected_hash is None
            or expected_hash != prospective._sha256_file(derived)
        ):
            checks["derived_state_hash_links"] = False

    if generated != sorted(generated):
        checks["generated_at_monotonic"] = False

    ordered = sorted(generated)
    gap_count = 0
    max_gap_minutes = 0.0
    for left, right in zip(ordered, ordered[1:]):
        gap = float((right - left) / pd.Timedelta(minutes=1))
        max_gap_minutes = max(max_gap_minutes, gap)
        if gap > prospective.EXPECTED_CADENCE_MINUTES * 2.25:
            gap_count += 1

    return {
        "protocol": prospective.PROSPECTIVE_PROTOCOL,
        "frozen": True,
        "ok": all(checks.values()),
        "audit_level": "STRICT_NON_MUTATING_HASH_GRAPH",
        "observation_count": len(observations),
        "first_observation_at": ordered[0].isoformat() if ordered else None,
        "last_observation_at": ordered[-1].isoformat() if ordered else None,
        "gap_count": gap_count,
        "max_gap_minutes": max_gap_minutes,
        "gaps_are_visible_not_backfilled": True,
        "checks": checks,
        "implementation_hashes": impl_details,
        "reference_hashes": ref_details,
    }


def strict_state_v02_status(data_root: Path) -> dict[str, Any]:
    integrity = strict_state_v02_integrity_report(data_root)
    if not integrity.get("frozen"):
        return {
            "protocol": prospective.PROSPECTIVE_PROTOCOL,
            "state": "NOT_FROZEN",
            "frozen": False,
            "integrity_ok": False,
        }
    freeze = prospective._load_freeze(data_root)
    observations = _load_clean_observations(data_root)
    gate = freeze["promotion_gate"]

    if observations:
        times = sorted(prospective._utc(row["generated_at"]) for row in observations)
        calendar_days = int((times[-1] - times[0]) / pd.Timedelta(days=1)) + 1
    else:
        calendar_days = 0
    episodes = prospective._count_stress_episodes(
        observations,
        float(gate["stress_episode_score_threshold"]),
        int(gate["stress_episode_cooldown_hours"]),
    )
    volume_checks = {
        "minimum_calendar_days": calendar_days
        >= int(gate["minimum_calendar_days_before_O2_candidate"]),
        "minimum_observations": len(observations) >= int(gate["minimum_observations"]),
        "minimum_distinct_stress_episodes": episodes
        >= int(gate["minimum_distinct_stress_episodes"]),
        "ledger_integrity": bool(integrity.get("ok")),
    }
    data_ready = all(volume_checks.values())
    if not observations:
        state = "FROZEN_AWAITING_FIRST_LIVE_OBSERVATION"
    elif data_ready:
        state = "ELIGIBLE_FOR_O2_PROTOCOL_DESIGN"
    else:
        state = "O1_PROSPECTIVE_EVIDENCE_ACCUMULATING"
    latest = observations[-1] if observations else None

    return {
        "protocol": prospective.PROSPECTIVE_PROTOCOL,
        "state_protocol": v02.PROTOCOL,
        "state": state,
        "frozen": True,
        "frozen_at": freeze["frozen_at"],
        "actionability": v02.ACTIONABILITY,
        "risk_multiplier": None,
        "observation_count": len(observations),
        "prospective_calendar_days": calendar_days,
        "stress_episode_count": episodes,
        "latest_generated_at": latest.get("generated_at") if latest else None,
        "latest_descriptive_stress_score": latest.get("descriptive_stress_score") if latest else None,
        "latest_data_confidence": latest.get("data_confidence") if latest else None,
        "gap_count": integrity.get("gap_count", 0),
        "promotion_gate": gate,
        "promotion_volume_checks": volume_checks,
        "outcome_linkage_test_completed": False,
        "predeclared_O2_rule_exists": False,
        "automatic_promotion_to_actionable_modifier_allowed": False,
        "integrity_ok": bool(integrity.get("ok")),
        "integrity_audit_level": integrity.get("audit_level"),
        "retrospective_backfill_allowed": False,
        "parameter_optimization_allowed": False,
    }
