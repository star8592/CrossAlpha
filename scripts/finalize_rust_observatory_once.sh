#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

DATA_ROOT="${CROSSALPHA_DATA_DIR:-}"
UNIT="$HOME/.config/systemd/user/crossalpha-observatory.service"
RELEASE_BINARY="$REPO_DIR/target/release/crossalpha-rs"
LOG_PREFIX="[CrossAlpha Rust Observatory]"

log() {
  printf '\n%s %s\n' "$LOG_PREFIX" "$*"
}

fail_summary() {
  rc=$?
  trap - ERR
  printf '\n%s FAILED rc=%s\n' "$LOG_PREFIX" "$rc" >&2
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
log "1/10 Rust format gate"
cargo fmt --all -- --check

log "2/10 Rust clippy gate"
cargo clippy --workspace --all-targets -- -D warnings

log "3/10 Rust tests"
cargo test --workspace

log "4/10 Build debug binary"
cargo build -p crossalpha-cli

log "5/10 Full health Python/Rust parity"
.venv/bin/python scripts/verify_rust_observatory_health_parity.py \
  --data-root "$DATA_ROOT"

log "6/10 Live-health Python/Rust parity"
.venv/bin/python scripts/verify_rust_observatory_live_health_parity.py \
  --data-root "$DATA_ROOT"

log "7/10 Real provider dry-run contract"
.venv/bin/python scripts/verify_rust_observatory_collectors.py

log "8/10 Debug-binary isolated shadow write"
.venv/bin/python scripts/verify_rust_observatory_shadow_write.py

log "9/10 Release build and release shadow write"
cargo build --release -p crossalpha-cli
.venv/bin/python scripts/verify_rust_observatory_shadow_write.py \
  --rust-binary "$RELEASE_BINARY"

log "10/10 Guarded production systemd cutover"
bash scripts/cutover_rust_observatory_service.sh \
  --activate \
  --data-root "$DATA_ROOT"

log "Post-cutover verification"
systemctl --user is-active --quiet crossalpha-observatory.service

if ! grep -Fq "$RELEASE_BINARY observatory-run" "$UNIT"; then
  echo "$LOG_PREFIX systemd unit does not point at Rust release binary after cutover." >&2
  exit 1
fi

"$RELEASE_BINARY" observatory-live-health "$DATA_ROOT" --no-write-report >/tmp/crossalpha-rust-live-health.json
"$RELEASE_BINARY" observatory-health "$DATA_ROOT" --no-write-report >/tmp/crossalpha-rust-full-health.json

python - <<'PY'
import json
from pathlib import Path
for path in [Path('/tmp/crossalpha-rust-live-health.json'), Path('/tmp/crossalpha-rust-full-health.json')]:
    report = json.loads(path.read_text())
    if not report.get('ok'):
        raise SystemExit(f'health failed: {path}: {report}')
print('post_cutover_health=true')
PY

if ! systemctl --user show -p MainPID --value crossalpha-observatory.service | grep -Eq '^[1-9][0-9]*$'; then
  echo "$LOG_PREFIX systemd MainPID is invalid." >&2
  exit 1
fi

if systemctl --user status crossalpha-observatory.service --no-pager | grep -q 'scripts/collect_loop.py'; then
  echo "$LOG_PREFIX Python collector is still active after cutover." >&2
  exit 1
fi

trap - ERR

printf '\n============================================================\n'
printf 'CrossAlpha Rust Observatory: SUCCESS\n'
printf 'data_root=%s\n' "$DATA_ROOT"
printf 'binary=%s\n' "$RELEASE_BINARY"
printf 'service=active\n'
printf 'live_health=true\n'
printf 'full_health=true\n'
printf 'rollback=bash scripts/rollback_rust_observatory_service.sh\n'
printf '============================================================\n'
