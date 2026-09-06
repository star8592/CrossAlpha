#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

cargo build --workspace --release
PAPER="$REPO_DIR/target/release/crossalpha-paper-rs"
AB="$REPO_DIR/target/release/crossalpha-ab-rs"
[[ -x "$PAPER" ]] || { echo "Missing native Paper binary: $PAPER" >&2; exit 2; }
[[ -x "$AB" ]] || { echo "Missing native A/B binary: $AB" >&2; exit 2; }

# Migration never rewrites the already-frozen V0.1 experiments. The Rust
# sidecar bindings must be valid before any production timer can be installed.
"$PAPER" integrity || {
  echo "Frozen B3 native binding/integrity is not green." >&2
  echo "Run scripts/bind_native_state_runtime.sh --activate after R7 pre-cutover acceptance." >&2
  exit 2
}
"$AB" integrity || {
  echo "State A/B native binding/integrity is not green." >&2
  echo "Run scripts/bind_native_state_runtime.sh --activate after R7 pre-cutover acceptance." >&2
  exit 2
}

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/crossalpha-free-paper-daily.service" <<EOF
[Unit]
Description=CrossAlpha Rust frozen B3 + State A/B daily prospective marks
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_DIR
ExecStart=/usr/bin/env bash $REPO_DIR/scripts/run_free_paper_daily.sh
TimeoutStartSec=20min
EOF

cat > "$UNIT_DIR/crossalpha-free-paper-daily.timer" <<'EOF'
[Unit]
Description=Run CrossAlpha Rust Frozen B3 + State A/B marks Tue-Sun

[Timer]
OnCalendar=Tue..Sun *-*-* 04:00:00 UTC
Persistent=true
AccuracySec=1min
Unit=crossalpha-free-paper-daily.service

[Install]
WantedBy=timers.target
EOF

cat > "$UNIT_DIR/crossalpha-free-paper-weekly.service" <<EOF
[Unit]
Description=CrossAlpha Rust frozen B3 + State A/B Monday prospective snapshots
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_DIR
ExecStart=/usr/bin/env bash $REPO_DIR/scripts/run_free_paper_weekly.sh
TimeoutStartSec=20min
EOF

cat > "$UNIT_DIR/crossalpha-free-paper-weekly.timer" <<'EOF'
[Unit]
Description=Run CrossAlpha Rust Frozen B3 + State A/B snapshot every Monday

[Timer]
OnCalendar=Mon *-*-* 00:20:00 UTC
Persistent=true
AccuracySec=1min
Unit=crossalpha-free-paper-weekly.service

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now crossalpha-free-paper-daily.timer
systemctl --user enable --now crossalpha-free-paper-weekly.timer

echo
echo "CrossAlpha Rust frozen B3 + prospective State A/B engine installed."
echo "Daily timer:  systemctl --user status crossalpha-free-paper-daily.timer --no-pager"
echo "Weekly timer: systemctl --user status crossalpha-free-paper-weekly.timer --no-pager"
echo "A status:     $PAPER status"
echo "A/B status:   $AB status"
