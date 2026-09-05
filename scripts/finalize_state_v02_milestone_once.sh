#!/usr/bin/env bash
# Final one-shot acceptance for descriptive State V0.2.
# Exactly one full pytest run. No Core/State-V0.1 historical research is rerun.
# Live Aave compatibility is preflighted BEFORE the immutable V0.2 freeze.
# Critical phases are gated and this script always exits 0 so the caller's
# interactive terminal stays open. Inspect FINAL_RESULT for the real result.

set -u -o pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="/tmp/crossalpha_state_v02_finalize_${STAMP}.log"
LATEST="/tmp/crossalpha_state_v02_finalize_latest.log"
SUMMARY="/tmp/crossalpha_state_v02_finalize_summary.json"
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
V02_PREFLIGHT_JSON="/tmp/crossalpha_state_v02_preflight.json"
V02_LIVE_JSON="/tmp/crossalpha_state_v02_first_live.json"
V02_STATUS_JSON="/tmp/crossalpha_state_v02_status.json"

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
V02_HASH_AT_START="$(sha_or_missing "$V02_FREEZE")"

echo "CrossAlpha State V0.2 milestone finalizer"
echo "repo=$REPO_DIR"
echo "data_root=$DATA_ROOT"
echo "today_utc=$(date -u +%F)"
echo "log=$LOG"
echo "A_hash_baseline=$A_HASH_BASELINE"
echo "AB_hash_baseline=$AB_HASH_BASELINE"
echo "V02_hash_at_start=$V02_HASH_AT_START"
python --version || true
git rev-parse --short HEAD || true

# A. Code validation. No new V0.2 freeze/service mutation before this passes.
run_critical "A1. editable install" python -m pip install -e ".[dev]"
run_critical "A2. Python compile check" python -m compileall -q src scripts
run_critical "A3. full pytest suite (single run)" pytest -q

# B. Prove that the already-live V0.1 research remains intact.
if phase_ok; then
  run_critical "B1. Frozen B3 ledger integrity" python scripts/check_free_paper_integrity.py
  run_critical "B2. State A/B V0.1 strict integrity" python scripts/check_state_ab_integrity.py
fi
if phase_ok; then
  if [[ "$A_HASH_BASELINE" == "MISSING" || "$AB_HASH_BASELINE" == "MISSING" ]]; then
    FAILURES+=("B3. V0.1 freeze files missing::1")
  fi
fi

# C. Validate protocol and live external compatibility before freezing V0.2.
if phase_ok; then
  run_critical "C1. State V0.2 config/implementation consistency" crossalpha-state-v02-config-check
fi
if phase_ok; then
  run_critical "C2. live Aave V0.2 preflight before freeze" bash -lc \
    "source .venv/bin/activate && crossalpha-state-v02-cycle > '$V02_PREFLIGHT_JSON' && cat '$V02_PREFLIGHT_JSON'"
fi
if phase_ok; then
  run_critical "C3. preflight is descriptive and non-mutating" python - "$V02_PREFLIGHT_JSON" <<'PY'
import json, sys
from pathlib import Path
p = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
s = p["state_v02"]
assert p["data_cost_usd"] == 0
assert p["v01_collector_or_paper_mutated"] is False
assert p["aave_market"]["ok"] is True
assert s["protocol"] == "CROSSALPHA_STATE_V0_2"
assert s["actionability"] == "DESCRIPTIVE_ONLY"
assert s["risk_multiplier"] is None
assert s["mutates_frozen_core"] is False
assert s["mutates_state_v01"] is False
assert s["mutates_state_ab_v01"] is False
assert p["prospective"]["status"] == "not_frozen_no_prospective_write" or p["prospective_status"].get("frozen") is True
print(json.dumps({
    "preflight_ok": True,
    "state_confidence": s.get("data_confidence"),
    "stress_score": s.get("descriptive_stress_score"),
    "optional_liquidation_rpc": p.get("aave_liquidations_rpc"),
}, ensure_ascii=False, indent=2, default=str))
PY
fi

# D. Freeze only after live preflight succeeds, then write first live observation.
if phase_ok; then
  run_critical "D1. freeze State V0.2 prospective protocol" crossalpha-state-v02-freeze
fi
if phase_ok; then
  run_critical "D2. first frozen live State V0.2 observation" bash -lc \
    "source .venv/bin/activate && crossalpha-state-v02-cycle > '$V02_LIVE_JSON' && cat '$V02_LIVE_JSON'"
fi
if phase_ok; then
  run_critical "D3. State V0.2 prospective integrity" crossalpha-state-v02-integrity
  run_critical "D4. State V0.2 prospective status" bash -lc \
    "source .venv/bin/activate && crossalpha-state-v02-status > '$V02_STATUS_JSON' && cat '$V02_STATUS_JSON'"
fi

# E. Enable isolated automation only after the frozen live cycle is clean.
if phase_ok; then
  run_critical "E1. install isolated State V0.2 timer" bash scripts/install_state_v02_user_service.sh
fi

# F. Cross-version hard invariant audit: V0.1 must be byte-identical and live.
if phase_ok; then
  run_critical "F1. final State V0.2 hard invariant audit" python scripts/final_state_v02_audit.py \
    --data-root "$DATA_ROOT" \
    --a-hash-baseline "$A_HASH_BASELINE" \
    --ab-hash-baseline "$AB_HASH_BASELINE" \
    --require-v02-timer
fi

# Operational context never masks the critical result.
run_warning "G1. all CrossAlpha timers" bash -lc 'systemctl --user list-timers --all | grep crossalpha || true'
run_warning "G2. State V0.2 timer status" bash -lc 'systemctl --user status crossalpha-state-v02.timer --no-pager || true'
run_warning "G3. latest State V0.2 service log" bash -lc 'journalctl --user -u crossalpha-state-v02.service -n 80 --no-pager || true'

A_HASH_FINAL="$(sha_or_missing "$A_FREEZE")"
AB_HASH_FINAL="$(sha_or_missing "$AB_FREEZE")"
V02_HASH_FINAL="$(sha_or_missing "$V02_FREEZE")"

export CROSSALPHA_V02_FAILURES="$(printf '%s\n' "${FAILURES[@]-}")"
export CROSSALPHA_V02_WARNINGS="$(printf '%s\n' "${WARNINGS[@]-}")"
export CROSSALPHA_V02_LOG="$LOG"
export CROSSALPHA_A_HASH_BASELINE="$A_HASH_BASELINE"
export CROSSALPHA_A_HASH_FINAL="$A_HASH_FINAL"
export CROSSALPHA_AB_HASH_BASELINE="$AB_HASH_BASELINE"
export CROSSALPHA_AB_HASH_FINAL="$AB_HASH_FINAL"
export CROSSALPHA_V02_HASH_AT_START="$V02_HASH_AT_START"
export CROSSALPHA_V02_HASH_FINAL="$V02_HASH_FINAL"
python - "$SUMMARY" <<'PY'
import json, os, sys
from pathlib import Path
failures = [x for x in os.environ.get("CROSSALPHA_V02_FAILURES", "").splitlines() if x]
warnings = [x for x in os.environ.get("CROSSALPHA_V02_WARNINGS", "").splitlines() if x]
a0 = os.environ.get("CROSSALPHA_A_HASH_BASELINE")
a1 = os.environ.get("CROSSALPHA_A_HASH_FINAL")
b0 = os.environ.get("CROSSALPHA_AB_HASH_BASELINE")
b1 = os.environ.get("CROSSALPHA_AB_HASH_FINAL")
v0 = os.environ.get("CROSSALPHA_V02_HASH_AT_START")
v1 = os.environ.get("CROSSALPHA_V02_HASH_FINAL")
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
    "V02_freeze_hash_at_start": v0,
    "V02_freeze_hash_final": v1,
    "V02_frozen": v1 != "MISSING",
    "log": os.environ.get("CROSSALPHA_V02_LOG"),
}
Path(sys.argv[1]).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY

echo
echo "=================================================="
echo "CROSSALPHA STATE V0.2 FINAL SUMMARY"
echo "=================================================="
if [[ ${#FAILURES[@]} -eq 0 ]]; then
  echo "FINAL_RESULT=PASS"
  echo "State V0.2 descriptive prospective milestone is installed and validated."
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
echo "LOG=$LOG"
echo "LATEST_LOG=$LATEST"
echo "SUMMARY=$SUMMARY"
echo "PERSISTENT_STATUS=$DATA_ROOT/manifests/final_state_v02_system_status.json"
echo

exit 0
