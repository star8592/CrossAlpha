from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from typing import Any

from crossalpha.outcomes import prospective
from crossalpha.outcomes import linkage
from crossalpha.core.free_paper import _verify_sealed as verify_core_seal
from crossalpha.state.ab_paper import _verify_sealed as verify_ab_seal


def _numeric_equal(left: Any, right: Any, tolerance: float = 1e-12) -> bool:
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


def _load_links(data_root: Path) -> list[dict[str, Any]]:
    root = data_root / "research" / "outcome_linkage_v01" / "links"
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("source=*/year=*/month=*/anchor_date=*/horizon=*d.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        rows.append({**row, "__path": str(path)})
    return rows


def strict_outcome_linkage_integrity_report(data_root: Path) -> dict[str, Any]:
    try:
        freeze = prospective.load_freeze(data_root)
    except FileNotFoundError:
        return {
            "protocol": prospective.PROTOCOL,
            "audit_level": "STRICT_SOURCE_TO_OUTCOME_RECOMPUTE",
            "frozen": False,
            "ok": False,
            "error": "not_frozen",
        }
    hashes_ok, hash_graph = prospective.hashes_unchanged(data_root, freeze)
    sources = linkage._load_source_records(data_root)
    anchors = linkage.select_daily_anchors(sources, not_before=freeze["frozen_at"])
    anchor_map = {
        (row["source_layer"], linkage._source_known_at(row).date()): row for row in anchors
    }
    a_marks, b_marks = linkage._load_marks(data_root)
    links = _load_links(data_root)

    checks = {
        "freeze_seal": prospective.verify_seal(freeze),
        "implementation_and_reference_hashes_unchanged": hashes_ok,
        "link_seals": True,
        "freeze_links": True,
        "daily_anchor_identity": True,
        "source_hash_links": True,
        "same_day_outcome_excluded": True,
        "exact_horizon_dates": True,
        "A_mark_hash_links": True,
        "B_mark_hash_links": True,
        "B_links_same_date_A": True,
        "outcomes_recomputed": True,
        "link_keys_unique": True,
        "all_matured_links_materialized": True,
        "no_unregistered_horizon": True,
        "no_unregistered_source": True,
    }
    seen: set[tuple[str, str, int]] = set()
    link_keys: set[tuple[str, date, int]] = set()

    outcome_metrics = (
        "A_cumulative_net_return",
        "B_cumulative_net_return",
        "B_minus_A_cumulative_return",
        "cash_cumulative_return",
        "A_max_drawdown",
        "B_max_drawdown",
        "A_worst_daily_return",
        "B_worst_daily_return",
        "A_negative_day_count",
        "B_negative_day_count",
        "intervention_day_count",
        "average_B_multiplier",
    )

    for raw in links:
        path = Path(str(raw["__path"]))
        row = dict(raw)
        row.pop("__path", None)
        if not prospective.verify_seal(row):
            checks["link_seals"] = False
        if row.get("freeze_record_sha256") != freeze.get("record_sha256"):
            checks["freeze_links"] = False
        source_layer = str(row.get("source_layer"))
        try:
            anchor_day = date.fromisoformat(str(row.get("anchor_date")))
            horizon = int(row.get("horizon_days"))
        except Exception:
            checks["daily_anchor_identity"] = False
            continue
        key = (source_layer, anchor_day.isoformat(), horizon)
        if key in seen:
            checks["link_keys_unique"] = False
        seen.add(key)
        link_keys.add((source_layer, anchor_day, horizon))
        if source_layer not in prospective.SOURCE_LAYERS:
            checks["no_unregistered_source"] = False
        if horizon not in prospective.HORIZONS_DAYS:
            checks["no_unregistered_horizon"] = False

        anchor = anchor_map.get((source_layer, anchor_day))
        if anchor is None:
            checks["daily_anchor_identity"] = False
            continue
        source_path = Path(str(anchor["__path"]))
        if (
            row.get("source_record_sha256") != anchor.get("record_sha256")
            or row.get("source_record_path") != str(source_path)
            or row.get("source_record_file_sha256") != prospective.sha256_file(source_path)
        ):
            checks["source_hash_links"] = False
        if linkage._source_features(source_layer, anchor) != row.get("source_features"):
            checks["source_hash_links"] = False

        known = linkage._source_known_at(anchor)
        expected_dates = linkage._expected_dates(known, horizon)
        if str(row.get("outcome_start_date")) != expected_dates[0].isoformat() or str(
            row.get("outcome_end_date")
        ) != expected_dates[-1].isoformat():
            checks["exact_horizon_dates"] = False
        if row.get("same_day_outcome_included") is not False or expected_dates[0] <= anchor_day:
            checks["same_day_outcome_excluded"] = False
        if any(day not in a_marks or day not in b_marks for day in expected_dates):
            checks["exact_horizon_dates"] = False
            continue

        for link_row, expected_marks, verify_func, check_name in (
            (row.get("A_mark_links"), a_marks, verify_core_seal, "A_mark_hash_links"),
            (row.get("B_mark_links"), b_marks, verify_ab_seal, "B_mark_hash_links"),
        ):
            if not isinstance(link_row, list) or len(link_row) != horizon:
                checks[check_name] = False
                continue
            for item, day in zip(link_row, expected_dates):
                expected = expected_marks[day]
                mark_path = Path(str(item.get("path", "")))
                if (
                    str(item.get("date")) != day.isoformat()
                    or item.get("record_sha256") != expected.get("record_sha256")
                    or mark_path != Path(str(expected["__path"]))
                    or not mark_path.exists()
                ):
                    checks[check_name] = False
                    continue
                mark = json.loads(mark_path.read_text(encoding="utf-8"))
                if not verify_func(mark):
                    checks[check_name] = False

        for day in expected_dates:
            if b_marks[day].get("a_mark_record_sha256") != a_marks[day].get("record_sha256"):
                checks["B_links_same_date_A"] = False
        recomputed = linkage._outcome_metrics(expected_dates, a_marks, b_marks)
        for metric in outcome_metrics:
            if not _numeric_equal(row.get(metric), recomputed.get(metric)):
                checks["outcomes_recomputed"] = False
        expected_path = linkage._link_path(data_root, source_layer, anchor_day, horizon)
        if expected_path != path:
            checks["link_keys_unique"] = False

    # Selective linking is forbidden: once every exact A/B mark required by a horizon exists,
    # the deterministic link must exist. Delayed materialization is allowed only until maturity.
    matured_count = 0
    for anchor in anchors:
        source_layer = str(anchor["source_layer"])
        known = linkage._source_known_at(anchor)
        anchor_day = known.date()
        for horizon in prospective.HORIZONS_DAYS:
            dates = linkage._expected_dates(known, horizon)
            if all(day in a_marks and day in b_marks for day in dates):
                matured_count += 1
                if (source_layer, anchor_day, horizon) not in link_keys:
                    checks["all_matured_links_materialized"] = False

    return {
        "protocol": prospective.PROTOCOL,
        "audit_level": "STRICT_SOURCE_ANCHOR_AND_A_B_OUTCOME_RECOMPUTE",
        "frozen": True,
        "ok": all(checks.values()),
        "source_record_count": sum(len(values) for values in sources.values()),
        "daily_anchor_count": len(anchors),
        "link_count": len(links),
        "matured_expected_link_count": matured_count,
        "checks": checks,
        "hash_graph": hash_graph,
        "sampling_unit": "one_per_source_per_utc_day",
        "same_day_outcome_allowed": False,
        "selective_linking_allowed": False,
    }


def outcome_linkage_status(data_root: Path) -> dict[str, Any]:
    integrity = strict_outcome_linkage_integrity_report(data_root)
    if not integrity.get("frozen"):
        return {
            "protocol": prospective.PROTOCOL,
            "state": "NOT_FROZEN",
            "frozen": False,
            "integrity_ok": False,
        }
    freeze = prospective.load_freeze(data_root)
    links = _load_links(data_root)
    by_horizon = {h: 0 for h in prospective.HORIZONS_DAYS}
    by_source = {source: 0 for source in prospective.SOURCE_LAYERS}
    for row in links:
        horizon = int(row["horizon_days"])
        source = str(row["source_layer"])
        if horizon in by_horizon:
            by_horizon[horizon] += 1
        if source in by_source:
            by_source[source] += 1
    if not links:
        state = "FROZEN_AWAITING_MATURED_OUTCOMES"
    elif integrity.get("ok"):
        state = "PROSPECTIVE_OUTCOMES_ACCUMULATING"
    else:
        state = "INTEGRITY_FAILURE"
    return {
        "protocol": prospective.PROTOCOL,
        "state": state,
        "frozen": True,
        "frozen_at": freeze["frozen_at"],
        "link_count": len(links),
        "links_by_horizon": by_horizon,
        "links_by_source": by_source,
        "daily_anchor_count": integrity.get("daily_anchor_count"),
        "matured_expected_link_count": integrity.get("matured_expected_link_count"),
        "horizons_days": list(prospective.HORIZONS_DAYS),
        "actionability": "NONE",
        "risk_multiplier": None,
        "parameter_optimization_allowed": False,
        "selective_linking_allowed": False,
        "same_day_outcome_allowed": False,
        "integrity_ok": bool(integrity.get("ok")),
        "integrity_audit_level": integrity.get("audit_level"),
    }
