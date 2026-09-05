#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

if ! command -v crossalpha-state-v03-cycle >/dev/null 2>&1; then
  python -m pip install -e ".[dev]"
fi

python - <<'PY'
from crossalpha.settings import Settings
from crossalpha.state.v03_rpc import resolve_rpc_url
settings = Settings()
_, source = resolve_rpc_url(settings.evm_rpc_url)
print(f"State V0.3 RPC source: {source}")
PY

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/crossalpha-state-v03.service" <<EOF
[Unit]
Description=CrossAlpha State V0.3 Aave borrower-risk shadow cycle
After=network-online.target crossalpha-state-v02.service
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_DIR
ExecStart=$REPO_DIR/.venv/bin/crossalpha-state-v03-cycle
TimeoutStartSec=12min
EOF

cat > "$UNIT_DIR/crossalpha-state-v03.timer" <<'EOF'
[Unit]
Description=Advance CrossAlpha State V0.3 borrower-risk shadow every 15 minutes

[Timer]
OnBootSec=4min
OnUnitActiveSec=15min
AccuracySec=30s
Unit=crossalpha-state-v03.service

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now crossalpha-state-v03.timer

echo "Installed: $UNIT_DIR/crossalpha-state-v03.service"
echo "Installed: $UNIT_DIR/crossalpha-state-v03.timer"
echo "Status:    systemctl --user status crossalpha-state-v03.timer --no-pager"
echo "Logs:      journalctl --user -u crossalpha-state-v03.service -n 100 --no-pager"
