#!/usr/bin/env bash
# One-shot finalizer for the current CrossAlpha milestone.
# Design goals:
#   * exactly one full pytest run
#   * no historical robustness/bootstrap reruns
#   * failures are collected and reported at the end
#   * never closes an interactive parent terminal (always exits 0)
#   * Frozen B3 paper hash must remain unchanged
#   * State Shadow remains fault-isolated from Core/Paper

set -u -o pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="/tmp/crossalpha_finalize_${STAMP}.log"
LATEST="/tmp/crossalpha_finalize_latest.log"
SUMMARY="/tmp/crossalpha_finalize_summary.json"
ln -sfn "$LOG" "$LATEST"
exec > >(tee -a "$LOG") 2>&1

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

DATA_ROOT="${CROSSALPHA_DATA_DIR:-/mnt/disk2/CrossAlphaData}"
PAPER_FREEZE="$DATA_ROOT/research/free_v01/paper/freeze.json"
PAPER_STATUS_JSON="/tmp/crossalpha_final_paper_status.json"
STATE_STATUS_JSON="/tmp/crossalpha_final_state_status.json"

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

PAPER_HASH_BEFORE="$(sha_or_missing "$PAPER_FREEZE")"
TODAY_UTC="$(date -u +%F)"

echo "CrossAlpha one-shot milestone finalizer"
echo "repo=$REPO_DIR"
echo "data_root=$DATA_ROOT"
echo "today_utc=$TODAY_UTC"
echo "log=$LOG"
echo "paper_hash_before=$PAPER_HASH_BEFORE"
echo
python --version || true
git rev-parse --short HEAD || true

# ---------------------------------------------------------------------------
# Phase A: code validation. ONE full pytest run only.
# ---------------------------------------------------------------------------
run_critical "A1. editable install" python -m pip install -e ".[dev]"
run_critical "A2. Python compile check" python -m compileall -q src scripts
run_critical "A3. full pytest suite (single run)" pytest -q

# Do not mutate services or ledgers if code validation failed.
if [[ ${#FAILURES[@]} -eq 0 ]]; then
  # -------------------------------------------------------------------------
  # Phase B: immutable Core/Paper validation.
  # -------------------------------------------------------------------------
  run_critical "B1. free Core credential/status gate" crossalpha free-core-status
  run_critical "B2. Frozen B3 paper status" bash -lc "source .venv/bin/activate && crossalpha-free-paper-status > '$PAPER_STATUS_JSON' && cat '$PAPER_STATUS_JSON'"
  run_critical "B3. Frozen B3 ledger integrity" python scripts/check_free_paper_integrity.py

  # -------------------------------------------------------------------------
  # Phase C: install/update all local services idempotently.
  # Child scripts may use set -e; failures are captured here and cannot close
  # the parent interactive terminal.
  # -------------------------------------------------------------------------
  run_critical "C1. install Observatory + materializer services" bash scripts/install_all_user_services.sh
  run_critical "C2. install Frozen B3 paper services" bash scripts/install_free_paper_user_services.sh

  # -------------------------------------------------------------------------
  # Phase D: one current materialization; no historical research reruns.
  # -------------------------------------------------------------------------
  run_critical "D1. materialize Observatory + State Shadow once" python scripts/materialize_observatory_and_state.py
  run_critical "D2. compute current State Shadow" bash -lc "source .venv/bin/activate && crossalpha-state-shadow --no-write > '$STATE_STATUS_JSON' && cat '$STATE_STATUS_JSON'"
  run_critical "D3. rebuild DuckDB catalog" crossalpha build-catalog

  # Live health is operationally important but may fail because of a transient
  # upstream/network outage. Record it as a warning, not a code-integrity failure.
  run_warning "D4. Observatory live health" crossalpha observatory-live-health

  # -------------------------------------------------------------------------
  # Phase E: hard invariants, database visibility, service state.
  # -------------------------------------------------------------------------
  run_critical "E1. final hard invariant audit" python - "$DATA_ROOT" "$PAPER_HASH_BEFORE" "$PAPER_STATUS_JSON" "$STATE_STATUS_JSON" <<'PY'
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import duckdb

root = Path(sys.argv[1])
paper_hash_before = sys.argv[2]
paper_status_path = Path(sys.argv[3])
state_status_path = Path(sys.argv[4])
freeze = root / "research" / "free_v01" / "paper" / "freeze.json"

errors: list[str] = []
checks: dict[str, object] = {}

if not freeze.exists():
    errors.append(f"missing Frozen B3 freeze file: {freeze}")
    paper_hash_after = "MISSING"
else:
    paper_hash_after = hashlib.sha256(freeze.read_bytes()).hexdigest()

checks["paper_hash_before"] = paper_hash_before
checks["paper_hash_after"] = paper_hash_after
checks["paper_hash_unchanged"] = paper_hash_before == paper_hash_after != "MISSING"
if not checks["paper_hash_unchanged"]:
    errors.append("Frozen B3 paper freeze hash changed or is missing")

if paper_status_path.exists():
    paper = json.loads(paper_status_path.read_text(encoding="utf-8"))
    checks["paper_state"] = paper.get("state")
    checks["paper_integrity_ok"] = bool(paper.get("integrity_ok"))
    checks["parameter_optimization_allowed"] = paper.get("parameter_optimization_allowed")
    checks["paper_first_eligible_effective_date"] = paper.get("first_eligible_effective_date")
    if not checks["paper_integrity_ok"]:
        errors.append("Frozen B3 paper ledger integrity is false")
    if paper.get("parameter_optimization_allowed") is not False:
        errors.append("Frozen B3 unexpectedly allows parameter optimization")
else:
    errors.append("paper status JSON missing")

if state_status_path.exists():
    state = json.loads(state_status_path.read_text(encoding="utf-8"))
    checks["state_protocol"] = state.get("protocol")
    checks["state_mode"] = state.get("mode")
    checks["state_shadow_only"] = state.get("shadow_only")
    checks["state_core_protocol_mutated"] = state.get("core_protocol_mutated")
    checks["state_band"] = state.get("state_band")
    checks["state_multiplier"] = state.get("shadow_risk_multiplier")
    checks["state_data_confidence"] = state.get("data_confidence")
    if state.get("status") != "no_inputs":
        if state.get("protocol") != "CROSSALPHA_STATE_SHADOW_V0_1":
            errors.append("unexpected State Shadow protocol")
        if state.get("shadow_only") is not True:
            errors.append("State Shadow is not shadow_only")
        if state.get("core_protocol_mutated") is not False:
            errors.append("State Shadow reports Core mutation")
        try:
            multiplier = float(state.get("shadow_risk_multiplier"))
        except (TypeError, ValueError):
            multiplier = None
        if multiplier not in {0.5, 0.75, 1.0}:
            errors.append(f"invalid State Shadow multiplier: {multiplier!r}")
else:
    errors.append("state status JSON missing")

# DuckDB views and latest state row.
db = root / "catalog" / "crossalpha.duckdb"
if not db.exists():
    errors.append(f"catalog missing: {db}")
else:
    con = duckdb.connect(str(db), read_only=True)
    try:
        required_views = {
            "core.free_asset_returns",
            "observatory.hyperliquid_market_state",
            "observatory.stablecoin_system_state",
            "state_engine.shadow_v01",
        }
        rows = con.execute(
            "SELECT table_schema || '.' || table_name FROM information_schema.views"
        ).fetchall()
        present = {row[0] for row in rows}
        missing = sorted(required_views - present)
        checks["required_views_present"] = not missing
        checks["missing_views"] = missing
        if missing:
            errors.append("missing DuckDB views: " + ", ".join(missing))
        if "state_engine.shadow_v01" in present:
            latest = con.execute(
                """
                SELECT protocol, shadow_only, core_protocol_mutated,
                       state_band, shadow_risk_multiplier, data_confidence, as_of
                FROM state_engine.shadow_v01
                ORDER BY as_of DESC, generated_at DESC
                LIMIT 1
                """
            ).fetchone()
            checks["latest_state_row"] = list(latest) if latest else None
            if latest is None:
                errors.append("state_engine.shadow_v01 is empty")
            else:
                protocol, shadow_only, mutated, _band, multiplier, _confidence, _as_of = latest
                if protocol != "CROSSALPHA_STATE_SHADOW_V0_1":
                    errors.append("latest State row protocol mismatch")
                if not bool(shadow_only) or bool(mutated):
                    errors.append("latest State row violates shadow isolation")
                if float(multiplier) not in {0.5, 0.75, 1.0}:
                    errors.append("latest State row multiplier outside frozen set")
    finally:
        con.close()

# systemd units. oneshot materializer service itself may be inactive; its timer must be active.
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

summary_path = root / "manifests" / "final_system_status.json"
summary_path.parent.mkdir(parents=True, exist_ok=True)
summary_path.write_text(json.dumps(checks, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print(json.dumps(checks, ensure_ascii=False, indent=2, default=str))
print(f"FINAL_STATUS_FILE={summary_path}")
if errors:
    raise SystemExit(1)
PY
fi

# Always show systemd/timer context, even after a critical failure.
run_warning "F1. all CrossAlpha timers" bash -lc 'systemctl --user list-timers --all | grep crossalpha || true'
run_warning "F2. collector status" bash -lc 'systemctl --user status crossalpha-observatory.service --no-pager || true'
run_warning "F3. materializer timer status" bash -lc 'systemctl --user status crossalpha-materializer.timer --no-pager || true'
run_warning "F4. paper daily timer status" bash -lc 'systemctl --user status crossalpha-free-paper-daily.timer --no-pager || true'
run_warning "F5. paper weekly timer status" bash -lc 'systemctl --user status crossalpha-free-paper-weekly.timer --no-pager || true'

PAPER_HASH_FINAL="$(sha_or_missing "$PAPER_FREEZE")"

# Persist a compact machine-readable execution summary outside the repo.
export CROSSALPHA_FINAL_FAILURES="$(printf '%s\n' "${FAILURES[@]-}")"
export CROSSALPHA_FINAL_WARNINGS="$(printf '%s\n' "${WARNINGS[@]-}")"
export CROSSALPHA_FINAL_LOG="$LOG"
export CROSSALPHA_FINAL_PAPER_HASH_BEFORE="$PAPER_HASH_BEFORE"
export CROSSALPHA_FINAL_PAPER_HASH_FINAL="$PAPER_HASH_FINAL"
python - "$SUMMARY" <<'PY'
import json
import os
import sys
from pathlib import Path

failures = [x for x in os.environ.get("CROSSALPHA_FINAL_FAILURES", "").splitlines() if x]
warnings = [x for x in os.environ.get("CROSSALPHA_FINAL_WARNINGS", "").splitlines() if x]
payload = {
    "result": "PASS" if not failures else "FAILED",
    "failures": failures,
    "warnings": warnings,
    "paper_hash_before": os.environ.get("CROSSALPHA_FINAL_PAPER_HASH_BEFORE"),
    "paper_hash_final": os.environ.get("CROSSALPHA_FINAL_PAPER_HASH_FINAL"),
    "paper_hash_unchanged": (
        os.environ.get("CROSSALPHA_FINAL_PAPER_HASH_BEFORE")
        == os.environ.get("CROSSALPHA_FINAL_PAPER_HASH_FINAL")
        != "MISSING"
    ),
    "log": os.environ.get("CROSSALPHA_FINAL_LOG"),
}
Path(sys.argv[1]).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY

echo
echo "=================================================="
echo "CROSSALPHA FINAL SUMMARY"
echo "=================================================="
if [[ ${#FAILURES[@]} -eq 0 ]]; then
  echo "FINAL_RESULT=PASS"
  echo "Current milestone is installed and validated."
else
  echo "FINAL_RESULT=FAILED"
  echo "FAILURE_COUNT=${#FAILURES[@]}"
  printf 'FAILURE=%s\n' "${FAILURES[@]}"
fi
if [[ ${#WARNINGS[@]} -gt 0 ]]; then
  echo "WARNING_COUNT=${#WARNINGS[@]}"
  printf 'WARNING=%s\n' "${WARNINGS[@]}"
fi
echo "PAPER_HASH_BEFORE=$PAPER_HASH_BEFORE"
echo "PAPER_HASH_FINAL=$PAPER_HASH_FINAL"
echo "LOG=$LOG"
echo "LATEST_LOG=$LATEST"
echo "SUMMARY=$SUMMARY"
echo "PERSISTENT_STATUS=$DATA_ROOT/manifests/final_system_status.json"
echo

# Interactive safety: NEVER close the caller's terminal.
exit 0
