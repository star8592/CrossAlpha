#!/usr/bin/env bash
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

LOG_ROOT="$REPO_DIR/.local-runs"
REPORTER="$REPO_DIR/scripts/report_local_autopilot_to_github.sh"
AUTOPILOT="$REPO_DIR/scripts/local_rust_migration_autopilot.sh"

command -v gh >/dev/null 2>&1 || {
  echo "GitHub CLI 'gh' is required for automatic ChatGPT report submission." >&2
  echo "Install gh, then run: gh auth login" >&2
  exit 2
}
gh auth status >/dev/null 2>&1 || {
  echo "GitHub CLI is not authenticated." >&2
  echo "One-time setup: gh auth login" >&2
  exit 2
}
[[ -f "$AUTOPILOT" ]] || { echo "Missing autopilot: $AUTOPILOT" >&2; exit 2; }
[[ -f "$REPORTER" ]] || { echo "Missing reporter: $REPORTER" >&2; exit 2; }

mkdir -p "$LOG_ROOT"
WRAP_ID="wrapper-$(date -u +%Y%m%dT%H%M%SZ)-$$"
WRAP_DIR="$LOG_ROOT/$WRAP_ID"
mkdir -p "$WRAP_DIR"
WRAP_LOG="$WRAP_DIR/terminal.log"
WRAP_SUMMARY="$WRAP_DIR/summary.txt"

before_latest="$(find "$LOG_ROOT" -mindepth 1 -maxdepth 1 -type d ! -path "$WRAP_DIR" -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n 1 | cut -d' ' -f2- || true)"

set +e
bash "$AUTOPILOT" "$@" 2>&1 | tee "$WRAP_LOG"
autopilot_rc=${PIPESTATUS[0]}
set -e

after_latest="$(find "$LOG_ROOT" -mindepth 1 -maxdepth 1 -type d ! -path "$WRAP_DIR" -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n 1 | cut -d' ' -f2- || true)"

TARGET_RUN=""
if [[ -n "$after_latest" && "$after_latest" != "$before_latest" ]]; then
  TARGET_RUN="$after_latest"
else
  status="FAILED"
  [[ "$autopilot_rc" == "0" ]] && status="PASSED"
  {
    echo "status=$status"
    echo "run_id=$WRAP_ID"
    echo "finished_at=$(date -u +%FT%TZ)"
    echo "branch=$(git branch --show-current 2>/dev/null || true)"
    echo "head=$(git rev-parse HEAD 2>/dev/null || true)"
    echo "current_step=wrapper_or_early_bootstrap"
    echo "current_log=$WRAP_LOG"
    echo "auto_push=unknown"
    echo "activate_production=unknown"
  } > "$WRAP_SUMMARY"
  TARGET_RUN="$WRAP_DIR"
fi

set +e
bash "$REPORTER" "$TARGET_RUN" "$autopilot_rc"
report_rc=$?
set -e

if (( report_rc != 0 )); then
  echo "Automatic report submission failed (report_rc=$report_rc)." >&2
  echo "Raw logs remain local: $TARGET_RUN" >&2
fi

if (( autopilot_rc != 0 )); then
  exit "$autopilot_rc"
fi
if (( report_rc != 0 )); then
  exit 97
fi

exit 0
