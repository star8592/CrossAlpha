#!/usr/bin/env bash
set -u

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

LOG="/tmp/crossalpha_latest_diagnose.log"
: > "$LOG"

run_step() {
  local name="$1"
  shift
  echo
  echo "=================================================="
  echo "$name"
  echo "=================================================="
  echo "+ $*"
  "$@" 2>&1 | tee -a "$LOG"
  local rc=${PIPESTATUS[0]}
  echo "EXIT_CODE=$rc" | tee -a "$LOG"
  return 0
}

echo "CrossAlpha focused diagnostics"
echo "repo=$REPO_DIR"
echo "head=$(git rev-parse --short HEAD 2>/dev/null || true)"
echo "log=$LOG"

run_step "1. PYTEST" pytest -q
run_step "2. STABLECOIN V3 MATERIALIZATION" crossalpha materialize-observatory
run_step "3. STABLECOIN STATE" crossalpha stablecoin-state --top-chains 5
run_step "4. B0-B7 BASELINES" crossalpha-free-baselines --start 2010-06-01 --end 2026-09-01 --cost-bps 5

echo
echo "=================================================="
echo "DIAGNOSTIC SUMMARY"
echo "=================================================="
grep -E "FAILED|ERROR|Traceback|Exception|EXIT_CODE=|passed in|canonical_schema_version|chain_coverage_ratio|DESCRIPTIVE_BASELINES_ONLY" "$LOG" | tail -n 120 || true

echo
echo "Full log: $LOG"
