#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

python -m pip install -e ".[dev]"
crossalpha-free-paper-freeze --historical-start 2010-06-01 --historical-end 2026-09-01

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/crossalpha-free-paper-daily.service" <<EOF
[Unit]
Description=CrossAlpha frozen B3 paper daily refresh and mark
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_DIR
ExecStart=/usr/bin/env bash $REPO_DIR/scripts/run_free_paper_daily.sh
EOF

cat > "$UNIT_DIR/crossalpha-free-paper-daily.timer" <<'EOF'
[Unit]
Description=Run CrossAlpha frozen B3 paper marks Tue-Sun

[Timer]
OnCalendar=Tue..Sun *-*-* 04:00:00 UTC
Persistent=true
AccuracySec=1min

[Install]
WantedBy=timers.target
EOF

cat > "$UNIT_DIR/crossalpha-free-paper-weekly.service" <<EOF
[Unit]
Description=CrossAlpha frozen B3 Monday paper snapshot
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_DIR
ExecStart=/usr/bin/env bash $REPO_DIR/scripts/run_free_paper_weekly.sh
EOF

cat > "$UNIT_DIR/crossalpha-free-paper-weekly.timer" <<'EOF'
[Unit]
Description=Run CrossAlpha frozen B3 snapshot every Monday

[Timer]
OnCalendar=Mon *-*-* 00:20:00 UTC
Persistent=true
AccuracySec=1min

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now crossalpha-free-paper-daily.timer
systemctl --user enable --now crossalpha-free-paper-weekly.timer

echo
echo "CrossAlpha frozen B3 paper engine installed."
echo "Daily timer:  systemctl --user status crossalpha-free-paper-daily.timer --no-pager"
echo "Weekly timer: systemctl --user status crossalpha-free-paper-weekly.timer --no-pager"
echo "Timers:       systemctl --user list-timers --all | grep crossalpha-free-paper"
echo "Status:       crossalpha-free-paper-status"
