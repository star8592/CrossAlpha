#!/usr/bin/env bash
# One-shot acceptance for Frozen B3 + State Shadow + prospective A/B.
# Exactly one full pytest run. No historical baseline/robustness/bootstrap reruns.
# Critical phases are gated: a failed phase prevents later mutations.
# Always exits 0 so the caller's interactive terminal stays open.

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

phase_ok() {
  [[ ${#FAILURES[@]} -eq 0 ]]
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

echo "CrossAlpha State A/B milestone finalizer"
echo "repo=$REPO_DIR"
echo "data_root=$DATA_ROOT"
echo "today_utc=$(date -u +%F)"
echo "log=$LOG"
echo "A_hash_at_start=$A_HASH_AT_START"
echo "AB_hash_at_start=$AB_HASH_AT_START"
python --version || true
git rev-parse --short HEAD || true

# A. Code validation. No ledger/service mutation before this phase passes.
run_critical "A1. editable install" python -m pip install -e ".[dev]"
run_critical "A2. Python compile check" python -m compileall -q src scripts
run_critical "A3. full pytest suite (single run)" pytest -q

# B. Establish immutable protocol freezes, then audit empty/current ledgers.
if phase_ok; then
  run_critical "B1. free Core credential/status gate" crossalpha free-core-status
fi
if phase_ok; then
  run_critical "B2. ensure Frozen B3 paper freeze" \
    crossalpha-free-paper-freeze --historical-start 2010-06-01 --historical-end 2026-09-01
  if [[ "$A_HASH_BASELINE" == "MISSING" ]]; then
    A_HASH_BASELINE="$(sha_or_missing "$A_FREEZE")"
  fi
fi
if phase_ok; then
  run_critical "B3. ensure prospective State A/B freeze" crossalpha-state-ab-freeze
  if [[ "$AB_HASH_BASELINE" == "MISSING" ]]; then
    AB_HASH_BASELINE="$(sha_or_missing "$AB_FREEZE")"
  fi
fi
if phase_ok; then
  run_critical "B4. Frozen B3 ledger integrity" python scripts/check_free_paper_integrity.py
  run_critical "B5. strict State A/B ledger integrity" python scripts/check_state_ab_integrity.py
fi

# C. Upgrade services only after both immutable protocols are clean.
if phase_ok; then
  run_critical "C1. install Observatory + State materializer" bash scripts/install_all_user_services.sh
fi
if phase_ok; then
  run_critical "C2. install Frozen B3 + State A/B paper services" \
    bash scripts/install_free_paper_user_services.sh
fi

# D. Materialize current observatory/state only; never rerun historical research.
if phase_ok; then
  run_critical "D1. materialize Observatory + State Shadow once" \
    python scripts/materialize_observatory_and_state.py
fi
if phase_ok; then
  run_critical "D2. compute current State Shadow without write" bash -lc \
    "source .venv/bin/activate && crossalpha-state-shadow --no-write > '$STATE_JSON' && cat '$STATE_JSON'"
fi
if phase_ok; then
  run_critical "D3. rebuild DuckDB catalog" crossalpha build-catalog
  run_warning "D4. Observatory live health" crossalpha observatory-live-health
fi

# E. Re-read authoritative states after all installation/materialization work.
if phase_ok; then
  run_critical "E1. final Frozen B3 status" bash -lc \
    "source .venv/bin/activate && crossalpha-free-paper-status > '$A_STATUS_JSON' && cat '$A_STATUS_JSON'"
fi
if phase_ok; then
  run_critical "E2. final strict State A/B status" bash -lc \
    "source .venv/bin/activate && crossalpha-state-ab-status > '$AB_STATUS_JSON' && cat '$AB_STATUS_JSON'"
fi
if phase_ok; then
  run_critical "E3. final Frozen B3 integrity" python scripts/check_free_paper_integrity.py
  run_critical "E4. final strict State A/B integrity" python scripts/check_state_ab_integrity.py
fi

# F. Hard invariant audit is the final acceptance gate.
if phase_ok; then
  run_critical "F1. final hard invariant audit" python scripts/final_state_ab_audit.py \
    --data-root "$DATA_ROOT" \
    --a-hash-baseline "$A_HASH_BASELINE" \
    --ab-hash-baseline "$AB_HASH_BASELINE" \
    --a-status "$A_STATUS_JSON" \
    --ab-status "$AB_STATUS_JSON" \
    --state-status "$STATE_JSON"
fi

# Operational context is always displayed but never masks the critical result.
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
