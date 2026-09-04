#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
SERVICE_SRC="$REPO_DIR/deploy/systemd/crossalpha-materializer.service"
TIMER_SRC="$REPO_DIR/deploy/systemd/crossalpha-materializer.timer"
SERVICE_DST="$UNIT_DIR/crossalpha-materializer.service"
TIMER_DST="$UNIT_DIR/crossalpha-materializer.timer"

mkdir -p "$UNIT_DIR"
sed "s|WorkingDirectory=%h/CrossAlpha|WorkingDirectory=$REPO_DIR|; s|EnvironmentFile=%h/CrossAlpha/.env|EnvironmentFile=$REPO_DIR/.env|; s|ExecStart=%h/CrossAlpha/.venv/bin/python|ExecStart=$REPO_DIR/.venv/bin/python|" "$SERVICE_SRC" > "$SERVICE_DST"
cp "$TIMER_SRC" "$TIMER_DST"

systemctl --user daemon-reload
systemctl --user enable --now crossalpha-materializer.timer
systemctl --user start crossalpha-materializer.service

echo "Installed: $SERVICE_DST"
echo "Installed: $TIMER_DST"
echo "Timer:     systemctl --user status crossalpha-materializer.timer"
echo "Service:   systemctl --user status crossalpha-materializer.service"
echo "Logs:      journalctl --user -u crossalpha-materializer.service -n 100 --no-pager"
