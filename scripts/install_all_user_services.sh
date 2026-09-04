#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

bash "$REPO_DIR/scripts/install_user_service.sh"
bash "$REPO_DIR/scripts/install_materializer_timer.sh"

systemctl --user daemon-reload

echo
echo "CrossAlpha local services installed."
echo "Collector:    systemctl --user status crossalpha-observatory.service --no-pager"
echo "Materializer: systemctl --user status crossalpha-materializer.timer --no-pager"
echo "Timers:       systemctl --user list-timers --all | grep crossalpha"
