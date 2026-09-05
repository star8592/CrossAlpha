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
from crossalpha.state.v02_integrity import strict_state_v02_status
from crossalpha.state.v03_integrity import (
    strict_state_v03_integrity_report,
    strict_state_v03_status,
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
    parser.add_argument("--v02-hash-baseline", required=True)
    parser.add_argument("--require-v03-timer", action="store_true")
    args = parser.parse_args()

    root = Path(args.data_root)
    a_freeze = root / "research" / "free_v01" / "paper" / "freeze.json"
    ab_freeze = root / "research" / "free_v01" / "state_ab_v01" / "freeze.json"
    v02_freeze = root / "research" / "state_v02" / "freeze.json"
    v03_freeze = root / "research" / "state_v03" / "freeze.json"

    a_hash = _sha256(a_freeze)
    ab_hash = _sha256(ab_freeze)
    v02_hash = _sha256(v02_freeze)
    v03_hash = _sha256(v03_freeze)

    a = paper_status(root)
    ab = strict_state_ab_status(root)
    v02 = strict_state_v02_status(root)
    v03_integrity = strict_state_v03_integrity_report(root)
    v03 = strict_state_v03_status(root)
    catalog = build_catalog(root)
    views = set(catalog.get("views", []))

    required_views = {
        "state_engine.shadow_v01",
        "state_engine.v02",
        "core.free_asset_returns",
    }
    # The first V0.3 cycle always creates the universe if at least one bootstrap
    # chunk succeeds. Full-census/prospective views are intentionally optional
    # until the historical borrower bootstrap reaches the finalized head.
    bootstrap_state = root / "research" / "state_v03" / "bootstrap_state.json"
    borrower_universe = root / "derived" / "state" / "v03" / "borrower_universe.parquet"
    if bootstrap_state.exists():
        required_views.add("state_engine.v03_borrower_universe")
    missing_views = sorted(required_views - views)

    systemd = {
        "crossalpha-observatory.service": _unit_state("crossalpha-observatory.service"),
        "crossalpha-materializer.timer": _unit_state("crossalpha-materializer.timer"),
        "crossalpha-free-paper-daily.timer": _unit_state("crossalpha-free-paper-daily.timer"),
        "crossalpha-free-paper-weekly.timer": _unit_state("crossalpha-free-paper-weekly.timer"),
        "crossalpha-state-v02.timer": _unit_state("crossalpha-state-v02.timer"),
        "crossalpha-state-v03.timer": _unit_state("crossalpha-state-v03.timer"),
    }

    checks: dict[str, bool] = {
        "A_freeze_hash_unchanged": a_hash == args.a_hash_baseline,
        "AB_freeze_hash_unchanged": ab_hash == args.ab_hash_baseline,
        "V02_freeze_hash_unchanged": v02_hash == args.v02_hash_baseline,
        "V03_freeze_exists": v03_hash is not None,
        "A_integrity_ok": bool(a.get("integrity_ok")),
        "AB_integrity_ok": bool(ab.get("integrity_ok")),
        "V02_integrity_ok": bool(v02.get("integrity_ok")),
        "V03_integrity_ok": bool(v03_integrity.get("ok")),
        "V03_descriptive_only": v03.get("actionability") == "DESCRIPTIVE_ONLY",
        "V03_has_no_risk_multiplier": v03.get("risk_multiplier") is None,
        "V03_backfill_disabled": v03.get("retrospective_backfill_allowed") is False,
        "V03_parameter_optimization_disabled": v03.get("parameter_optimization_allowed") is False,
        "V03_not_auto_actionable": v03.get("automatic_promotion_to_actionable_modifier_allowed") is False,
        "required_catalog_views_present": not missing_views,
        "bootstrap_state_and_universe_consistent": bootstrap_state.exists() == borrower_universe.exists(),
    }
    for unit in (
        "crossalpha-observatory.service",
        "crossalpha-materializer.timer",
        "crossalpha-free-paper-daily.timer",
        "crossalpha-free-paper-weekly.timer",
        "crossalpha-state-v02.timer",
    ):
        checks[f"systemd_{unit}_active"] = systemd[unit] == "active"
    if args.require_v03_timer:
        checks["systemd_crossalpha-state-v03.timer_active"] = (
            systemd["crossalpha-state-v03.timer"] == "active"
        )

    errors = [name for name, ok in checks.items() if not ok]
    payload: dict[str, Any] = {
        "protocol": "CROSSALPHA_STATE_V0_3_FINAL_AUDIT",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "A_freeze_hash_baseline": args.a_hash_baseline,
        "A_freeze_hash_final": a_hash,
        "AB_freeze_hash_baseline": args.ab_hash_baseline,
        "AB_freeze_hash_final": ab_hash,
        "V02_freeze_hash_baseline": args.v02_hash_baseline,
        "V02_freeze_hash_final": v02_hash,
        "V03_freeze_hash": v03_hash,
        "A_state": a.get("state"),
        "AB_state": ab.get("state"),
        "V02_state": v02.get("state"),
        "V03_state": v03.get("state"),
        "V03_bootstrap_complete": v03.get("bootstrap_complete"),
        "V03_candidate_address_count": v03.get("candidate_address_count"),
        "V03_valid_full_census_count": v03.get("valid_full_census_count"),
        "V03_integrity_audit": v03_integrity,
        "catalog_views": sorted(views),
        "missing_views": missing_views,
        "systemd": systemd,
        "checks": checks,
        "ok": all(checks.values()),
        "errors": errors,
    }

    status_path = root / "manifests" / "final_state_v03_system_status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = status_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(status_path)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    print(f"FINAL_STATE_V03_STATUS_FILE={status_path}")
    if not payload["ok"]:
        raise SystemExit("STATE V0.3 FINAL HARD AUDIT FAILED")


if __name__ == "__main__":
    main()
