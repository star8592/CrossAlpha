#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

DATA_ROOT="${CROSSALPHA_DATA_DIR:-}"
EXPECTED_BRANCH="${CROSSALPHA_AUTOPILOT_BRANCH:-feat/rust-core-v01}"
REMOTE="${CROSSALPHA_AUTOPILOT_REMOTE:-origin}"
AUTO_PUSH=false
SKIP_LIVE=false
SYNC=true
ACTIVATE_PRODUCTION=false
SOAK_SECONDS=1800
COMMIT_MESSAGE="Local Rust migration autopilot: validated updates"
LOG_ROOT="$REPO_DIR/.local-runs"

usage() {
  cat <<'EOF'
Usage:
  bash scripts/local_rust_migration_autopilot.sh [options]

Safe default:
  Sync branch, run rustfmt, run the full non-live local R7 suite, create a local
  commit if needed, then run the clean-worktree R7 suite. Nothing is pushed and
  production services are never changed unless explicitly requested.

Options:
  --data-root PATH           CrossAlphaData root (or use CROSSALPHA_DATA_DIR)
  --push                     Push the validated local commit/HEAD to origin
  --skip-live                Skip network/live R7 probes; incompatible with production activation
  --no-sync                  Do not git fetch/pull before qualification
  --branch NAME              Expected local branch (default: feat/rust-core-v01)
  --remote NAME              Git remote used for sync/push (default: origin)
  --commit-message TEXT      Commit message for local validated changes
  --log-root PATH            Local ignored directory for run logs
  --activate-production      After validated push, bind runtimes, cut over the unified
                             Rust daemon, install Rust Paper/Outcome timers, soak, and
                             run post-cutover retirement acceptance. Explicit and guarded.
  --soak-seconds N           Post-cutover soak duration (default: 1800)
  -h, --help                 Show this help

Examples:
  bash scripts/local_rust_migration_autopilot.sh \
    --data-root /mnt/disk2/CrossAlphaData --push

  bash scripts/local_rust_migration_autopilot.sh \
    --data-root /mnt/disk2/CrossAlphaData --push \
    --activate-production --soak-seconds 1800
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --data-root)
      [[ $# -ge 2 ]] || { echo "--data-root requires a value" >&2; exit 2; }
      DATA_ROOT="$2"; shift 2 ;;
    --push) AUTO_PUSH=true; shift ;;
    --skip-live) SKIP_LIVE=true; shift ;;
    --no-sync) SYNC=false; shift ;;
    --branch)
      [[ $# -ge 2 ]] || { echo "--branch requires a value" >&2; exit 2; }
      EXPECTED_BRANCH="$2"; shift 2 ;;
    --remote)
      [[ $# -ge 2 ]] || { echo "--remote requires a value" >&2; exit 2; }
      REMOTE="$2"; shift 2 ;;
    --commit-message)
      [[ $# -ge 2 ]] || { echo "--commit-message requires a value" >&2; exit 2; }
      COMMIT_MESSAGE="$2"; shift 2 ;;
    --log-root)
      [[ $# -ge 2 ]] || { echo "--log-root requires a value" >&2; exit 2; }
      LOG_ROOT="$2"; shift 2 ;;
    --activate-production) ACTIVATE_PRODUCTION=true; AUTO_PUSH=true; shift ;;
    --soak-seconds)
      [[ $# -ge 2 ]] || { echo "--soak-seconds requires a value" >&2; exit 2; }
      SOAK_SECONDS="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$SOAK_SECONDS" =~ ^[0-9]+$ ]] || { echo "--soak-seconds must be a non-negative integer" >&2; exit 2; }
if [[ "$ACTIVATE_PRODUCTION" == true && "$SKIP_LIVE" == true ]]; then
  echo "--activate-production cannot be combined with --skip-live" >&2
  exit 2
fi

if [[ -z "$DATA_ROOT" && -f "$REPO_DIR/.env" ]]; then
  line="$(grep -E '^[[:space:]]*CROSSALPHA_DATA_DIR[[:space:]]*=' "$REPO_DIR/.env" | tail -n 1 || true)"
  if [[ -n "$line" ]]; then
    DATA_ROOT="${line#*=}"
    DATA_ROOT="${DATA_ROOT#${DATA_ROOT%%[![:space:]]*}}"
    DATA_ROOT="${DATA_ROOT%${DATA_ROOT##*[![:space:]]}}"
    DATA_ROOT="${DATA_ROOT%\"}"; DATA_ROOT="${DATA_ROOT#\"}"
    DATA_ROOT="${DATA_ROOT%\'}"; DATA_ROOT="${DATA_ROOT#\'}"
  fi
fi
[[ -n "$DATA_ROOT" ]] || { echo "CROSSALPHA_DATA_DIR is not set; pass --data-root" >&2; exit 2; }
[[ "$DATA_ROOT" == /* ]] || DATA_ROOT="$REPO_DIR/$DATA_ROOT"
DATA_ROOT="$(realpath -m "$DATA_ROOT")"
export CROSSALPHA_DATA_DIR="$DATA_ROOT"

PY="$REPO_DIR/.venv/bin/python"
[[ -x "$PY" ]] || { echo "Python venv missing: $PY" >&2; exit 2; }
[[ -d "$DATA_ROOT/manifests" ]] || { echo "CrossAlphaData manifests missing: $DATA_ROOT/manifests" >&2; exit 2; }

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "Not inside a git worktree" >&2; exit 2; }
CURRENT_BRANCH="$(git branch --show-current)"
[[ -n "$CURRENT_BRANCH" ]] || { echo "Detached HEAD is not allowed" >&2; exit 2; }
[[ "$CURRENT_BRANCH" == "$EXPECTED_BRANCH" ]] || {
  echo "Refusing autopilot on branch '$CURRENT_BRANCH'; expected '$EXPECTED_BRANCH'" >&2
  exit 2
}
git remote get-url "$REMOTE" >/dev/null 2>&1 || { echo "Git remote not found: $REMOTE" >&2; exit 2; }

for marker in MERGE_HEAD REBASE_HEAD CHERRY_PICK_HEAD REVERT_HEAD; do
  if git rev-parse -q --verify "$marker" >/dev/null 2>&1; then
    echo "Git operation already in progress ($marker); resolve it before autopilot" >&2
    exit 2
  fi
done

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
RUN_DIR="$LOG_ROOT/$RUN_ID"
mkdir -p "$RUN_DIR"
SUMMARY="$RUN_DIR/summary.txt"
CURRENT_STEP="bootstrap"
CURRENT_LOG="$RUN_DIR/00_bootstrap.log"
STEP_NO=0

exec > >(tee -a "$CURRENT_LOG") 2>&1

echo "CrossAlpha Local Rust Migration Autopilot"
echo "========================================"
echo "run_id=$RUN_ID"
echo "repo=$REPO_DIR"
echo "branch=$CURRENT_BRANCH"
echo "remote=$REMOTE"
echo "data_root=$DATA_ROOT"
echo "log_dir=$RUN_DIR"
echo "auto_push=$AUTO_PUSH"
echo "skip_live=$SKIP_LIVE"
echo "activate_production=$ACTIVATE_PRODUCTION"
echo

write_summary() {
  local status="$1"
  {
    echo "status=$status"
    echo "run_id=$RUN_ID"
    echo "finished_at=$(date -u +%FT%TZ)"
    echo "branch=$CURRENT_BRANCH"
    echo "head=$(git rev-parse HEAD 2>/dev/null || true)"
    echo "data_root=$DATA_ROOT"
    echo "log_dir=$RUN_DIR"
    echo "current_step=$CURRENT_STEP"
    echo "current_log=$CURRENT_LOG"
    echo "auto_push=$AUTO_PUSH"
    echo "activate_production=$ACTIVATE_PRODUCTION"
  } > "$SUMMARY"
}

on_error() {
  local rc=$?
  trap - ERR
  write_summary "FAILED"
  echo
  echo "============================================================" >&2
  echo "AUTOPILOT FAILED rc=$rc step=$CURRENT_STEP" >&2
  echo "log=$CURRENT_LOG" >&2
  echo "summary=$SUMMARY" >&2
  echo "---------------- last 160 log lines ------------------------" >&2
  tail -n 160 "$CURRENT_LOG" >&2 2>/dev/null || true
  echo "------------------------------------------------------------" >&2
  git status --short >&2 2>/dev/null || true
  exit "$rc"
}
trap on_error ERR

run_step() {
  local name="$1"; shift
  STEP_NO=$((STEP_NO + 1))
  CURRENT_STEP="$name"
  CURRENT_LOG="$RUN_DIR/$(printf '%02d' "$STEP_NO")_${name}.log"
  echo
  echo "==> [$STEP_NO] $name"
  echo "command: $*"
  set +e
  "$@" 2>&1 | tee "$CURRENT_LOG"
  local rc=${PIPESTATUS[0]}
  set -e
  if (( rc != 0 )); then
    return "$rc"
  fi
  echo "<== $name ok"
}

acceptance_value() {
  local key="$1"
  "$PY" - "$DATA_ROOT/manifests/rust_migration_acceptance.json" "$key" <<'PY'
import json, sys
path, key = sys.argv[1], sys.argv[2]
value = json.load(open(path, encoding="utf-8"))
result = value.get(key)
if isinstance(result, bool):
    print(str(result).lower())
elif result is None:
    print("null")
else:
    print(result)
PY
}

if [[ "$SYNC" == true ]]; then
  run_step "git_fetch" git fetch --prune "$REMOTE" "$EXPECTED_BRANCH"
  # Preserve local tracked edits while rebasing onto the latest remote branch.
  # If an actual conflict exists, git exits non-zero and autopilot stops with logs.
  run_step "git_rebase_sync" git pull --rebase --autostash "$REMOTE" "$EXPECTED_BRANCH"
fi

run_step "cargo_fmt" cargo fmt --all

# First pass intentionally skips live probes. It validates compilation, clippy,
# unit/integration tests, deterministic parity, and real-data parity even though
# rustfmt/Cargo.lock/local code may still make the worktree dirty.
run_step "r7_precommit" "$PY" scripts/run_rust_migration_acceptance.py \
  --data-root "$DATA_ROOT" --skip-live

run_step "git_diff_check" git diff --check
run_step "git_stage" git add -A
run_step "git_staged_diff_check" git diff --cached --check

if ! git diff --cached --quiet; then
  run_step "git_commit" git commit -m "$COMMIT_MESSAGE"
else
  echo "No repository changes to commit."
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Worktree is not clean after local commit:" >&2
  git status --short >&2
  false
fi

# Second pass runs on the exact clean commit that can be pushed. By default it
# includes live provider/preflight gates; --skip-live is available for offline use.
if [[ "$SKIP_LIVE" == true ]]; then
  run_step "r7_clean_head" "$PY" scripts/run_rust_migration_acceptance.py \
    --data-root "$DATA_ROOT" --skip-live
else
  run_step "r7_clean_head" "$PY" scripts/run_rust_migration_acceptance.py \
    --data-root "$DATA_ROOT"
fi

DECISION="$(acceptance_value decision)"
DAEMON_ALLOWED="$(acceptance_value daemon_cutover_allowed)"
PYTHON_RETIREMENT_ALLOWED="$(acceptance_value python_retirement_allowed)"
echo "R7 decision=$DECISION"
echo "daemon_cutover_allowed=$DAEMON_ALLOWED"
echo "python_retirement_allowed=$PYTHON_RETIREMENT_ALLOWED"

if [[ "$AUTO_PUSH" == true ]]; then
  if [[ "$SKIP_LIVE" != true && "$DAEMON_ALLOWED" != "true" ]]; then
    echo "Refusing push: clean-head live R7 did not authorize daemon cutover." >&2
    false
  fi
  run_step "git_push" git push "$REMOTE" "$EXPECTED_BRANCH"
  run_step "git_verify_remote_head" bash -lc \
    "test \"\$(git rev-parse HEAD)\" = \"\$(git ls-remote '$REMOTE' 'refs/heads/$EXPECTED_BRANCH' | awk '{print \$1}')\""
fi

if [[ "$ACTIVATE_PRODUCTION" == true ]]; then
  [[ "$AUTO_PUSH" == true ]] || { echo "production activation requires validated push" >&2; false; }
  [[ "$DAEMON_ALLOWED" == "true" ]] || { echo "production activation requires daemon_cutover_allowed=true" >&2; false; }

  run_step "bind_native_runtime" bash scripts/bind_native_state_runtime.sh \
    --activate --data-root "$DATA_ROOT"

  # Re-run the complete gate after binding. Binding mutates only data-root sidecars,
  # never repository source, and all State integrity checks must now remain green.
  run_step "r7_after_binding" "$PY" scripts/run_rust_migration_acceptance.py \
    --data-root "$DATA_ROOT"
  [[ "$(acceptance_value daemon_cutover_allowed)" == "true" ]] || {
    echo "R7 lost daemon cutover authorization after runtime binding" >&2
    false
  }

  run_step "unified_daemon_cutover" bash scripts/cutover_unified_rust_daemon.sh \
    --activate --data-root "$DATA_ROOT" \
    --include-state-v02 --include-state-v03 --include-state-v04

  run_step "install_paper_timers" bash scripts/install_free_paper_user_services.sh
  run_step "install_outcome_timer" bash scripts/install_outcome_linkage_user_service.sh

  CURRENT_STEP="production_soak"
  CURRENT_LOG="$RUN_DIR/$(printf '%02d' $((STEP_NO + 1)))_production_soak.log"
  STEP_NO=$((STEP_NO + 1))
  echo
  echo "==> [$STEP_NO] production_soak seconds=$SOAK_SECONDS"
  elapsed=0
  while (( elapsed < SOAK_SECONDS )); do
    remaining=$((SOAK_SECONDS - elapsed))
    chunk=60
    (( remaining < chunk )) && chunk=$remaining
    sleep "$chunk"
    elapsed=$((elapsed + chunk))
    {
      echo "soak_elapsed=$elapsed/$SOAK_SECONDS at=$(date -u +%FT%TZ)"
      systemctl --user is-active crossalpha-daemon.service || true
      if [[ -f "$DATA_ROOT/manifests/crossalpha_daemon_health.json" ]]; then
        "$PY" - "$DATA_ROOT/manifests/crossalpha_daemon_health.json" <<'PY' || true
import json, sys
v=json.load(open(sys.argv[1], encoding="utf-8"))
print("daemon_status=", v.get("status"), "checked_at=", v.get("checked_at"), "components=", v.get("components"))
PY
      fi
    } | tee -a "$CURRENT_LOG"
    systemctl --user is-active --quiet crossalpha-daemon.service || {
      echo "Unified daemon became inactive during soak" >&2
      false
    }
  done
  echo "<== production_soak ok"

  run_step "r7_post_cutover" "$PY" scripts/run_rust_migration_acceptance.py \
    --data-root "$DATA_ROOT" --post-cutover --minimum-soak-seconds "$SOAK_SECONDS"

  run_step "journal_daemon" bash -lc \
    "journalctl --user -u crossalpha-daemon.service -n 250 --no-pager"
  run_step "systemd_status" bash -lc \
    "systemctl --user status crossalpha-daemon.service --no-pager && systemctl --user list-timers --all | grep crossalpha"

  PYTHON_RETIREMENT_ALLOWED="$(acceptance_value python_retirement_allowed)"
  [[ "$PYTHON_RETIREMENT_ALLOWED" == "true" ]] || {
    echo "Post-cutover R7 did not authorize Python production retirement." >&2
    echo "Unified daemon is left running for diagnosis; use scripts/rollback_unified_rust_daemon.sh --activate for explicit rollback." >&2
    false
  }
  echo "python_retirement_allowed=true"
fi

CURRENT_STEP="complete"
CURRENT_LOG="$RUN_DIR/$(printf '%02d' $((STEP_NO + 1)))_complete.log"
write_summary "PASSED"
trap - ERR

echo
echo "============================================================"
echo "AUTOPILOT PASSED"
echo "head=$(git rev-parse HEAD)"
echo "branch=$CURRENT_BRANCH"
echo "pushed=$AUTO_PUSH"
echo "production_activated=$ACTIVATE_PRODUCTION"
echo "r7_decision=$(acceptance_value decision)"
echo "daemon_cutover_allowed=$(acceptance_value daemon_cutover_allowed)"
echo "python_retirement_allowed=$(acceptance_value python_retirement_allowed)"
echo "logs=$RUN_DIR"
echo "summary=$SUMMARY"
echo "============================================================"
