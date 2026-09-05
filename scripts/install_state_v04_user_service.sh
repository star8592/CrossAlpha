#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

if ! command -v crossalpha-state-v04-cycle >/dev/null 2>&1; then
  python -m pip install -e ".[dev]"
fi

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/crossalpha-state-v04.service" <<EOF
[Unit]
Description=CrossAlpha State V0.4 multi-venue mechanics shadow cycle
After=network-online.target crossalpha-state-v03.service
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_DIR
ExecStart=$REPO_DIR/.venv/bin/python $REPO_DIR/scripts/run_state_v04_cycle.py
TimeoutStartSec=4min
EOF

cat > "$UNIT_DIR/crossalpha-state-v04.timer" <<'EOF'
[Unit]
Description=Collect CrossAlpha State V0.4 multi-venue mechanics every 5 minutes

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

echo "Installed: $UNIT_DIR/crossalpha-state-v04.service"
echo "Installed: $UNIT_DIR/crossalpha-state-v04.timer"
echo "First automatic cycle: approximately 5 minutes after timer activation"
echo "Status:    systemctl --user status crossalpha-state-v04.timer --no-pager"
echo "Logs:      journalctl --user -u crossalpha-state-v04.service -n 100 --no-pager"
