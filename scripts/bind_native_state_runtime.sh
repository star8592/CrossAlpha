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

[[ "$ACTIVATE" == true ]] || { echo "Refusing State runtime binding without explicit --activate." >&2; exit 2; }

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
python3 - "$ACCEPTANCE" <<'PY'
import json, sys
v=json.load(open(sys.argv[1], encoding="utf-8"))
if v.get("daemon_cutover_allowed") is not True:
    raise SystemExit("State runtime binding refused: daemon_cutover_allowed is not true")
if v.get("cargo_lock", {}).get("ok") is not True:
    raise SystemExit("State runtime binding refused: Cargo.lock gate is not green")
if v.get("git_clean", {}).get("ok") is not True:
    raise SystemExit("State runtime binding refused: git worktree is not clean")
PY

cd "$REPO_DIR"
cargo build --workspace --release
BINARY="$REPO_DIR/target/release/crossalpha-state-rs"

# Freeze is idempotent for a valid existing legacy freeze. The native sidecar binding
# is immutable and includes Cargo.lock plus the complete State/CLI source hash graph.
echo "==> Binding State V0.3 to Rust runtime"
"$BINARY" v03 freeze --data-root "$DATA_ROOT"
"$BINARY" v03 integrity --data-root "$DATA_ROOT" | python3 -c 'import json,sys; v=json.load(sys.stdin); print(json.dumps(v,indent=2)); raise SystemExit(0 if v.get("cycle_enabled") is True else 2)'

echo "==> Binding State V0.4 to Rust runtime"
"$BINARY" v04 freeze --data-root "$DATA_ROOT"
"$BINARY" v04 integrity --data-root "$DATA_ROOT" | python3 -c 'import json,sys; v=json.load(sys.stdin); print(json.dumps(v,indent=2)); raise SystemExit(0 if v.get("cycle_enabled") is True else 2)'

echo "Native State runtime bindings are valid."
echo "Next full cutover: bash scripts/cutover_unified_rust_daemon.sh --activate --data-root '$DATA_ROOT' --include-state-v03 --include-state-v04"
