#!/usr/bin/env bash
set -u -o pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="/tmp/crossalpha_free_paper_diagnose_${STAMP}.log"
LATEST="/tmp/crossalpha_free_paper_diagnose_latest.log"

exec > >(tee -a "$LOG") 2>&1
ln -sfn "$LOG" "$LATEST"

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
  if [[ -n "$FAIL_STEP" ]]; then
    return 1
  fi
  return 0
}

TODAY_UTC="$(date -u +%F)"

if [[ $- == *e* ]]; then
  SHELL_ERREXIT=on
else
  SHELL_ERREXIT=off
fi

echo "CrossAlpha frozen paper safe diagnostic"
echo "repo=$REPO_DIR"
echo "today_utc=$TODAY_UTC"
echo "log=$LOG"
echo "shell_errexit=$SHELL_ERREXIT"

echo
python --version || true
git rev-parse --short HEAD || true

run_critical "1. editable install" python -m pip install -e ".[dev]" || true
if [[ -z "$FAIL_STEP" ]]; then
  run_critical "2. paper/final tests" pytest -q tests/test_free_paper.py tests/test_free_paper_guard.py tests/test_free_final_evaluation.py || true
fi
if [[ -z "$FAIL_STEP" ]]; then
  run_critical "3. freeze protocol" crossalpha-free-paper-freeze --historical-start 2010-06-01 --historical-end 2026-09-01 || true
fi
if [[ -z "$FAIL_STEP" ]]; then
  run_critical "4. paper status" crossalpha-free-paper-status || true
fi
if [[ -z "$FAIL_STEP" ]]; then
  run_critical "5. refresh current free core" crossalpha-free-paper-refresh --start 2010-06-01 --end "$TODAY_UTC" || true
fi
if [[ -z "$FAIL_STEP" ]]; then
  run_critical "6. ledger integrity" python scripts/check_free_paper_integrity.py || true
fi
if [[ -z "$FAIL_STEP" ]]; then
  run_critical "7. prospective mark" crossalpha-free-paper-mark --end "$TODAY_UTC" || true
fi

run_step "8. paper timers currently known to systemd" bash -lc 'systemctl --user list-timers --all | grep crossalpha-free-paper || true'
run_step "9. paper daily timer status" bash -lc 'systemctl --user status crossalpha-free-paper-daily.timer --no-pager || true'
run_step "10. paper weekly timer status" bash -lc 'systemctl --user status crossalpha-free-paper-weekly.timer --no-pager || true'

echo
echo "=================================================="
echo "DIAGNOSTIC SUMMARY"
echo "=================================================="
if [[ -n "$FAIL_STEP" ]]; then
  echo "RESULT=FAILED"
  echo "FIRST_FAILED_STEP=$FAIL_STEP"
  echo "FIRST_FAILED_EXIT_CODE=$FAIL_CODE"
else
  echo "RESULT=PASS"
fi
echo "LOG=$LOG"
echo "LATEST_LOG=$LATEST"
echo

echo "Last 120 log lines:"
tail -n 120 "$LOG" || true

# Safety rule for interactive use: always return success to the caller. The
# real diagnostic result is reported above and persisted in the log. This
# prevents an interactive parent shell with `set -e` from closing its terminal.
exit 0
