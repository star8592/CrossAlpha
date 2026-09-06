#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
META="$UNIT_DIR/crossalpha-daemon.pre-cutover.meta"
ACTIVATE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --activate) ACTIVATE=true; shift ;;
    -h|--help)
      echo "Usage: bash scripts/rollback_unified_rust_daemon.sh --activate"
      exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ "$ACTIVATE" == true ]] || { echo "Refusing rollback without explicit --activate." >&2; exit 2; }
[[ -f "$META" ]] || { echo "Rollback metadata missing: $META" >&2; exit 2; }

# shellcheck disable=SC1090
source "$META"

systemctl --user disable --now crossalpha-daemon.service >/dev/null 2>&1 || true

restore_timer() {
  local unit="$1"
  local enabled="$2"
  local active="$3"
  if [[ "$enabled" == true ]]; then
    systemctl --user enable "$unit" >/dev/null
  else
    systemctl --user disable "$unit" >/dev/null 2>&1 || true
  fi
  if [[ "$active" == true ]]; then
    systemctl --user start "$unit"
  fi
}

if [[ "${observatory_enabled:-false}" == true ]]; then
  systemctl --user enable crossalpha-observatory.service >/dev/null
else
  systemctl --user disable crossalpha-observatory.service >/dev/null 2>&1 || true
fi
if [[ "${observatory_active:-false}" == true ]]; then
  systemctl --user start crossalpha-observatory.service
fi

restore_timer \
  crossalpha-materializer.timer \
  "${materializer_enabled:-false}" \
  "${materializer_active:-false}"
restore_timer \
  crossalpha-state-v03.timer \
  "${state_v03_enabled:-false}" \
  "${state_v03_active:-false}"
restore_timer \
  crossalpha-state-v04.timer \
  "${state_v04_enabled:-false}" \
  "${state_v04_active:-false}"

systemctl --user daemon-reload

echo "Unified Rust daemon stopped and previous service state restored."
echo "observatory_active=${observatory_active:-false} observatory_enabled=${observatory_enabled:-false}"
echo "materializer_active=${materializer_active:-false} materializer_enabled=${materializer_enabled:-false}"
echo "state_v03_active=${state_v03_active:-false} state_v03_enabled=${state_v03_enabled:-false}"
echo "state_v04_active=${state_v04_active:-false} state_v04_enabled=${state_v04_enabled:-false}"
echo "data_root=${data_root:-unknown}"
echo "repo_dir=$REPO_DIR"
