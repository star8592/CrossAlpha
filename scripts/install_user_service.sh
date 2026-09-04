#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$REPO_DIR/deploy/systemd/crossalpha-observatory.service"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_DST="$UNIT_DIR/crossalpha-observatory.service"

mkdir -p "$UNIT_DIR"
sed "s|WorkingDirectory=%h/CrossAlpha|WorkingDirectory=$REPO_DIR|; s|EnvironmentFile=%h/CrossAlpha/.env|EnvironmentFile=$REPO_DIR/.env|; s|ExecStart=%h/CrossAlpha/.venv/bin/python|ExecStart=$REPO_DIR/.venv/bin/python|" "$UNIT_SRC" > "$UNIT_DST"

systemctl --user daemon-reload
systemctl --user enable --now crossalpha-observatory.service

echo "Installed: $UNIT_DST"
echo "Status:    systemctl --user status crossalpha-observatory.service"
echo "Logs:      journalctl --user -u crossalpha-observatory.service -f"
