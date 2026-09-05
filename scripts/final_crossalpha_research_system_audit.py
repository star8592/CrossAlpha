#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from crossalpha.core.free_paper import paper_status
from crossalpha.outcomes.integrity import (
    outcome_linkage_status,
    strict_outcome_linkage_integrity_report,
)
from crossalpha.outcomes.prospective import config_consistency_report as outcome_config_report
from crossalpha.research_catalog import build_research_catalog
from crossalpha.state.ab_integrity import strict_state_ab_status
from crossalpha.state.v02_config import strict_v02_config_consistency_report
from crossalpha.state.v02_integrity import strict_state_v02_status
from crossalpha.state.v03_config import strict_v03_config_report
from crossalpha.state.v03_integrity import strict_state_v03_status
from crossalpha.state.v04 import FUNDING_SEMANTICS
from crossalpha.state.v04_config import strict_v04_config_report
from crossalpha.state.v04_integrity import strict_state_v04_status


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


def _unit_failed(unit: str) -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "is-failed", unit],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() == "failed"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--a-hash-baseline", required=True)
    parser.add_argument("--ab-hash-baseline", required=True)
    parser.add_argument("--v02-hash-expected", required=True)
    parser.add_argument("--v03-hash-expected", required=True)
    parser.add_argument("--v04-hash-expected", required=True)
    parser.add_argument("--outcome-hash-expected", required=True)
    args = parser.parse_args()

    root = Path(args.data_root)
    freezes = {
        "A": root / "research" / "free_v01" / "paper" / "freeze.json",
        "AB": root / "research" / "free_v01" / "state_ab_v01" / "freeze.json",
        "V02": root / "research" / "state_v02" / "freeze.json",
        "V03": root / "research" / "state_v03" / "freeze.json",
        "V04": root / "research" / "state_v04" / "freeze.json",
        "OUTCOME": root / "research" / "outcome_linkage_v01" / "freeze.json",
    }
    hashes = {name: _sha256(path) for name, path in freezes.items()}

    a = paper_status(root)
    ab = strict_state_ab_status(root)
    v02 = strict_state_v02_status(root)
    v03 = strict_state_v03_status(root)
    v04 = strict_state_v04_status(root)
    outcome_integrity = strict_outcome_linkage_integrity_report(root)
    outcome = outcome_linkage_status(root)
    configs = {
        "V02": strict_v02_config_consistency_report(Path("config/state_v02.yaml")),
        "V03": strict_v03_config_report(Path("config/state_v03.yaml")),
        "V04": strict_v04_config_report(Path("config/state_v04.yaml")),
        "OUTCOME": outcome_config_report(),
    }
    catalog = build_research_catalog(root)
    views = set(catalog.get("views", []))

    required_views = {
        "core.free_asset_returns",
        "state_engine.shadow_v01",
        "state_engine.v02",
        "state_engine.v04_venues",
        "state_engine.v04_mechanics",
        "state_engine.v04_prospective",
    }
    v03_bootstrap = root / "research" / "state_v03" / "bootstrap_state.json"
    if v03_bootstrap.exists():
        required_views.add("state_engine.v03_borrower_universe")
    missing_views = sorted(required_views - views)

    units = (
        "crossalpha-observatory.service",
        "crossalpha-materializer.timer",
        "crossalpha-free-paper-daily.timer",
        "crossalpha-free-paper-weekly.timer",
        "crossalpha-state-v02.timer",
        "crossalpha-state-v03.timer",
        "crossalpha-state-v04.timer",
        "crossalpha-outcome-linkage.timer",
    )
    systemd = {unit: _unit_state(unit) for unit in units}
    scheduled_oneshots = (
        "crossalpha-state-v02.service",
        "crossalpha-state-v03.service",
        "crossalpha-state-v04.service",
        "crossalpha-outcome-linkage.service",
    )
    oneshot_failed = {unit: _unit_failed(unit) for unit in scheduled_oneshots}

    v04_health = _load_json(root / "manifests" / "state_v04_cycle_health.json")
    outcome_health = _load_json(root / "manifests" / "outcome_linkage_cycle_health.json")

    checks: dict[str, bool] = {
        "A_freeze_hash_unchanged": hashes["A"] == args.a_hash_baseline,
        "AB_freeze_hash_unchanged": hashes["AB"] == args.ab_hash_baseline,
        "V02_freeze_hash_matches": hashes["V02"] == args.v02_hash_expected,
        "V03_freeze_hash_matches": hashes["V03"] == args.v03_hash_expected,
        "V04_freeze_hash_matches": hashes["V04"] == args.v04_hash_expected,
        "OUTCOME_freeze_hash_matches": hashes["OUTCOME"] == args.outcome_hash_expected,
        "A_integrity_ok": bool(a.get("integrity_ok")),
        "AB_integrity_ok": bool(ab.get("integrity_ok")),
        "V02_integrity_ok": bool(v02.get("integrity_ok")),
        "V03_integrity_ok": bool(v03.get("integrity_ok")),
        "V04_integrity_ok": bool(v04.get("integrity_ok")),
        "OUTCOME_integrity_ok": bool(outcome_integrity.get("ok")),
        "V02_config_ok": bool(configs["V02"].get("ok")),
        "V03_config_ok": bool(configs["V03"].get("ok")),
        "V04_config_and_fault_isolation_hash_ok": bool(configs["V04"].get("ok")),
        "OUTCOME_config_ok": bool(configs["OUTCOME"].get("ok")),
        "V02_descriptive_only": v02.get("actionability") == "DESCRIPTIVE_ONLY"
        and v02.get("risk_multiplier") is None,
        "V03_descriptive_only": v03.get("actionability") == "DESCRIPTIVE_ONLY"
        and v03.get("risk_multiplier") is None,
        "V04_descriptive_only": v04.get("actionability") == "DESCRIPTIVE_ONLY"
        and v04.get("risk_multiplier") is None,
        "V04_settled_funding_semantics": v04.get("funding_semantics") == FUNDING_SEMANTICS,
        "OUTCOME_non_actionable": outcome.get("actionability") == "NONE"
        and outcome.get("risk_multiplier") is None,
        "OUTCOME_selective_linking_disabled": outcome.get("selective_linking_allowed") is False,
        "OUTCOME_same_day_disabled": outcome.get("same_day_outcome_allowed") is False,
        "required_catalog_views_present": not missing_views,
        "V04_live_cycle_health_exists": bool(v04_health),
        "V04_live_cycle_zero_cost": v04_health.get("data_cost_usd") == 0,
        "V04_live_cycle_funding_semantics": v04_health.get("funding_semantics") == FUNDING_SEMANTICS,
        "OUTCOME_cycle_health_exists": bool(outcome_health),
        "OUTCOME_cycle_integrity_ok": outcome_health.get("integrity_ok") is True,
    }
    for unit, state in systemd.items():
        checks[f"systemd_{unit}_active"] = state == "active"
    for unit, failed in oneshot_failed.items():
        checks[f"systemd_{unit}_not_failed"] = not failed

    errors = [name for name, ok in checks.items() if not ok]
    payload = {
        "protocol": "CROSSALPHA_RESEARCH_SYSTEM_FINAL_AUDIT",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "freeze_hashes": hashes,
        "states": {
            "A": a.get("state"),
            "AB": ab.get("state"),
            "V02": v02.get("state"),
            "V03": v03.get("state"),
            "V04": v04.get("state"),
            "OUTCOME": outcome.get("state"),
        },
        "V03_bootstrap_complete": v03.get("bootstrap_complete"),
        "V03_valid_full_census_count": v03.get("valid_full_census_count"),
        "V04_observation_count": v04.get("observation_count"),
        "OUTCOME_link_count": outcome.get("link_count"),
        "OUTCOME_matured_expected_link_count": outcome.get("matured_expected_link_count"),
        "catalog_views": sorted(views),
        "missing_views": missing_views,
        "systemd": systemd,
        "scheduled_oneshot_failed": oneshot_failed,
        "checks": checks,
        "ok": all(checks.values()),
        "errors": errors,
        "research_boundary": (
            "Engineering stack sealed. Future evidence, not parameter tuning, determines whether "
            "State V0.2/V0.3/V0.4 earn a separately preregistered O2 protocol."
        ),
    }
    path = root / "manifests" / "final_crossalpha_research_system_status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    print(f"FINAL_CROSSALPHA_SYSTEM_STATUS_FILE={path}")
    if not payload["ok"]:
        raise SystemExit("CROSSALPHA FINAL RESEARCH SYSTEM AUDIT FAILED")


if __name__ == "__main__":
    main()
