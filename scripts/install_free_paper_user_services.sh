#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# Standalone installs still self-bootstrap. The milestone finalizer already runs
# one editable install, so do not repeat it when the new A/B entry point exists.
if [[ "${CROSSALPHA_SKIP_EDITABLE_INSTALL:-0}" != "1" ]] \
  && ! command -v crossalpha-state-ab-freeze >/dev/null 2>&1; then
  python -m pip install -e ".[dev]"
fi

# A must be frozen first; the B experiment freeze references A's immutable hash.
crossalpha-free-paper-freeze --historical-start 2010-06-01 --historical-end 2026-09-01
crossalpha-state-ab-freeze
crossalpha-state-ab-integrity

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/crossalpha-free-paper-daily.service" <<EOF
[Unit]
Description=CrossAlpha frozen B3 + State A/B daily prospective marks
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_DIR
ExecStart=/usr/bin/env bash $REPO_DIR/scripts/run_free_paper_daily.sh
EOF

cat > "$UNIT_DIR/crossalpha-free-paper-daily.timer" <<'EOF'
[Unit]
Description=Run CrossAlpha Frozen B3 + State A/B marks Tue-Sun

[Timer]
OnCalendar=Tue..Sun *-*-* 04:00:00 UTC
Persistent=true
AccuracySec=1min

[Install]
WantedBy=timers.target
EOF

cat > "$UNIT_DIR/crossalpha-free-paper-weekly.service" <<EOF
[Unit]
Description=CrossAlpha frozen B3 + State A/B Monday prospective snapshots
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_DIR
ExecStart=/usr/bin/env bash $REPO_DIR/scripts/run_free_paper_weekly.sh
EOF

cat > "$UNIT_DIR/crossalpha-free-paper-weekly.timer" <<'EOF'
[Unit]
Description=Run CrossAlpha Frozen B3 + State A/B snapshot every Monday

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
echo "CrossAlpha frozen B3 + prospective State A/B engine installed."
echo "Daily timer:  systemctl --user status crossalpha-free-paper-daily.timer --no-pager"
echo "Weekly timer: systemctl --user status crossalpha-free-paper-weekly.timer --no-pager"
echo "Timers:       systemctl --user list-timers --all | grep crossalpha-free-paper"
echo "A status:     crossalpha-free-paper-status"
echo "A/B status:   crossalpha-state-ab-status"
