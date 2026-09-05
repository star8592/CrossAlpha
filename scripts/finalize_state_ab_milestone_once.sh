#!/usr/bin/env bash
# Final one-shot acceptance for Frozen B3 + State Shadow + prospective A/B.
# It runs exactly one full pytest suite and never reruns historical research.
# It always exits 0 so an interactive parent terminal is never closed; inspect
# FINAL_RESULT in the summary for the real acceptance result.

set -u -o pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="/tmp/crossalpha_state_ab_finalize_${STAMP}.log"
LATEST="/tmp/crossalpha_state_ab_finalize_latest.log"
SUMMARY="/tmp/crossalpha_state_ab_finalize_summary.json"
ln -sfn "$LOG" "$LATEST"
exec > >(tee -a "$LOG") 2>&1

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

DATA_ROOT="${CROSSALPHA_DATA_DIR:-/mnt/disk2/CrossAlphaData}"
A_FREEZE="$DATA_ROOT/research/free_v01/paper/freeze.json"
AB_FREEZE="$DATA_ROOT/research/free_v01/state_ab_v01/freeze.json"
A_STATUS_JSON="/tmp/crossalpha_state_ab_A_status.json"
AB_STATUS_JSON="/tmp/crossalpha_state_ab_B_status.json"
STATE_JSON="/tmp/crossalpha_state_ab_state.json"

FAILURES=()
WARNINGS=()

run_critical() {
  local name="$1"
  shift
  echo
  echo "=================================================="
  echo "$name"
  echo "=================================================="
  "$@"
  local rc=$?
  echo "EXIT_CODE=$rc"
  if [[ $rc -ne 0 ]]; then
    FAILURES+=("$name::$rc")
  fi
  return 0
}

run_warning() {
  local name="$1"
  shift
  echo
  echo "--------------------------------------------------"
  echo "$name"
  echo "--------------------------------------------------"
  "$@"
  local rc=$?
  echo "EXIT_CODE=$rc"
  if [[ $rc -ne 0 ]]; then
    WARNINGS+=("$name::$rc")
  fi
  return 0
}

sha_or_missing() {
  local path="$1"
  if [[ -f "$path" ]]; then
    sha256sum "$path" | awk '{print $1}'
  else
    echo "MISSING"
  fi
}

A_HASH_AT_START="$(sha_or_missing "$A_FREEZE")"
AB_HASH_AT_START="$(sha_or_missing "$AB_FREEZE")"
A_HASH_BASELINE="$A_HASH_AT_START"
AB_HASH_BASELINE="$AB_HASH_AT_START"
TODAY_UTC="$(date -u +%F)"

echo "CrossAlpha State A/B milestone finalizer"
echo "repo=$REPO_DIR"
echo "data_root=$DATA_ROOT"
echo "today_utc=$TODAY_UTC"
echo "log=$LOG"
echo "A_hash_at_start=$A_HASH_AT_START"
echo "AB_hash_at_start=$AB_HASH_AT_START"
python --version || true
git rev-parse --short HEAD || true

# ---------------------------------------------------------------------------
# A. Code validation: exactly one full pytest run.
# ---------------------------------------------------------------------------
run_critical "A1. editable install" python -m pip install -e ".[dev]"
run_critical "A2. Python compile check" python -m compileall -q src scripts
run_critical "A3. full pytest suite (single run)" pytest -q

if [[ ${#FAILURES[@]} -eq 0 ]]; then
  # -------------------------------------------------------------------------
  # B. Establish immutable A and A/B freezes before service activation.
  # -------------------------------------------------------------------------
  run_critical "B1. free Core credential/status gate" crossalpha free-core-status
  run_critical "B2. ensure Frozen B3 paper freeze" \
    crossalpha-free-paper-freeze --historical-start 2010-06-01 --historical-end 2026-09-01
  if [[ "$A_HASH_BASELINE" == "MISSING" ]]; then
    A_HASH_BASELINE="$(sha_or_missing "$A_FREEZE")"
  fi

  run_critical "B3. ensure prospective State A/B freeze" crossalpha-state-ab-freeze
  if [[ "$AB_HASH_BASELINE" == "MISSING" ]]; then
    AB_HASH_BASELINE="$(sha_or_missing "$AB_FREEZE")"
  fi

  run_critical "B4. Frozen B3 ledger integrity" python scripts/check_free_paper_integrity.py
  run_critical "B5. strict State A/B ledger integrity" python scripts/check_state_ab_integrity.py

  # -------------------------------------------------------------------------
  # C. Idempotent service installation. Existing A timers are upgraded to also
  #    seal B records in the same run; no extra timers are introduced.
  # -------------------------------------------------------------------------
  run_critical "C1. install Observatory + State materializer" bash scripts/install_all_user_services.sh
  run_critical "C2. install Frozen B3 + State A/B paper services" bash scripts/install_free_paper_user_services.sh

  # -------------------------------------------------------------------------
  # D. Current State/Observatory materialization and catalog visibility.
  # -------------------------------------------------------------------------
  run_critical "D1. materialize Observatory + State Shadow once" python scripts/materialize_observatory_and_state.py
  run_critical "D2. compute current State Shadow without write" bash -lc \
    "source .venv/bin/activate && crossalpha-state-shadow --no-write > '$STATE_JSON' && cat '$STATE_JSON'"
  run_critical "D3. rebuild DuckDB catalog" crossalpha build-catalog
  run_warning "D4. Observatory live health" crossalpha observatory-live-health

  # -------------------------------------------------------------------------
  # E. Final authoritative A and A/B status.
  # -------------------------------------------------------------------------
  run_critical "E1. final Frozen B3 status" bash -lc \
    "source .venv/bin/activate && crossalpha-free-paper-status > '$A_STATUS_JSON' && cat '$A_STATUS_JSON'"
  run_critical "E2. final strict State A/B status" bash -lc \
    "source .venv/bin/activate && crossalpha-state-ab-status > '$AB_STATUS_JSON' && cat '$AB_STATUS_JSON'"
  run_critical "E3. final Frozen B3 integrity" python scripts/check_free_paper_integrity.py
  run_critical "E4. final strict State A/B integrity" python scripts/check_state_ab_integrity.py

  # -------------------------------------------------------------------------
  # F. Hard machine-readable invariant audit.
  # -------------------------------------------------------------------------
  run_critical "F1. final hard invariant audit" python - \
    "$DATA_ROOT" "$A_HASH_BASELINE" "$AB_HASH_BASELINE" \
    "$A_STATUS_JSON" "$AB_STATUS_JSON" "$STATE_JSON" <<'PY'
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import duckdb

root = Path(sys.argv[1])
a_hash_baseline = sys.argv[2]
ab_hash_baseline = sys.argv[3]
a_status_path = Path(sys.argv[4])
ab_status_path = Path(sys.argv[5])
state_path = Path(sys.argv[6])

a_freeze = root / "research" / "free_v01" / "paper" / "freeze.json"
ab_freeze = root / "research" / "free_v01" / "state_ab_v01" / "freeze.json"
errors: list[str] = []
checks: dict[str, object] = {}


def sha(path: Path) -> str:
    if not path.exists():
        return "MISSING"
    return hashlib.sha256(path.read_bytes()).hexdigest()


a_hash_final = sha(a_freeze)
ab_hash_final = sha(ab_freeze)
checks["A_freeze_hash_baseline"] = a_hash_baseline
checks["A_freeze_hash_final"] = a_hash_final
checks["A_freeze_hash_unchanged"] = a_hash_baseline == a_hash_final != "MISSING"
checks["AB_freeze_hash_baseline"] = ab_hash_baseline
checks["AB_freeze_hash_final"] = ab_hash_final
checks["AB_freeze_hash_unchanged"] = ab_hash_baseline == ab_hash_final != "MISSING"
if not checks["A_freeze_hash_unchanged"]:
    errors.append("Frozen B3 freeze hash changed or is missing")
if not checks["AB_freeze_hash_unchanged"]:
    errors.append("State A/B freeze hash changed or is missing")

if not a_status_path.exists():
    errors.append("Frozen B3 status JSON missing")
    a = {}
else:
    a = json.loads(a_status_path.read_text(encoding="utf-8"))
    checks["A_state"] = a.get("state")
    checks["A_integrity_ok"] = bool(a.get("integrity_ok"))
    checks["A_parameter_optimization_allowed"] = a.get("parameter_optimization_allowed")
    if not a.get("integrity_ok"):
        errors.append("Frozen B3 ledger integrity false")
    if a.get("parameter_optimization_allowed") is not False:
        errors.append("Frozen B3 unexpectedly allows parameter optimization")

if not ab_status_path.exists():
    errors.append("State A/B status JSON missing")
    ab = {}
else:
    ab = json.loads(ab_status_path.read_text(encoding="utf-8"))
    checks["AB_state"] = ab.get("state")
    checks["AB_integrity_ok"] = bool(ab.get("integrity_ok"))
    checks["AB_parameter_optimization_allowed"] = ab.get("parameter_optimization_allowed")
    checks["AB_retrospective_backfill_allowed"] = ab.get("retrospective_backfill_allowed")
    checks["AB_first_eligible_effective_date"] = ab.get("first_eligible_effective_date")
    checks["AB_snapshot_count"] = ab.get("snapshot_count")
    checks["AB_mark_count"] = ab.get("mark_count")
    if not ab.get("integrity_ok"):
        errors.append("State A/B strict ledger integrity false")
    if ab.get("parameter_optimization_allowed") is not False:
        errors.append("State A/B unexpectedly allows parameter optimization")
    if ab.get("retrospective_backfill_allowed") is not False:
        errors.append("State A/B unexpectedly allows retrospective backfill")
    if a and ab.get("first_eligible_effective_date") != a.get("first_eligible_effective_date"):
        errors.append("A/B first eligible date does not equal Frozen B3 first eligible date")

if not state_path.exists():
    errors.append("current State Shadow JSON missing")
    state = {}
else:
    state = json.loads(state_path.read_text(encoding="utf-8"))
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

# Before the first eligible Monday, prospective records must still be empty.
first = ab.get("first_eligible_effective_date") if ab else None
if first:
    today = date.today()
    first_date = date.fromisoformat(first)
    if today < first_date:
        if int(ab.get("snapshot_count", 0)) != 0 or int(ab.get("mark_count", 0)) != 0:
            errors.append("State A/B contains records before first eligible date")

# DuckDB: base views must exist now. Prospective A/B views become mandatory as
# soon as marks exist.
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
PY
fi

# Always display operational context without changing the acceptance result.
run_warning "G1. all CrossAlpha timers" bash -lc 'systemctl --user list-timers --all | grep crossalpha || true'
run_warning "G2. collector status" bash -lc 'systemctl --user status crossalpha-observatory.service --no-pager || true'
run_warning "G3. materializer timer status" bash -lc 'systemctl --user status crossalpha-materializer.timer --no-pager || true'
run_warning "G4. paper daily timer status" bash -lc 'systemctl --user status crossalpha-free-paper-daily.timer --no-pager || true'
run_warning "G5. paper weekly timer status" bash -lc 'systemctl --user status crossalpha-free-paper-weekly.timer --no-pager || true'

A_HASH_FINAL="$(sha_or_missing "$A_FREEZE")"
AB_HASH_FINAL="$(sha_or_missing "$AB_FREEZE")"

export CROSSALPHA_FINAL_FAILURES="$(printf '%s\n' "${FAILURES[@]-}")"
export CROSSALPHA_FINAL_WARNINGS="$(printf '%s\n' "${WARNINGS[@]-}")"
export CROSSALPHA_FINAL_LOG="$LOG"
export CROSSALPHA_A_HASH_BASELINE="$A_HASH_BASELINE"
export CROSSALPHA_A_HASH_FINAL="$A_HASH_FINAL"
export CROSSALPHA_AB_HASH_BASELINE="$AB_HASH_BASELINE"
export CROSSALPHA_AB_HASH_FINAL="$AB_HASH_FINAL"
python - "$SUMMARY" <<'PY'
import json
import os
import sys
from pathlib import Path

failures = [x for x in os.environ.get("CROSSALPHA_FINAL_FAILURES", "").splitlines() if x]
warnings = [x for x in os.environ.get("CROSSALPHA_FINAL_WARNINGS", "").splitlines() if x]
a0 = os.environ.get("CROSSALPHA_A_HASH_BASELINE")
a1 = os.environ.get("CROSSALPHA_A_HASH_FINAL")
b0 = os.environ.get("CROSSALPHA_AB_HASH_BASELINE")
b1 = os.environ.get("CROSSALPHA_AB_HASH_FINAL")
payload = {
    "result": "PASS" if not failures else "FAILED",
    "failures": failures,
    "warnings": warnings,
    "A_freeze_hash_baseline": a0,
    "A_freeze_hash_final": a1,
    "A_freeze_hash_unchanged": a0 == a1 != "MISSING",
    "AB_freeze_hash_baseline": b0,
    "AB_freeze_hash_final": b1,
    "AB_freeze_hash_unchanged": b0 == b1 != "MISSING",
    "log": os.environ.get("CROSSALPHA_FINAL_LOG"),
}
Path(sys.argv[1]).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY

echo
echo "=================================================="
echo "CROSSALPHA STATE A/B FINAL SUMMARY"
echo "=================================================="
if [[ ${#FAILURES[@]} -eq 0 ]]; then
  echo "FINAL_RESULT=PASS"
  echo "Frozen B3 + State Shadow + prospective A/B milestone is installed and validated."
else
  echo "FINAL_RESULT=FAILED"
  echo "FAILURE_COUNT=${#FAILURES[@]}"
  printf 'FAILURE=%s\n' "${FAILURES[@]}"
fi
if [[ ${#WARNINGS[@]} -gt 0 ]]; then
  echo "WARNING_COUNT=${#WARNINGS[@]}"
  printf 'WARNING=%s\n' "${WARNINGS[@]}"
fi
echo "A_HASH_BASELINE=$A_HASH_BASELINE"
echo "A_HASH_FINAL=$A_HASH_FINAL"
echo "AB_HASH_BASELINE=$AB_HASH_BASELINE"
echo "AB_HASH_FINAL=$AB_HASH_FINAL"
echo "LOG=$LOG"
echo "LATEST_LOG=$LATEST"
echo "SUMMARY=$SUMMARY"
echo "PERSISTENT_STATUS=$DATA_ROOT/manifests/final_state_ab_system_status.json"
echo

# Interactive safety: never close the caller's terminal.
exit 0
