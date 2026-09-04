#!/usr/bin/env bash
set -u -o pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="/tmp/crossalpha_state_shadow_setup_${STAMP}.log"
LATEST="/tmp/crossalpha_state_shadow_setup_latest.log"
ln -sfn "$LOG" "$LATEST"
exec > >(tee -a "$LOG") 2>&1

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

FAIL_STEP=""
FAIL_CODE=0

run_step() {
  local name="$1"
  shift
  echo
  echo "=================================================="
  echo "$name"
  echo "=================================================="
  "$@"
  local rc=$?
  echo "EXIT_CODE=$rc"
  if [[ $rc -ne 0 && -z "$FAIL_STEP" ]]; then
    FAIL_STEP="$name"
    FAIL_CODE=$rc
  fi
  return 0
}

run_critical() {
  local name="$1"
  shift
  run_step "$name" "$@"
  [[ -z "$FAIL_STEP" ]]
}

FREEZE="${CROSSALPHA_DATA_DIR:-/mnt/disk2/CrossAlphaData}/research/free_v01/paper/freeze.json"
if [[ -f "$FREEZE" ]]; then
  PAPER_HASH_BEFORE="$(sha256sum "$FREEZE" | awk '{print $1}')"
else
  PAPER_HASH_BEFORE="MISSING"
fi

echo "CrossAlpha State Shadow v0.1 safe setup"
echo "repo=$REPO_DIR"
echo "log=$LOG"
echo "paper_freeze=$FREEZE"
echo "paper_hash_before=$PAPER_HASH_BEFORE"

run_critical "1. editable install" python -m pip install -e ".[dev]" || true
if [[ -z "$FAIL_STEP" ]]; then
  run_critical "2. full pytest" pytest -q || true
fi
if [[ -z "$FAIL_STEP" ]]; then
  run_critical "3. frozen B3 paper status before state" crossalpha-free-paper-status || true
fi
if [[ -z "$FAIL_STEP" ]]; then
  run_critical "4. compute state shadow without write" crossalpha-state-shadow --no-write || true
fi
if [[ -z "$FAIL_STEP" ]]; then
  run_critical "5. materialize state shadow and catalog" crossalpha-state-shadow || true
fi
if [[ -z "$FAIL_STEP" ]]; then
  run_critical "6. verify state DuckDB view" python - <<'PY'
import duckdb
from pathlib import Path

root = Path(__import__('os').environ.get('CROSSALPHA_DATA_DIR', '/mnt/disk2/CrossAlphaData'))
db = root / 'catalog' / 'crossalpha.duckdb'
con = duckdb.connect(str(db), read_only=True)
try:
    row = con.execute('''
        SELECT
            protocol,
            shadow_only,
            core_protocol_mutated,
            state_band,
            shadow_risk_multiplier,
            data_confidence,
            as_of
        FROM state_engine.shadow_v01
        ORDER BY as_of DESC
        LIMIT 1
    ''').fetchone()
finally:
    con.close()

if row is None:
    raise SystemExit('STATE SHADOW VIEW EMPTY')
protocol, shadow_only, mutated, band, multiplier, confidence, as_of = row
print('protocol=', protocol)
print('shadow_only=', shadow_only)
print('core_protocol_mutated=', mutated)
print('state_band=', band)
print('shadow_risk_multiplier=', multiplier)
print('data_confidence=', confidence)
print('as_of=', as_of)
assert protocol == 'CROSSALPHA_STATE_SHADOW_V0_1'
assert bool(shadow_only) is True
assert bool(mutated) is False
assert float(multiplier) in {0.5, 0.75, 1.0}
PY
fi

if [[ -f "$FREEZE" ]]; then
  PAPER_HASH_AFTER="$(sha256sum "$FREEZE" | awk '{print $1}')"
else
  PAPER_HASH_AFTER="MISSING"
fi

echo "paper_hash_after=$PAPER_HASH_AFTER"
if [[ -z "$FAIL_STEP" && "$PAPER_HASH_BEFORE" != "$PAPER_HASH_AFTER" ]]; then
  FAIL_STEP="7. verify Frozen B3 paper isolation"
  FAIL_CODE=97
  echo "ERROR: Frozen B3 paper freeze hash changed during State setup"
fi
if [[ -z "$FAIL_STEP" ]]; then
  echo "Frozen B3 paper isolation: PASS"
fi

if [[ -z "$FAIL_STEP" ]]; then
  run_critical "8. reinstall materializer with state fault isolation" bash scripts/install_materializer_timer.sh || true
fi

run_step "9. materializer timer status" bash -lc 'systemctl --user status crossalpha-materializer.timer --no-pager || true'
run_step "10. recent materializer log" bash -lc 'journalctl --user -u crossalpha-materializer.service -n 80 --no-pager || true'
run_step "11. frozen B3 paper status after state" bash -lc 'source .venv/bin/activate && crossalpha-free-paper-status || true'

if [[ -f "$FREEZE" ]]; then
  PAPER_HASH_FINAL="$(sha256sum "$FREEZE" | awk '{print $1}')"
else
  PAPER_HASH_FINAL="MISSING"
fi

echo
echo "=================================================="
echo "STATE SHADOW SETUP SUMMARY"
echo "=================================================="
if [[ -n "$FAIL_STEP" ]]; then
  echo "RESULT=FAILED"
  echo "FIRST_FAILED_STEP=$FAIL_STEP"
  echo "FIRST_FAILED_EXIT_CODE=$FAIL_CODE"
else
  echo "RESULT=PASS"
fi
echo "PAPER_HASH_BEFORE=$PAPER_HASH_BEFORE"
echo "PAPER_HASH_FINAL=$PAPER_HASH_FINAL"
echo "LOG=$LOG"
echo "LATEST_LOG=$LATEST"
echo

tail -n 140 "$LOG" || true

# Interactive safety: always return 0. Inspect RESULT= above for the real outcome.
exit 0
