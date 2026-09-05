from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import duckdb


def _sha(path: Path) -> str:
    if not path.exists():
        return "MISSING"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path, errors: list[str], label: str) -> dict:
    if not path.exists():
        errors.append(f"{label} JSON missing: {path}")
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append(f"{label} JSON unreadable: {type(exc).__name__}: {exc}")
        return {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--a-hash-baseline", required=True)
    parser.add_argument("--ab-hash-baseline", required=True)
    parser.add_argument("--a-status", required=True)
    parser.add_argument("--ab-status", required=True)
    parser.add_argument("--state-status", required=True)
    args = parser.parse_args()

    root = Path(args.data_root)
    a_freeze = root / "research" / "free_v01" / "paper" / "freeze.json"
    ab_freeze = root / "research" / "free_v01" / "state_ab_v01" / "freeze.json"
    errors: list[str] = []
    checks: dict[str, object] = {}

    a_hash_final = _sha(a_freeze)
    ab_hash_final = _sha(ab_freeze)
    checks["A_freeze_hash_baseline"] = args.a_hash_baseline
    checks["A_freeze_hash_final"] = a_hash_final
    checks["A_freeze_hash_unchanged"] = args.a_hash_baseline == a_hash_final != "MISSING"
    checks["AB_freeze_hash_baseline"] = args.ab_hash_baseline
    checks["AB_freeze_hash_final"] = ab_hash_final
    checks["AB_freeze_hash_unchanged"] = args.ab_hash_baseline == ab_hash_final != "MISSING"
    if not checks["A_freeze_hash_unchanged"]:
        errors.append("Frozen B3 freeze hash changed or is missing")
    if not checks["AB_freeze_hash_unchanged"]:
        errors.append("State A/B freeze hash changed or is missing")

    a = _load(Path(args.a_status), errors, "Frozen B3 status")
    ab = _load(Path(args.ab_status), errors, "State A/B status")
    state = _load(Path(args.state_status), errors, "State Shadow status")

    if a:
        checks["A_state"] = a.get("state")
        checks["A_integrity_ok"] = bool(a.get("integrity_ok"))
        checks["A_parameter_optimization_allowed"] = a.get("parameter_optimization_allowed")
        if not a.get("integrity_ok"):
            errors.append("Frozen B3 ledger integrity false")
        if a.get("parameter_optimization_allowed") is not False:
            errors.append("Frozen B3 unexpectedly allows parameter optimization")

    if ab:
        checks["AB_state"] = ab.get("state")
        checks["AB_integrity_ok"] = bool(ab.get("integrity_ok"))
        checks["AB_integrity_audit_level"] = ab.get("integrity_audit_level")
        checks["AB_parameter_optimization_allowed"] = ab.get("parameter_optimization_allowed")
        checks["AB_retrospective_backfill_allowed"] = ab.get("retrospective_backfill_allowed")
        checks["AB_first_eligible_effective_date"] = ab.get("first_eligible_effective_date")
        checks["AB_snapshot_count"] = ab.get("snapshot_count")
        checks["AB_mark_count"] = ab.get("mark_count")
        if not ab.get("integrity_ok"):
            errors.append("State A/B strict ledger integrity false")
        if ab.get("integrity_audit_level") != "STRICT_BIDIRECTIONAL_HASH_GRAPH":
            errors.append("State A/B is not using the authoritative strict hash-graph audit")
        if ab.get("parameter_optimization_allowed") is not False:
            errors.append("State A/B unexpectedly allows parameter optimization")
        if ab.get("retrospective_backfill_allowed") is not False:
            errors.append("State A/B unexpectedly allows retrospective backfill")
        if a and ab.get("first_eligible_effective_date") != a.get("first_eligible_effective_date"):
            errors.append("A/B first eligible date differs from Frozen B3")

    if state:
        checks["state_protocol"] = state.get("protocol")
        checks["state_band"] = state.get("state_band")
        checks["state_multiplier"] = state.get("shadow_risk_multiplier")
        checks["state_data_confidence"] = state.get("data_confidence")
        checks["state_shadow_only"] = state.get("shadow_only")
        checks["state_core_protocol_mutated"] = state.get("core_protocol_mutated")
        if state.get("status") != "no_inputs":
            if state.get("protocol") != "CROSSALPHA_STATE_SHADOW_V0_1":
                errors.append("unexpected State Shadow protocol")
            if state.get("shadow_only") is not True or state.get("core_protocol_mutated") is not False:
                errors.append("State Shadow violates Core isolation")
            try:
                multiplier = float(state.get("shadow_risk_multiplier"))
            except (TypeError, ValueError):
                multiplier = None
            if multiplier not in {0.5, 0.75, 1.0}:
                errors.append(f"invalid State Shadow multiplier: {multiplier!r}")

    first = ab.get("first_eligible_effective_date") if ab else None
    if first:
        today_utc = datetime.now(timezone.utc).date()
        first_date = datetime.fromisoformat(str(first)).date()
        if today_utc < first_date:
            if int(ab.get("snapshot_count", 0) or 0) != 0 or int(ab.get("mark_count", 0) or 0) != 0:
                errors.append("State A/B contains prospective records before first eligible date")

    db = root / "catalog" / "crossalpha.duckdb"
    if not db.exists():
        errors.append(f"catalog missing: {db}")
    else:
        con = duckdb.connect(str(db), read_only=True)
        try:
            rows = con.execute(
                "SELECT table_schema || '.' || table_name FROM information_schema.views"
            ).fetchall()
            present = {row[0] for row in rows}
            required = {
                "core.free_asset_returns",
                "observatory.hyperliquid_market_state",
                "observatory.stablecoin_system_state",
                "state_engine.shadow_v01",
            }
            if int(ab.get("mark_count", 0) or 0) > 0:
                required |= {
                    "core.frozen_b3_paper_marks",
                    "state_engine.shadow_ab_marks",
                }
            missing = sorted(required - present)
            checks["required_views_present"] = not missing
            checks["missing_views"] = missing
            if missing:
                errors.append("missing DuckDB views: " + ", ".join(missing))
        finally:
            con.close()

    required_active = [
        "crossalpha-observatory.service",
        "crossalpha-materializer.timer",
        "crossalpha-free-paper-daily.timer",
        "crossalpha-free-paper-weekly.timer",
    ]
    unit_status: dict[str, str] = {}
    for unit in required_active:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            text=True,
            capture_output=True,
        )
        status = (proc.stdout or proc.stderr).strip()
        unit_status[unit] = status
        if proc.returncode != 0 or status != "active":
            errors.append(f"required systemd unit not active: {unit} ({status})")
    checks["systemd"] = unit_status
    checks["ok"] = not errors
    checks["errors"] = errors

    out = root / "manifests" / "final_state_ab_system_status.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(checks, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(checks, ensure_ascii=False, indent=2, default=str))
    print(f"FINAL_STATE_AB_STATUS_FILE={out}")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
