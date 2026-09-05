#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

if ! command -v crossalpha-state-v02-cycle >/dev/null 2>&1; then
  python -m pip install -e ".[dev]"
fi

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/crossalpha-state-v02.service" <<EOF
[Unit]
Description=CrossAlpha State V0.2 descriptive capital-state cycle
After=network-online.target crossalpha-observatory.service
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_DIR
ExecStart=$REPO_DIR/.venv/bin/crossalpha-state-v02-cycle
TimeoutStartSec=5min
EOF

cat > "$UNIT_DIR/crossalpha-state-v02.timer" <<'EOF'
[Unit]
Description=Run CrossAlpha State V0.2 descriptive cycle every 15 minutes

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

echo "Installed: $UNIT_DIR/crossalpha-state-v02.service"
echo "Installed: $UNIT_DIR/crossalpha-state-v02.timer"
echo "Status:    systemctl --user status crossalpha-state-v02.timer --no-pager"
echo "Logs:      journalctl --user -u crossalpha-state-v02.service -n 100 --no-pager"
