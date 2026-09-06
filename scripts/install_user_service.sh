#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$REPO_DIR/deploy/systemd/crossalpha-observatory.service"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_DST="$UNIT_DIR/crossalpha-observatory.service"
BINARY="$REPO_DIR/target/release/crossalpha-rs"
DATA_ROOT="${CROSSALPHA_DATA_DIR:-}"

if [[ -z "$DATA_ROOT" && -f "$REPO_DIR/.env" ]]; then
  line="$(grep -E '^[[:space:]]*CROSSALPHA_DATA_DIR[[:space:]]*=' "$REPO_DIR/.env" | tail -n 1 || true)"
  if [[ -n "$line" ]]; then
    DATA_ROOT="${line#*=}"
    DATA_ROOT="${DATA_ROOT#${DATA_ROOT%%[![:space:]]*}}"
    DATA_ROOT="${DATA_ROOT%${DATA_ROOT##*[![:space:]]}}"
    DATA_ROOT="${DATA_ROOT%\"}"
    DATA_ROOT="${DATA_ROOT#\"}"
    DATA_ROOT="${DATA_ROOT%\'}"
    DATA_ROOT="${DATA_ROOT#\'}"
  fi
fi

if [[ -z "$DATA_ROOT" ]]; then
  DATA_ROOT="$REPO_DIR/data"
elif [[ "$DATA_ROOT" != /* ]]; then
  DATA_ROOT="$REPO_DIR/$DATA_ROOT"
fi
DATA_ROOT="$(realpath -m "$DATA_ROOT")"

cd "$REPO_DIR"
cargo build --release -p crossalpha-cli --bin crossalpha-rs

mkdir -p "$UNIT_DIR"
sed \
  -e "s|@REPO_DIR@|$REPO_DIR|g" \
  -e "s|@DATA_ROOT@|$DATA_ROOT|g" \
  "$UNIT_SRC" > "$UNIT_DST"

systemctl --user daemon-reload
systemctl --user enable --now crossalpha-observatory.service

echo "Installed Rust Observatory: $UNIT_DST"
echo "Binary:    $BINARY"
echo "Data root: $DATA_ROOT"
echo "Status:    systemctl --user status crossalpha-observatory.service"
echo "Logs:      journalctl --user -u crossalpha-observatory.service -f"
