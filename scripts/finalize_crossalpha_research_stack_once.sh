#!/usr/bin/env bash
# Final one-shot acceptance for the complete CrossAlpha research stack.
# Runs exactly one full pytest suite. It never invokes older nested finalizers.
# The script always exits 0 so an interactive caller cannot be closed by a failure;
# FINAL_RESULT is the authoritative outcome.

set -u -o pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="/tmp/crossalpha_research_stack_finalize_${STAMP}.log"
LATEST="/tmp/crossalpha_research_stack_finalize_latest.log"
SUMMARY="/tmp/crossalpha_research_stack_finalize_summary.json"
ln -sfn "$LOG" "$LATEST"
exec > >(tee -a "$LOG") 2>&1

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

DATA_ROOT="${CROSSALPHA_DATA_DIR:-/mnt/disk2/CrossAlphaData}"
A_FREEZE="$DATA_ROOT/research/free_v01/paper/freeze.json"
AB_FREEZE="$DATA_ROOT/research/free_v01/state_ab_v01/freeze.json"
V02_FREEZE="$DATA_ROOT/research/state_v02/freeze.json"
V03_FREEZE="$DATA_ROOT/research/state_v03/freeze.json"
V04_FREEZE="$DATA_ROOT/research/state_v04/freeze.json"
OUTCOME_FREEZE="$DATA_ROOT/research/outcome_linkage_v01/freeze.json"

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

phase_ok() { [[ ${#FAILURES[@]} -eq 0 ]]; }
sha_or_missing() {
  local path="$1"
  if [[ -f "$path" ]]; then sha256sum "$path" | awk '{print $1}'; else echo MISSING; fi
}

A_HASH_BASELINE="$(sha_or_missing "$A_FREEZE")"
AB_HASH_BASELINE="$(sha_or_missing "$AB_FREEZE")"
V02_HASH_START="$(sha_or_missing "$V02_FREEZE")"
V03_HASH_START="$(sha_or_missing "$V03_FREEZE")"
V04_HASH_START="$(sha_or_missing "$V04_FREEZE")"
OUTCOME_HASH_START="$(sha_or_missing "$OUTCOME_FREEZE")"

echo "CrossAlpha complete research-stack finalizer"
echo "repo=$REPO_DIR"
echo "data_root=$DATA_ROOT"
echo "today_utc=$(date -u +%F)"
echo "log=$LOG"
echo "A_HASH_BASELINE=$A_HASH_BASELINE"
echo "AB_HASH_BASELINE=$AB_HASH_BASELINE"
echo "V02_HASH_START=$V02_HASH_START"
echo "V03_HASH_START=$V03_HASH_START"
echo "V04_HASH_START=$V04_HASH_START"
echo "OUTCOME_HASH_START=$OUTCOME_HASH_START"
python --version || true
git rev-parse --short HEAD || true

# A. Validate the entire codebase exactly once before creating any new freeze.
run_critical "A1. editable install" python -m pip install -e ".[dev]"
run_critical "A2. Python compile check" python -m compileall -q src scripts
run_critical "A3. full pytest suite (single run)" pytest -q

# B. The already-frozen Core/A-B experiment is immutable and must remain healthy.
if phase_ok; then
  if [[ "$A_HASH_BASELINE" == MISSING || "$AB_HASH_BASELINE" == MISSING ]]; then
    FAILURES+=("B0. Frozen V0.1/A-B freeze missing::1")
  fi
fi
if phase_ok; then
  run_critical "B1. Frozen B3 integrity" python scripts/check_free_paper_integrity.py
  run_critical "B2. State A/B V0.1 strict integrity" python scripts/check_state_ab_integrity.py
fi

# C. State V0.2. A pre-freeze cycle is required because its freeze command itself
# does not perform the external-data preflight. A second post-freeze cycle creates
# the first admissible prospective observation.
if phase_ok; then
  run_critical "C1. State V0.2 config consistency" crossalpha-state-v02-config-check
fi
if phase_ok && [[ ! -f "$V02_FREEZE" ]]; then
  run_critical "C2. State V0.2 live preflight" bash -lc \
    'source .venv/bin/activate && crossalpha-state-v02-cycle > /tmp/crossalpha_stack_v02_preflight.json && cat /tmp/crossalpha_stack_v02_preflight.json'
fi
if phase_ok && [[ ! -f "$V02_FREEZE" ]]; then
  run_critical "C3. freeze State V0.2" crossalpha-state-v02-freeze
  run_critical "C4. first frozen State V0.2 live observation" crossalpha-state-v02-cycle
fi
if phase_ok; then
  run_critical "C5. State V0.2 strict integrity" crossalpha-state-v02-integrity
  run_critical "C6. State V0.2 status" crossalpha-state-v02-status
fi
V02_HASH_EXPECTED="$(sha_or_missing "$V02_FREEZE")"

# D. State V0.3 freeze already performs its own live RPC preflight. Do not call
# the preflight separately; after freeze run only one bounded bootstrap/live cycle.
if phase_ok; then
  run_critical "D1. State V0.3 config consistency" crossalpha-state-v03-config-check
fi
if phase_ok && [[ ! -f "$V03_FREEZE" ]]; then
  run_critical "D2. preflight + freeze State V0.3" crossalpha-state-v03-freeze
fi
if phase_ok; then
  run_critical "D3. one bounded State V0.3 bootstrap/live cycle" crossalpha-state-v03-cycle
  run_critical "D4. State V0.3 strict integrity" crossalpha-state-v03-integrity
  run_critical "D5. State V0.3 status" crossalpha-state-v03-status
fi
V03_HASH_EXPECTED="$(sha_or_missing "$V03_FREEZE")"

# E. State V0.4 freeze already performs one live multi-venue preflight. After
# freeze, run exactly one formal data cycle; that cycle also persists health.
if phase_ok; then
  run_critical "E1. State V0.4 config + fault-isolation hash" crossalpha-state-v04-config-check
fi
if phase_ok && [[ ! -f "$V04_FREEZE" ]]; then
  run_critical "E2. preflight + freeze State V0.4" crossalpha-state-v04-freeze
fi
if phase_ok; then
  run_critical "E3. single frozen State V0.4 live cycle + health" python scripts/run_state_v04_cycle.py
  run_critical "E4. State V0.4 strict raw-to-vector integrity" crossalpha-state-v04-integrity
  run_critical "E5. State V0.4 status" crossalpha-state-v04-status
fi
V04_HASH_EXPECTED="$(sha_or_missing "$V04_FREEZE")"

# F. Outcome Linkage is frozen only after all source ledgers are healthy. The cycle script
# performs exactly one deterministic materialization and persists health/catalog.
if phase_ok; then
  run_critical "F1. Outcome Linkage config consistency" crossalpha-outcome-config-check
fi
if phase_ok && [[ ! -f "$OUTCOME_FREEZE" ]]; then
  run_critical "F2. freeze Outcome Linkage V0.1" crossalpha-outcome-freeze
fi
if phase_ok; then
  run_critical "F3. single deterministic outcome materialization + health/catalog" python scripts/run_outcome_linkage_cycle.py
  run_critical "F4. Outcome Linkage strict integrity" crossalpha-outcome-integrity
  run_critical "F5. Outcome Linkage status" crossalpha-outcome-status
fi
OUTCOME_HASH_EXPECTED="$(sha_or_missing "$OUTCOME_FREEZE")"

# G. Install automation only after all protocol/data invariants above pass.
if phase_ok; then
  run_critical "G1. install/update State V0.2 timer" bash scripts/install_state_v02_user_service.sh
  run_critical "G2. install/update State V0.3 timer" bash scripts/install_state_v03_user_service.sh
  run_critical "G3. install/update State V0.4 timer" bash scripts/install_state_v04_user_service.sh
  run_critical "G4. install/update Outcome Linkage timer" bash scripts/install_outcome_linkage_user_service.sh
fi

# H. The hard audit performs the authoritative final catalog rebuild itself.
if phase_ok; then
  run_critical "H1. complete cross-version hard invariant audit + final catalog" python scripts/final_crossalpha_research_system_audit.py \
    --data-root "$DATA_ROOT" \
    --a-hash-baseline "$A_HASH_BASELINE" \
    --ab-hash-baseline "$AB_HASH_BASELINE" \
    --v02-hash-expected "$V02_HASH_EXPECTED" \
    --v03-hash-expected "$V03_HASH_EXPECTED" \
    --v04-hash-expected "$V04_HASH_EXPECTED" \
    --outcome-hash-expected "$OUTCOME_HASH_EXPECTED"
fi

run_warning "I1. CrossAlpha timers" bash -lc 'systemctl --user list-timers --all | grep crossalpha || true'
run_warning "I2. V0.4 recent service log" bash -lc 'journalctl --user -u crossalpha-state-v04.service -n 60 --no-pager || true'
run_warning "I3. Outcome recent service log" bash -lc 'journalctl --user -u crossalpha-outcome-linkage.service -n 60 --no-pager || true'

A_HASH_FINAL="$(sha_or_missing "$A_FREEZE")"
AB_HASH_FINAL="$(sha_or_missing "$AB_FREEZE")"
V02_HASH_FINAL="$(sha_or_missing "$V02_FREEZE")"
V03_HASH_FINAL="$(sha_or_missing "$V03_FREEZE")"
V04_HASH_FINAL="$(sha_or_missing "$V04_FREEZE")"
OUTCOME_HASH_FINAL="$(sha_or_missing "$OUTCOME_FREEZE")"

# Existing freezes must never mutate. New freezes must remain byte-identical after creation.
if [[ "$A_HASH_FINAL" != "$A_HASH_BASELINE" ]]; then FAILURES+=("J1. A freeze hash changed::1"); fi
if [[ "$AB_HASH_FINAL" != "$AB_HASH_BASELINE" ]]; then FAILURES+=("J2. AB freeze hash changed::1"); fi
if [[ "$V02_HASH_FINAL" != "$V02_HASH_EXPECTED" ]]; then FAILURES+=("J3. V02 freeze hash changed after acceptance::1"); fi
if [[ "$V03_HASH_FINAL" != "$V03_HASH_EXPECTED" ]]; then FAILURES+=("J4. V03 freeze hash changed after acceptance::1"); fi
if [[ "$V04_HASH_FINAL" != "$V04_HASH_EXPECTED" ]]; then FAILURES+=("J5. V04 freeze hash changed after acceptance::1"); fi
if [[ "$OUTCOME_HASH_FINAL" != "$OUTCOME_HASH_EXPECTED" ]]; then FAILURES+=("J6. Outcome freeze hash changed after acceptance::1"); fi

export CROSSALPHA_STACK_FAILURES="$(printf '%s\n' "${FAILURES[@]-}")"
export CROSSALPHA_STACK_WARNINGS="$(printf '%s\n' "${WARNINGS[@]-}")"
export CROSSALPHA_STACK_LOG="$LOG"
export CROSSALPHA_A_HASH_BASELINE="$A_HASH_BASELINE"
export CROSSALPHA_A_HASH_FINAL="$A_HASH_FINAL"
export CROSSALPHA_AB_HASH_BASELINE="$AB_HASH_BASELINE"
export CROSSALPHA_AB_HASH_FINAL="$AB_HASH_FINAL"
export CROSSALPHA_V02_HASH_FINAL="$V02_HASH_FINAL"
export CROSSALPHA_V03_HASH_FINAL="$V03_HASH_FINAL"
export CROSSALPHA_V04_HASH_FINAL="$V04_HASH_FINAL"
export CROSSALPHA_OUTCOME_HASH_FINAL="$OUTCOME_HASH_FINAL"
python - "$SUMMARY" <<'PY'
import json, os, sys
from pathlib import Path
failures = [x for x in os.environ.get("CROSSALPHA_STACK_FAILURES", "").splitlines() if x]
warnings = [x for x in os.environ.get("CROSSALPHA_STACK_WARNINGS", "").splitlines() if x]
payload = {
    "result": "PASS" if not failures else "FAILED",
    "failures": failures,
    "warnings": warnings,
    "A_hash_unchanged": os.environ.get("CROSSALPHA_A_HASH_BASELINE") == os.environ.get("CROSSALPHA_A_HASH_FINAL"),
    "AB_hash_unchanged": os.environ.get("CROSSALPHA_AB_HASH_BASELINE") == os.environ.get("CROSSALPHA_AB_HASH_FINAL"),
    "V02_hash_final": os.environ.get("CROSSALPHA_V02_HASH_FINAL"),
    "V03_hash_final": os.environ.get("CROSSALPHA_V03_HASH_FINAL"),
    "V04_hash_final": os.environ.get("CROSSALPHA_V04_HASH_FINAL"),
    "OUTCOME_hash_final": os.environ.get("CROSSALPHA_OUTCOME_HASH_FINAL"),
    "log": os.environ.get("CROSSALPHA_STACK_LOG"),
}
Path(sys.argv[1]).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY

echo
echo "=================================================="
echo "CROSSALPHA COMPLETE RESEARCH STACK SUMMARY"
echo "=================================================="
if [[ ${#FAILURES[@]} -eq 0 ]]; then
  echo "FINAL_RESULT=PASS"
  echo "ENGINEERING_STACK=SEALED"
  echo "NEXT_PHASE=PROSPECTIVE_EVIDENCE_ACCUMULATION"
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
echo "V02_HASH_FINAL=$V02_HASH_FINAL"
echo "V03_HASH_FINAL=$V03_HASH_FINAL"
echo "V04_HASH_FINAL=$V04_HASH_FINAL"
echo "OUTCOME_HASH_FINAL=$OUTCOME_HASH_FINAL"
echo "LOG=$LOG"
echo "LATEST_LOG=$LATEST"
echo "SUMMARY=$SUMMARY"
echo "PERSISTENT_STATUS=$DATA_ROOT/manifests/final_crossalpha_research_system_status.json"

exit 0
