#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

cargo build --workspace --release
OUTCOME="$REPO_DIR/target/release/crossalpha-outcome-rs"
[[ -x "$OUTCOME" ]] || { echo "Missing native Outcome binary: $OUTCOME" >&2; exit 2; }

"$OUTCOME" integrity || {
  echo "Outcome Linkage native binding/integrity is not green." >&2
  echo "Run scripts/bind_native_state_runtime.sh --activate after R7 pre-cutover acceptance." >&2
  exit 2
}

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/crossalpha-outcome-linkage.service" <<EOF
[Unit]
Description=CrossAlpha Rust prospective state-to-outcome linkage materializer
After=network-online.target crossalpha-free-paper-daily.service crossalpha-free-paper-weekly.service
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_DIR
ExecStart=$OUTCOME materialize
ExecStart=$OUTCOME integrity
TimeoutStartSec=5min
EOF

cat > "$UNIT_DIR/crossalpha-outcome-linkage.timer" <<'EOF'
[Unit]
Description=Materialize CrossAlpha Rust prospective outcomes daily at 05:00 UTC

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
