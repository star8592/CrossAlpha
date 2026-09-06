#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if systemctl --user is-active --quiet crossalpha-daemon.service \
  || systemctl --user is-enabled --quiet crossalpha-daemon.service; then
  echo "Refusing standalone State V0.2 install: unified Rust daemon already owns State runtime." >&2
  exit 2
fi

DATA_ROOT="${CROSSALPHA_DATA_DIR:-}"
if [[ -z "$DATA_ROOT" && -f .env ]]; then
  line="$(grep -E '^[[:space:]]*CROSSALPHA_DATA_DIR[[:space:]]*=' .env | tail -n 1 || true)"
  [[ -n "$line" ]] && DATA_ROOT="${line#*=}"
fi
[[ -n "$DATA_ROOT" ]] || { echo "CROSSALPHA_DATA_DIR is not set" >&2; exit 2; }
[[ "$DATA_ROOT" == /* ]] || DATA_ROOT="$REPO_DIR/$DATA_ROOT"
DATA_ROOT="$(realpath -m "$DATA_ROOT")"

cargo build --workspace --release
BINARY="$REPO_DIR/target/release/crossalpha-state-rs"
"$BINARY" v02 integrity --data-root "$DATA_ROOT" >/dev/null

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/crossalpha-state-v02.service" <<EOF
[Unit]
Description=CrossAlpha State V0.2 native Rust descriptive cycle
After=network-online.target crossalpha-observatory.service
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_DIR
Environment=CROSSALPHA_DATA_DIR=$DATA_ROOT
ExecStart=$BINARY v02 cycle --data-root $DATA_ROOT
TimeoutStartSec=5min
EOF

cat > "$UNIT_DIR/crossalpha-state-v02.timer" <<'EOF'
[Unit]
Description=Run CrossAlpha State V0.2 native Rust cycle every 15 minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=15min
AccuracySec=30s
Unit=crossalpha-state-v02.service

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now crossalpha-state-v02.timer

echo "Installed native Rust State V0.2 timer."
echo "Status: systemctl --user status crossalpha-state-v02.timer --no-pager"
