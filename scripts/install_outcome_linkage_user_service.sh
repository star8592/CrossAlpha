#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

if ! command -v crossalpha-outcome-materialize >/dev/null 2>&1; then
  python -m pip install -e ".[dev]"
fi

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/crossalpha-outcome-linkage.service" <<EOF
[Unit]
Description=CrossAlpha prospective state-to-outcome linkage materializer
After=network-online.target crossalpha-free-paper-daily.service crossalpha-free-paper-weekly.service
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_DIR
ExecStart=$REPO_DIR/.venv/bin/python $REPO_DIR/scripts/run_outcome_linkage_cycle.py
TimeoutStartSec=5min
EOF

cat > "$UNIT_DIR/crossalpha-outcome-linkage.timer" <<'EOF'
[Unit]
Description=Materialize matured CrossAlpha prospective outcomes daily at 05:00 UTC

[Timer]
OnCalendar=*-*-* 05:00:00 UTC
Persistent=true
AccuracySec=1min
Unit=crossalpha-outcome-linkage.service

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now crossalpha-outcome-linkage.timer

echo "Installed: $UNIT_DIR/crossalpha-outcome-linkage.service"
echo "Installed: $UNIT_DIR/crossalpha-outcome-linkage.timer"
echo "Status:    systemctl --user status crossalpha-outcome-linkage.timer --no-pager"
echo "Logs:      journalctl --user -u crossalpha-outcome-linkage.service -n 100 --no-pager"
