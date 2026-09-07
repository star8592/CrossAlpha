#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_ROOT="${CROSSALPHA_DATA_DIR:-}"
ACTIVATE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --activate) ACTIVATE=true; shift ;;
    --data-root)
      [[ $# -ge 2 ]] || { echo "--data-root requires a value" >&2; exit 2; }
      DATA_ROOT="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: bash scripts/bind_native_state_runtime.sh --activate [--data-root PATH]"
      exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ "$ACTIVATE" == true ]] || {
  echo "Refusing native runtime binding without explicit --activate." >&2
  exit 2
}

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
[[ -n "$DATA_ROOT" ]] || { echo "CROSSALPHA_DATA_DIR is not set" >&2; exit 2; }
[[ "$DATA_ROOT" == /* ]] || DATA_ROOT="$REPO_DIR/$DATA_ROOT"
DATA_ROOT="$(realpath -m "$DATA_ROOT")"

ACCEPTANCE="$DATA_ROOT/manifests/rust_migration_acceptance.json"
[[ -f "$ACCEPTANCE" ]] || { echo "Acceptance report missing: $ACCEPTANCE" >&2; exit 2; }
grep -Eq '"daemon_cutover_allowed"[[:space:]]*:[[:space:]]*true' "$ACCEPTANCE" || {
  echo "Native runtime binding refused: daemon_cutover_allowed is not true" >&2
  exit 2
}

cd "$REPO_DIR"
[[ -f Cargo.lock ]] || { echo "Native runtime binding refused: Cargo.lock missing" >&2; exit 2; }
git ls-files --error-unmatch Cargo.lock >/dev/null 2>&1 || {
  echo "Native runtime binding refused: Cargo.lock is not tracked" >&2
  exit 2
}
[[ -z "$(git status --porcelain)" ]] || {
  echo "Native runtime binding refused: git worktree is not clean" >&2
  git status --short >&2
  exit 2
}

# This migration is allowed to bind only already-frozen experiments. Never
# create a legacy research freeze as a side effect of language migration.
for required in \
  "$DATA_ROOT/research/state_v02/freeze.json" \
  "$DATA_ROOT/research/state_v03/freeze.json" \
  "$DATA_ROOT/research/state_v04/freeze.json" \
  "$DATA_ROOT/research/free_v01/paper/freeze.json" \
  "$DATA_ROOT/research/free_v01/state_ab_v01/freeze.json" \
  "$DATA_ROOT/research/outcome_linkage_v01/freeze.json"; do
  [[ -f "$required" ]] || {
    echo "Native runtime binding refused: legacy freeze missing: $required" >&2
    exit 2
  }
done

cargo build --workspace --release
STATE="$REPO_DIR/target/release/crossalpha-state-rs"
PAPER="$REPO_DIR/target/release/crossalpha-paper-rs"
AB="$REPO_DIR/target/release/crossalpha-ab-rs"
OUTCOME="$REPO_DIR/target/release/crossalpha-outcome-rs"
for binary in "$STATE" "$PAPER" "$AB" "$OUTCOME"; do
  [[ -x "$binary" ]] || { echo "Native binary missing: $binary" >&2; exit 2; }
done

bind_state() {
  local version="$1"
  echo "==> Binding State $version to Rust runtime"
  "$STATE" "$version" freeze --data-root "$DATA_ROOT"
  "$STATE" "$version" integrity --data-root "$DATA_ROOT" \
    | grep -Eq '"cycle_enabled"[[:space:]]*:[[:space:]]*true'
}

# Prove the immutable Python-era V0.2 ledger before the Rust handoff. The
# attestation snapshots file bytes; it never rewrites historical observations.
PYTHON="$REPO_DIR/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON=python3
"$PYTHON" scripts/attest_state_v02_legacy_ledger.py --data-root "$DATA_ROOT"

# Bind predecessor State layers first because later freezes reference them.
bind_state v02
bind_state v03
bind_state v04

echo "==> Binding Frozen B3 Paper to Rust runtime"
"$PAPER" bind --data-root "$DATA_ROOT"
"$PAPER" integrity --data-root "$DATA_ROOT"

echo "==> Binding State A/B to Rust runtime"
"$AB" bind --data-root "$DATA_ROOT"
"$AB" integrity --data-root "$DATA_ROOT"

echo "==> Binding Outcome Linkage to Rust runtime"
"$OUTCOME" bind --data-root "$DATA_ROOT"
"$OUTCOME" integrity --data-root "$DATA_ROOT"

echo "All production research runtimes are Rust-bound and integrity-valid."
echo "Next full cutover: bash scripts/cutover_unified_rust_daemon.sh --activate --data-root '$DATA_ROOT' --include-state-v02 --include-state-v03 --include-state-v04"
echo "Then install Rust Paper/A-B/Outcome timers with scripts/install_free_paper_user_services.sh and scripts/install_outcome_linkage_user_service.sh"
