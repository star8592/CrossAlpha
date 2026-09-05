#!/usr/bin/env bash
# One-shot acceptance for State V0.3 borrower health-factor shadow research.
# Exactly one full pytest run. Historical borrower bootstrap is operational only
# and is allowed to remain incomplete after this acceptance run.
# This script always exits 0 so it cannot close the caller's interactive shell.
# Inspect FINAL_RESULT for the actual milestone result.

set -u -o pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="/tmp/crossalpha_state_v03_finalize_${STAMP}.log"
LATEST="/tmp/crossalpha_state_v03_finalize_latest.log"
SUMMARY="/tmp/crossalpha_state_v03_finalize_summary.json"
PREFLIGHT="/tmp/crossalpha_state_v03_preflight.json"
FIRST_CYCLE="/tmp/crossalpha_state_v03_first_cycle.json"
STATUS_JSON="/tmp/crossalpha_state_v03_status.json"
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

A_HASH_BASELINE="$(sha_or_missing "$A_FREEZE")"
AB_HASH_BASELINE="$(sha_or_missing "$AB_FREEZE")"
V02_HASH_BASELINE="$(sha_or_missing "$V02_FREEZE")"
V03_HASH_AT_START="$(sha_or_missing "$V03_FREEZE")"

echo "CrossAlpha State V0.3 milestone finalizer"
echo "repo=$REPO_DIR"
echo "data_root=$DATA_ROOT"
echo "today_utc=$(date -u +%F)"
echo "log=$LOG"
echo "A_hash_baseline=$A_HASH_BASELINE"
echo "AB_hash_baseline=$AB_HASH_BASELINE"
echo "V02_hash_baseline=$V02_HASH_BASELINE"
echo "V03_hash_at_start=$V03_HASH_AT_START"
python --version || true
git rev-parse --short HEAD || true

# A. Validate code once. No V0.3 freeze/service changes before the full suite passes.
run_critical "A1. editable install" python -m pip install -e ".[dev]"
run_critical "A2. Python compile check" python -m compileall -q src scripts
run_critical "A3. full pytest suite (single run)" pytest -q

# B. Previous live protocols must already be frozen and healthy.
if phase_ok; then
  if [[ "$A_HASH_BASELINE" == "MISSING" || "$AB_HASH_BASELINE" == "MISSING" || "$V02_HASH_BASELINE" == "MISSING" ]]; then
    FAILURES+=("B0. predecessor freeze missing::1")
  fi
fi
if phase_ok; then
  run_critical "B1. Frozen B3 ledger integrity" python scripts/check_free_paper_integrity.py
  run_critical "B2. State A/B V0.1 strict integrity" python scripts/check_state_ab_integrity.py
  run_critical "B3. State V0.2 strict integrity" crossalpha-state-v02-integrity
fi

# C. Validate V0.3 protocol and real RPC capabilities BEFORE freezing.
if phase_ok; then
  run_critical "C1. State V0.3 config/implementation consistency" crossalpha-state-v03-config-check
fi
if phase_ok; then
  run_critical "C2. live State V0.3 RPC preflight" bash -lc \
    "source .venv/bin/activate && crossalpha-state-v03-preflight > '$PREFLIGHT' && cat '$PREFLIGHT'"
fi
if phase_ok; then
  run_critical "C3. preflight proves zero-cost historical + fixed-block capability" python - "$PREFLIGHT" <<'PY'
import json, sys
from pathlib import Path
p = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert p["protocol"] == "CROSSALPHA_STATE_V0_3_PREFLIGHT"
assert p["data_cost_usd"] == 0
assert p["historical_log_scan_ok"] is True
assert p["fixed_block_account_call_ok"] is True
assert p["finalized_block"] > 16291127
assert p["actionability"] == "DESCRIPTIVE_ONLY"
assert p["risk_multiplier"] is None
assert p["rpc_source"] in {"EVM_RPC_URL", "PUBLICNODE_ZERO_COST_FALLBACK"}
print(json.dumps({
    "preflight_ok": True,
    "rpc_source": p["rpc_source"],
    "latest_block": p["latest_block"],
    "finalized_block": p["finalized_block"],
}, indent=2))
PY
fi

# D. Freeze only after predecessor integrity and live RPC compatibility pass.
if phase_ok; then
  run_critical "D1. freeze State V0.3 prospective protocol" crossalpha-state-v03-freeze
fi

# E. Advance one bounded bootstrap cycle. Bootstrap incompleteness is expected/healthy.
if phase_ok; then
  run_critical "E1. first bounded State V0.3 cycle" bash -lc \
    "source .venv/bin/activate && crossalpha-state-v03-cycle > '$FIRST_CYCLE' && cat '$FIRST_CYCLE'"
fi
if phase_ok; then
  run_critical "E2. first cycle is descriptive zero-cost and isolated" python - "$FIRST_CYCLE" <<'PY'
import json, sys
from pathlib import Path
p = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
allowed = {
    "BORROWER_UNIVERSE_BOOTSTRAPPING",
    "FULL_CENSUS_RECORDED",
    "FULL_CENSUS_PARTIAL_RETRY_REQUIRED",
    "WATCHLIST_RECORDED",
    "CAUGHT_UP_AWAITING_NEXT_FULL_CENSUS",
}
assert p["protocol"] == "CROSSALPHA_STATE_V0_3_CYCLE"
assert p["status"] in allowed
assert p["data_cost_usd"] == 0
assert p["mutates_v01_or_v02"] is False
assert p["actionability"] == "DESCRIPTIVE_ONLY"
assert p["risk_multiplier"] is None
print(json.dumps({
    "cycle_ok": True,
    "status": p["status"],
    "rpc_source": p.get("rpc_source"),
    "candidate_address_count": p.get("candidate_address_count"),
    "next_block": p.get("next_block"),
}, indent=2))
PY
fi

# F. Frozen ledger can be empty while bootstrap is incomplete; integrity still must hold.
if phase_ok; then
  run_critical "F1. State V0.3 strict prospective integrity" crossalpha-state-v03-integrity
  run_critical "F2. State V0.3 status" bash -lc \
    "source .venv/bin/activate && crossalpha-state-v03-status > '$STATUS_JSON' && cat '$STATUS_JSON'"
fi

# G. Install isolated automation only after all research invariants above are clean.
if phase_ok; then
  run_critical "G1. install isolated State V0.3 timer" bash scripts/install_state_v03_user_service.sh
fi

# H. Cross-version hard audit; predecessor freeze bytes must be unchanged.
if phase_ok; then
  run_critical "H1. build DuckDB catalog" crossalpha build-catalog
  run_critical "H2. final State V0.3 hard invariant audit" python scripts/final_state_v03_audit.py \
    --data-root "$DATA_ROOT" \
    --a-hash-baseline "$A_HASH_BASELINE" \
    --ab-hash-baseline "$AB_HASH_BASELINE" \
    --v02-hash-baseline "$V02_HASH_BASELINE" \
    --require-v03-timer
fi

# Operational context never masks the critical result.
run_warning "I1. all CrossAlpha timers" bash -lc 'systemctl --user list-timers --all | grep crossalpha || true'
run_warning "I2. State V0.3 timer status" bash -lc 'systemctl --user status crossalpha-state-v03.timer --no-pager || true'
run_warning "I3. latest State V0.3 service log" bash -lc 'journalctl --user -u crossalpha-state-v03.service -n 80 --no-pager || true'

A_HASH_FINAL="$(sha_or_missing "$A_FREEZE")"
AB_HASH_FINAL="$(sha_or_missing "$AB_FREEZE")"
V02_HASH_FINAL="$(sha_or_missing "$V02_FREEZE")"
V03_HASH_FINAL="$(sha_or_missing "$V03_FREEZE")"

export CROSSALPHA_V03_FAILURES="$(printf '%s\n' "${FAILURES[@]-}")"
export CROSSALPHA_V03_WARNINGS="$(printf '%s\n' "${WARNINGS[@]-}")"
export CROSSALPHA_V03_LOG="$LOG"
export CROSSALPHA_A_HASH_BASELINE="$A_HASH_BASELINE"
export CROSSALPHA_A_HASH_FINAL="$A_HASH_FINAL"
export CROSSALPHA_AB_HASH_BASELINE="$AB_HASH_BASELINE"
export CROSSALPHA_AB_HASH_FINAL="$AB_HASH_FINAL"
export CROSSALPHA_V02_HASH_BASELINE="$V02_HASH_BASELINE"
export CROSSALPHA_V02_HASH_FINAL="$V02_HASH_FINAL"
export CROSSALPHA_V03_HASH_AT_START="$V03_HASH_AT_START"
export CROSSALPHA_V03_HASH_FINAL="$V03_HASH_FINAL"
python - "$SUMMARY" <<'PY'
import json, os, sys
from pathlib import Path
failures = [x for x in os.environ.get("CROSSALPHA_V03_FAILURES", "").splitlines() if x]
warnings = [x for x in os.environ.get("CROSSALPHA_V03_WARNINGS", "").splitlines() if x]
a0, a1 = os.environ.get("CROSSALPHA_A_HASH_BASELINE"), os.environ.get("CROSSALPHA_A_HASH_FINAL")
b0, b1 = os.environ.get("CROSSALPHA_AB_HASH_BASELINE"), os.environ.get("CROSSALPHA_AB_HASH_FINAL")
v20, v21 = os.environ.get("CROSSALPHA_V02_HASH_BASELINE"), os.environ.get("CROSSALPHA_V02_HASH_FINAL")
v30, v31 = os.environ.get("CROSSALPHA_V03_HASH_AT_START"), os.environ.get("CROSSALPHA_V03_HASH_FINAL")
payload = {
    "result": "PASS" if not failures else "FAILED",
    "failures": failures,
    "warnings": warnings,
    "A_freeze_hash_unchanged": a0 == a1 != "MISSING",
    "AB_freeze_hash_unchanged": b0 == b1 != "MISSING",
    "V02_freeze_hash_unchanged": v20 == v21 != "MISSING",
    "V03_freeze_hash_at_start": v30,
    "V03_freeze_hash_final": v31,
    "V03_frozen": v31 != "MISSING",
    "log": os.environ.get("CROSSALPHA_V03_LOG"),
}
Path(sys.argv[1]).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY

echo
echo "=================================================="
echo "CROSSALPHA STATE V0.3 FINAL SUMMARY"
echo "=================================================="
if [[ ${#FAILURES[@]} -eq 0 ]]; then
  echo "FINAL_RESULT=PASS"
  echo "State V0.3 borrower-risk shadow milestone is installed and validated."
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
echo "V02_HASH_BASELINE=$V02_HASH_BASELINE"
echo "V02_HASH_FINAL=$V02_HASH_FINAL"
echo "V03_HASH_FINAL=$V03_HASH_FINAL"
echo "LOG=$LOG"
echo "LATEST_LOG=$LATEST"
echo "SUMMARY=$SUMMARY"
echo "PERSISTENT_STATUS=$DATA_ROOT/manifests/final_state_v03_system_status.json"

echo
exit 0
