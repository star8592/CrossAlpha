#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

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
"$BINARY" v04 integrity --data-root "$DATA_ROOT" | python3 -c 'import json,sys; v=json.load(sys.stdin); raise SystemExit(0 if v.get("cycle_enabled") is True else 2)'

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/crossalpha-state-v04.service" <<EOF
[Unit]
Description=CrossAlpha State V0.4 native Rust multi-venue mechanics cycle
After=network-online.target crossalpha-state-v03.service
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_DIR
Environment=CROSSALPHA_DATA_DIR=$DATA_ROOT
ExecStart=$BINARY v04 cycle --data-root $DATA_ROOT
TimeoutStartSec=4min
EOF

cat > "$UNIT_DIR/crossalpha-state-v04.timer" <<'EOF'
[Unit]
Description=Run CrossAlpha State V0.4 native Rust cycle every 5 minutes

[Timer]
OnActiveSec=5min
OnUnitActiveSec=5min
AccuracySec=20s
Unit=crossalpha-state-v04.service

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now crossalpha-state-v04.timer

echo "Installed native Rust State V0.4 timer."
echo "Status: systemctl --user status crossalpha-state-v04.timer --no-pager"
