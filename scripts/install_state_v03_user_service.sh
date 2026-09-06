#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if systemctl --user is-active --quiet crossalpha-daemon.service \
  || systemctl --user is-enabled --quiet crossalpha-daemon.service; then
  echo "Refusing standalone State V0.3 install: unified Rust daemon already owns State runtime." >&2
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
"$BINARY" v03 integrity --data-root "$DATA_ROOT" | python3 -c 'import json,sys; v=json.load(sys.stdin); raise SystemExit(0 if v.get("cycle_enabled") is True else 2)'

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/crossalpha-state-v03.service" <<EOF
[Unit]
Description=CrossAlpha State V0.3 native Rust borrower-risk cycle
After=network-online.target crossalpha-state-v02.service
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_DIR
Environment=CROSSALPHA_DATA_DIR=$DATA_ROOT
ExecStart=$BINARY v03 cycle --data-root $DATA_ROOT
TimeoutStartSec=12min
EOF

cat > "$UNIT_DIR/crossalpha-state-v03.timer" <<'EOF'
[Unit]
Description=Run CrossAlpha State V0.3 native Rust cycle every 15 minutes

[Timer]
OnActiveSec=15min
OnUnitActiveSec=15min
AccuracySec=30s
Unit=crossalpha-state-v03.service

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now crossalpha-state-v03.timer

echo "Installed native Rust State V0.3 timer."
echo "Status: systemctl --user status crossalpha-state-v03.timer --no-pager"
