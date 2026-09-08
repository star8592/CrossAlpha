#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

DATA_ROOT="${CROSSALPHA_DATA_DIR:-}"
UNIT="$HOME/.config/systemd/user/crossalpha-observatory.service"
RELEASE_BINARY="$REPO_DIR/target/release/crossalpha-rs"
LOG_PREFIX="[CrossAlpha Rust Observatory]"
CUTOVER_COMPLETED=false

log() {
  printf '\n%s %s\n' "$LOG_PREFIX" "$*"
}

fail_summary() {
  rc=$?
  trap - ERR
  printf '\n%s FAILED rc=%s\n' "$LOG_PREFIX" "$rc" >&2

  if [[ "$CUTOVER_COMPLETED" == true ]]; then
    printf '%s Post-cutover verification failed; restoring Python service automatically.\n' "$LOG_PREFIX" >&2
    bash scripts/rollback_rust_observatory_service.sh >&2 || true
  fi

  printf '%s Current service status:\n' "$LOG_PREFIX" >&2
  systemctl --user status crossalpha-observatory.service --no-pager >&2 || true
  printf '%s Recent service logs:\n' "$LOG_PREFIX" >&2
  journalctl --user -u crossalpha-observatory.service -n 60 --no-pager >&2 || true
  exit "$rc"
}
trap fail_summary ERR

resolve_data_root() {
  if [[ -n "$DATA_ROOT" ]]; then
    return
  fi

  if [[ -f "$REPO_DIR/.env" ]]; then
    local line
    line="$(grep -E '^[[:space:]]*CROSSALPHA_DATA_DIR[[:space:]]*=' "$REPO_DIR/.env" | tail -n 1 || true)"
    if [[ -n "$line" ]]; then
      DATA_ROOT="${line#*=}"
      DATA_ROOT="${DATA_ROOT#${DATA_ROOT%%[![:space:]]*}}"
      DATA_ROOT="${DATA_ROOT%${DATA_ROOT##*[![:space:]]}}"
      if [[ "$DATA_ROOT" == \"*\" && "$DATA_ROOT" == *\" ]]; then
        DATA_ROOT="${DATA_ROOT:1:${#DATA_ROOT}-2}"
      elif [[ "$DATA_ROOT" == \'*\' && "$DATA_ROOT" == *\' ]]; then
        DATA_ROOT="${DATA_ROOT:1:${#DATA_ROOT}-2}"
      fi
    fi
  fi

  if [[ -z "$DATA_ROOT" && -d /mnt/disk2/CrossAlphaData/manifests ]]; then
    DATA_ROOT="/mnt/disk2/CrossAlphaData"
  fi

  if [[ -z "$DATA_ROOT" ]]; then
    echo "$LOG_PREFIX Unable to determine CROSSALPHA_DATA_DIR from environment, .env, or /mnt/disk2/CrossAlphaData." >&2
    exit 2
  fi

  if [[ "$DATA_ROOT" != /* ]]; then
    DATA_ROOT="$REPO_DIR/$DATA_ROOT"
  fi
  DATA_ROOT="$(realpath -m "$DATA_ROOT")"
}

resolve_data_root

if [[ ! -d "$DATA_ROOT/manifests" ]]; then
  echo "$LOG_PREFIX Invalid data root: $DATA_ROOT" >&2
  exit 2
fi
if [[ ! -x "$REPO_DIR/.venv/bin/python" ]]; then
  echo "$LOG_PREFIX Python venv missing: $REPO_DIR/.venv/bin/python" >&2
  exit 2
fi
if [[ ! -f "$UNIT" ]]; then
  echo "$LOG_PREFIX Existing Observatory systemd unit missing: $UNIT" >&2
  exit 2
fi

log "data_root=$DATA_ROOT"
log "1/9 format"
cargo fmt --all -- --check

log "2/9 clippy"
cargo clippy --workspace --all-targets -- -D warnings

log "3/9 tests"
cargo test --workspace

log "4/9 debug build"
cargo build -p crossalpha-cli

log "5/9 full-health parity"
.venv/bin/python scripts/verify_rust_observatory_health_parity.py \
  --data-root "$DATA_ROOT"

log "6/9 live-health parity"
.venv/bin/python scripts/verify_rust_observatory_live_health_parity.py \
  --data-root "$DATA_ROOT"

log "7/9 real-provider dry-run"
.venv/bin/python scripts/verify_rust_observatory_collectors.py

log "8/9 isolated shadow write"
.venv/bin/python scripts/verify_rust_observatory_shadow_write.py

if grep -Fq "$RELEASE_BINARY observatory-run" "$UNIT"; then
  log "9/9 Rust service already installed; verify only"
  CUTOVER_COMPLETED=true
else
  log "9/9 guarded production cutover"
  bash scripts/cutover_rust_observatory_service.sh \
    --activate \
    --data-root "$DATA_ROOT"
  CUTOVER_COMPLETED=true
fi

log "post-cutover verification"
systemctl --user is-active --quiet crossalpha-observatory.service

if [[ ! -x "$RELEASE_BINARY" ]]; then
  echo "$LOG_PREFIX Rust release binary missing after cutover: $RELEASE_BINARY" >&2
  false
fi

if ! grep -Fq "$RELEASE_BINARY observatory-run" "$UNIT"; then
  echo "$LOG_PREFIX systemd unit does not point at Rust release binary after cutover." >&2
  false
fi

LIVE_REPORT="$(mktemp /tmp/crossalpha-rust-live-health.XXXXXX.json)"
FULL_REPORT="$(mktemp /tmp/crossalpha-rust-full-health.XXXXXX.json)"
trap 'rm -f "$LIVE_REPORT" "$FULL_REPORT"' EXIT

"$RELEASE_BINARY" observatory-live-health "$DATA_ROOT" --no-write-report >"$LIVE_REPORT"
"$RELEASE_BINARY" observatory-health "$DATA_ROOT" --no-write-report >"$FULL_REPORT"

.venv/bin/python - "$LIVE_REPORT" "$FULL_REPORT" <<'PY'
import json
import sys
from pathlib import Path
for raw in sys.argv[1:]:
    path = Path(raw)
    report = json.loads(path.read_text())
    if not report.get("ok"):
        raise SystemExit(f"health failed: {path}: {report}")
print("post_cutover_health=true")
PY

MAIN_PID="$(systemctl --user show -p MainPID --value crossalpha-observatory.service)"
if [[ ! "$MAIN_PID" =~ ^[1-9][0-9]*$ ]]; then
  echo "$LOG_PREFIX systemd MainPID is invalid: $MAIN_PID" >&2
  false
fi

if systemctl --user status crossalpha-observatory.service --no-pager | grep -q 'scripts/collect_loop.py'; then
  echo "$LOG_PREFIX Python collector is still active after cutover." >&2
  false
fi

if ! ps -p "$MAIN_PID" -o args= | grep -Fq "$RELEASE_BINARY observatory-run"; then
  echo "$LOG_PREFIX MainPID is not the Rust observatory-run process." >&2
  ps -p "$MAIN_PID" -o pid=,args= >&2 || true
  false
fi

trap - ERR

printf '\n============================================================\n'
printf 'CrossAlpha Rust Observatory: SUCCESS\n'
printf 'data_root=%s\n' "$DATA_ROOT"
printf 'binary=%s\n' "$RELEASE_BINARY"
printf 'main_pid=%s\n' "$MAIN_PID"
printf 'service=active\n'
printf 'live_health=true\n'
printf 'full_health=true\n'
printf 'rollback=bash scripts/rollback_rust_observatory_service.sh\n'
printf '============================================================\n'
