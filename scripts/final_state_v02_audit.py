#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from crossalpha.catalog import build_catalog
from crossalpha.core.free_paper import paper_status
from crossalpha.state.ab_integrity import strict_state_ab_status
from crossalpha.state.shadow import build_latest_shadow_state
from crossalpha.state.v02_integrity import (
    strict_state_v02_integrity_report,
    strict_state_v02_status,
)


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unit_state(unit: str) -> str:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", unit],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() or result.stderr.strip() or f"rc={result.returncode}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--a-hash-baseline", required=True)
    parser.add_argument("--ab-hash-baseline", required=True)
    parser.add_argument("--require-v02-timer", action="store_true")
    args = parser.parse_args()

    root = Path(args.data_root)
    a_freeze = root / "research" / "free_v01" / "paper" / "freeze.json"
    ab_freeze = root / "research" / "free_v01" / "state_ab_v01" / "freeze.json"
    a_hash = _sha256(a_freeze)
    ab_hash = _sha256(ab_freeze)

    errors: list[str] = []
    a = paper_status(root)
    ab = strict_state_ab_status(root)
    v02_integrity = strict_state_v02_integrity_report(root)
    v02 = strict_state_v02_status(root)
    v01_shadow = build_latest_shadow_state(root, write=False)
    catalog = build_catalog(root)
    views = set(catalog.get("views", []))

    required_views = {
        "observatory.aave_markets",
        "state_engine.shadow_v01",
        "state_engine.v02",
        "state_engine.v02_prospective",
        "core.free_asset_returns",
    }
    missing_views = sorted(required_views - views)

    checks: dict[str, bool] = {
        "A_freeze_hash_unchanged": a_hash == args.a_hash_baseline,
        "AB_freeze_hash_unchanged": ab_hash == args.ab_hash_baseline,
        "A_integrity_ok": bool(a.get("integrity_ok")),
        "A_parameter_optimization_disabled": a.get("parameter_optimization_allowed") is False,
        "AB_integrity_ok": bool(ab.get("integrity_ok")),
        "AB_parameter_optimization_disabled": ab.get("parameter_optimization_allowed") is False,
        "AB_backfill_disabled": ab.get("retrospective_backfill_allowed") is False,
        "State_V01_shadow_only": v01_shadow.get("shadow_only") is True,
        "State_V01_core_unmutated": v01_shadow.get("core_protocol_mutated") is False,
        "State_V02_integrity_ok": bool(v02_integrity.get("ok")),
        "State_V02_strict_audit": v02_integrity.get("audit_level") == "STRICT_NON_MUTATING_HASH_GRAPH",
        "State_V02_descriptive_only": v02.get("actionability") == "DESCRIPTIVE_ONLY",
        "State_V02_has_no_risk_multiplier": v02.get("risk_multiplier") is None,
        "State_V02_backfill_disabled": v02.get("retrospective_backfill_allowed") is False,
        "State_V02_parameter_optimization_disabled": v02.get("parameter_optimization_allowed") is False,
        "State_V02_not_auto_actionable": v02.get("automatic_promotion_to_actionable_modifier_allowed") is False,
        "required_catalog_views_present": not missing_views,
    }

    systemd = {
        "crossalpha-observatory.service": _unit_state("crossalpha-observatory.service"),
        "crossalpha-materializer.timer": _unit_state("crossalpha-materializer.timer"),
        "crossalpha-free-paper-daily.timer": _unit_state("crossalpha-free-paper-daily.timer"),
        "crossalpha-free-paper-weekly.timer": _unit_state("crossalpha-free-paper-weekly.timer"),
        "crossalpha-state-v02.timer": _unit_state("crossalpha-state-v02.timer"),
    }
    for unit in (
        "crossalpha-observatory.service",
        "crossalpha-materializer.timer",
        "crossalpha-free-paper-daily.timer",
        "crossalpha-free-paper-weekly.timer",
    ):
        checks[f"systemd_{unit}_active"] = systemd[unit] == "active"
    if args.require_v02_timer:
        checks["systemd_crossalpha-state-v02.timer_active"] = (
            systemd["crossalpha-state-v02.timer"] == "active"
        )

    for name, ok in checks.items():
        if not ok:
            errors.append(name)

    payload: dict[str, Any] = {
        "protocol": "CROSSALPHA_STATE_V0_2_FINAL_AUDIT",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "A_freeze_hash_baseline": args.a_hash_baseline,
        "A_freeze_hash_final": a_hash,
        "AB_freeze_hash_baseline": args.ab_hash_baseline,
        "AB_freeze_hash_final": ab_hash,
        "A_state": a.get("state"),
        "AB_state": ab.get("state"),
        "State_V01_band": v01_shadow.get("state_band"),
        "State_V01_multiplier": v01_shadow.get("shadow_risk_multiplier"),
        "State_V02_state": v02.get("state"),
        "State_V02_observation_count": v02.get("observation_count"),
        "State_V02_latest_stress_score": v02.get("latest_descriptive_stress_score"),
        "State_V02_data_confidence": v02.get("latest_data_confidence"),
        "State_V02_gap_count": v02.get("gap_count"),
        "State_V02_integrity_audit": v02_integrity,
        "catalog_views": sorted(views),
        "missing_views": missing_views,
        "systemd": systemd,
        "checks": checks,
        "ok": all(checks.values()),
        "errors": errors,
    }

    status_path = root / "manifests" / "final_state_v02_system_status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = status_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(status_path)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    print(f"FINAL_STATE_V02_STATUS_FILE={status_path}")
    if not payload["ok"]:
        raise SystemExit("STATE V0.2 FINAL HARD AUDIT FAILED")


if __name__ == "__main__":
    main()
