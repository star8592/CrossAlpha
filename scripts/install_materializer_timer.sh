#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
SERVICE_SRC="$REPO_DIR/deploy/systemd/crossalpha-materializer.service"
TIMER_SRC="$REPO_DIR/deploy/systemd/crossalpha-materializer.timer"
SERVICE_DST="$UNIT_DIR/crossalpha-materializer.service"
TIMER_DST="$UNIT_DIR/crossalpha-materializer.timer"
DATA_ROOT="${CROSSALPHA_DATA_DIR:-}"
ACTIVATE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --activate) ACTIVATE=true; shift ;;
    --data-root)
      [[ $# -ge 2 ]] || { echo "--data-root requires a value" >&2; exit 2; }
      DATA_ROOT="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: bash scripts/install_materializer_timer.sh --activate [--data-root PATH]"
      exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ "$ACTIVATE" != true ]]; then
  echo "Refusing to activate production materialization without explicit --activate." >&2
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
[[ -n "$DATA_ROOT" ]] || { echo "CROSSALPHA_DATA_DIR is not set" >&2; exit 2; }
[[ "$DATA_ROOT" == /* ]] || DATA_ROOT="$REPO_DIR/$DATA_ROOT"
DATA_ROOT="$(realpath -m "$DATA_ROOT")"

cd "$REPO_DIR"
cargo build --release -p crossalpha-cli --bin crossalpha-materialize-rs

mkdir -p "$UNIT_DIR"
sed -e "s|@REPO_DIR@|$REPO_DIR|g" -e "s|@DATA_ROOT@|$DATA_ROOT|g" "$SERVICE_SRC" > "$SERVICE_DST"
cp "$TIMER_SRC" "$TIMER_DST"

systemctl --user daemon-reload
systemctl --user enable --now crossalpha-materializer.timer
systemctl --user start crossalpha-materializer.service

echo "Installed Rust materializer: $SERVICE_DST"
echo "Installed timer:            $TIMER_DST"
echo "Timer:   systemctl --user status crossalpha-materializer.timer"
echo "Service: systemctl --user status crossalpha-materializer.service"
echo "Logs:    journalctl --user -u crossalpha-materializer.service -n 100 --no-pager"
