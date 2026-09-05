from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from crossalpha.core import frozen_b3_v01
from crossalpha.core.free_paper import _load_marks as _load_a_marks
from crossalpha.core.free_paper import _load_snapshots as _load_a_snapshots
from crossalpha.core.free_paper import _parse_date
from crossalpha.state import ab_paper


def strict_state_ab_integrity_report(data_root: Path) -> dict[str, Any]:
    """Audit both directions of the prospective A/B experiment.

    The record-producing implementation stays frozen. This auditor may become
    stricter over time, but it must never repair, rewrite, or backfill either
    ledger. It proves that A and B have the same observation set after B goes
    live and that B differs only through the frozen risk multiplier.
    """
    base = ab_paper.state_ab_integrity_report(data_root)
    if not base.get("frozen"):
        return base

    freeze = ab_paper._load_freeze(data_root)
    decisions = ab_paper._load_decisions(data_root)
    b_snapshots = ab_paper._load_snapshots(data_root)
    b_marks = ab_paper._load_marks(data_root)
    a_snapshots = _load_a_snapshots(data_root)
    a_marks = _load_a_marks(data_root)

    freeze_hash = freeze["record_sha256"]
    first_eligible = _parse_date(freeze["first_eligible_effective_date"])
    decisions_by_date = {row["effective_date"]: row for row in decisions}
    b_snapshots_by_date = {row["effective_date"]: row for row in b_snapshots}
    a_snapshots_by_date = {row["effective_date"]: row for row in a_snapshots}
    b_marks_by_date = {row["date"]: row for row in b_marks}
    a_marks_by_date = {row["date"]: row for row in a_marks}

    eligible_a_snapshot_dates = {
        key for key in a_snapshots_by_date if _parse_date(key) >= first_eligible
    }
    b_snapshot_dates = set(b_snapshots_by_date)

    checks: dict[str, bool] = dict(base.get("checks", {}))
    checks["state_decisions_exactly_match_B_snapshots"] = set(decisions_by_date) == b_snapshot_dates
    checks["eligible_A_snapshots_have_B"] = eligible_a_snapshot_dates == b_snapshot_dates
    checks["all_records_link_to_same_AB_freeze"] = True
    checks["state_decisions_are_shadow_only"] = True
    checks["state_decision_multipliers_frozen"] = True
    checks["B_weights_follow_uniform_frozen_rule"] = True
    checks["B_snapshot_weights_sum_to_one"] = True
    checks["state_decisions_link_to_A_snapshot"] = True

    for decision in decisions:
        if decision.get("ab_freeze_record_sha256") != freeze_hash:
            checks["all_records_link_to_same_AB_freeze"] = False
        state_payload = decision.get("state_payload", {})
        if (
            state_payload.get("shadow_only") is not True
            or state_payload.get("core_protocol_mutated") is not False
        ):
            checks["state_decisions_are_shadow_only"] = False
        try:
            decision_multiplier = float(decision.get("shadow_risk_multiplier", -1.0))
        except (TypeError, ValueError):
            decision_multiplier = -1.0
        if decision_multiplier not in ab_paper.ALLOWED_MULTIPLIERS:
            checks["state_decision_multipliers_frozen"] = False

    for effective, b in b_snapshots_by_date.items():
        if b.get("ab_freeze_record_sha256") != freeze_hash:
            checks["all_records_link_to_same_AB_freeze"] = False
        a = a_snapshots_by_date.get(effective)
        decision = decisions_by_date.get(effective)
        if a is None or decision is None:
            checks["B_weights_follow_uniform_frozen_rule"] = False
            checks["state_decisions_link_to_A_snapshot"] = False
            continue
        if decision.get("a_snapshot_record_sha256") != a.get("record_sha256"):
            checks["state_decisions_link_to_A_snapshot"] = False
        if b.get("state_decision_record_sha256") != decision.get("record_sha256"):
            checks["snapshot_links_to_state_decision"] = False
        try:
            multiplier = float(b.get("shadow_risk_multiplier", -1.0))
        except (TypeError, ValueError):
            multiplier = -1.0
        if multiplier not in ab_paper.ALLOWED_MULTIPLIERS:
            checks["B_weights_follow_uniform_frozen_rule"] = False
            continue
        if abs(multiplier - float(decision.get("shadow_risk_multiplier", -2.0))) > 1e-15:
            checks["B_weights_follow_uniform_frozen_rule"] = False
        a_weights = b.get("a_weights", {})
        b_weights = b.get("b_weights", {})
        for asset in frozen_b3_v01.RISK_ASSETS:
            expected = float(a_weights.get(asset, 0.0)) * multiplier
            observed = float(b_weights.get(asset, 0.0))
            if abs(expected - observed) > 1e-12:
                checks["B_weights_follow_uniform_frozen_rule"] = False
        expected_cash = 1.0 - sum(
            float(b_weights.get(asset, 0.0)) for asset in frozen_b3_v01.RISK_ASSETS
        )
        if abs(float(b_weights.get("CASH", 0.0)) - expected_cash) > 1e-12:
            checks["B_weights_follow_uniform_frozen_rule"] = False
        if abs(
            sum(float(b_weights.get(asset, 0.0)) for asset in frozen_b3_v01.ALL_ASSETS) - 1.0
        ) > 1e-10:
            checks["B_snapshot_weights_sum_to_one"] = False

    if b_snapshots:
        first_b = min(_parse_date(row["effective_date"]) for row in b_snapshots)
        expected_a_mark_dates = {
            key for key in a_marks_by_date if _parse_date(key) >= first_b
        }
        checks["A_marks_have_exactly_one_B_mark"] = expected_a_mark_dates == set(b_marks_by_date)
    else:
        checks["A_marks_have_exactly_one_B_mark"] = not eligible_a_snapshot_dates

    checks["B_marks_reuse_exact_A_asset_returns"] = True
    checks["B_marks_link_same_active_A_snapshot"] = True
    checks["B_marks_copy_exact_A_net_return"] = True
    checks["B_mark_weights_match_active_B_snapshot"] = True
    checks["B_marks_inherit_active_state_decision"] = True

    b_snapshot_by_hash = {row["record_sha256"]: row for row in b_snapshots}
    for mark_date, b in b_marks_by_date.items():
        if b.get("ab_freeze_record_sha256") != freeze_hash:
            checks["all_records_link_to_same_AB_freeze"] = False
        a = a_marks_by_date.get(mark_date)
        active_b = b_snapshot_by_hash.get(b.get("b_snapshot_record_sha256"))
        if a is None or active_b is None:
            checks["B_marks_reuse_exact_A_asset_returns"] = False
            checks["B_marks_link_same_active_A_snapshot"] = False
            checks["B_marks_copy_exact_A_net_return"] = False
            checks["B_mark_weights_match_active_B_snapshot"] = False
            checks["B_marks_inherit_active_state_decision"] = False
            continue
        if b.get("asset_returns") != a.get("asset_returns"):
            checks["B_marks_reuse_exact_A_asset_returns"] = False
        if (
            b.get("a_snapshot_effective_date") != a.get("active_snapshot_effective_date")
            or b.get("a_snapshot_record_sha256") != a.get("active_snapshot_record_sha256")
        ):
            checks["B_marks_link_same_active_A_snapshot"] = False
        if abs(float(b.get("a_net_return", 0.0)) - float(a.get("net_return", 0.0))) > 1e-15:
            checks["B_marks_copy_exact_A_net_return"] = False
        if b.get("state_decision_record_sha256") != active_b.get("state_decision_record_sha256"):
            checks["B_marks_inherit_active_state_decision"] = False
        for asset in frozen_b3_v01.ALL_ASSETS:
            if abs(
                float(b.get("weights", {}).get(asset, 0.0))
                - float(active_b.get("b_weights", {}).get(asset, 0.0))
            ) > 1e-12:
                checks["B_mark_weights_match_active_B_snapshot"] = False

    if b_marks:
        ordered = sorted(_parse_date(key) for key in b_marks_by_date)
        checks["B_mark_calendar_contiguous"] = all(
            right - left == timedelta(days=1) for left, right in zip(ordered, ordered[1:])
        )
    else:
        checks["B_mark_calendar_contiguous"] = True

    ok = all(checks.values())
    return {
        **base,
        "ok": ok,
        "checks": checks,
        "audit_level": "STRICT_BIDIRECTIONAL_HASH_GRAPH",
        "A_snapshot_count": len(a_snapshots),
        "B_snapshot_count": len(b_snapshots),
        "A_mark_count": len(a_marks),
        "B_mark_count": len(b_marks),
        "policy": "NO_RETROSPECTIVE_AB_BACKFILL",
    }


def strict_state_ab_status(data_root: Path) -> dict[str, Any]:
    status = ab_paper.state_ab_status(data_root)
    if not status.get("frozen"):
        return status
    integrity = strict_state_ab_integrity_report(data_root)
    checks = dict(status.get("promotion_gate_checks", {}))
    checks["ledger_integrity"] = bool(integrity.get("ok"))
    promoted = all(checks.values())

    state = status.get("state")
    if not integrity.get("ok"):
        state = "LEDGER_INTEGRITY_FAILED"
    elif status.get("mark_count", 0) > 0 and promoted:
        state = "O2_PROSPECTIVE_RISK_MODIFIER_SUPPORTED"

    return {
        **status,
        "state": state,
        "integrity_ok": bool(integrity.get("ok")),
        "integrity_audit_level": integrity.get("audit_level"),
        "promotion_gate_checks": checks,
    }
