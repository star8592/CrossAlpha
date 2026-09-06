#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTIVATE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --activate)
      ACTIVATE=true
      shift
      ;;
    -h|--help)
      echo "Usage: bash scripts/install_all_user_services.sh --activate"
      echo "Installs the guarded split Rust service stack only when the unified daemon does not own writers."
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ "$ACTIVATE" != true ]]; then
  echo "Refusing split-stack activation without explicit --activate." >&2
  exit 2
fi

if systemctl --user is-active --quiet crossalpha-daemon.service \
  || systemctl --user is-enabled --quiet crossalpha-daemon.service; then
  echo "Refusing split-service install: crossalpha-daemon.service already owns production writers." >&2
  echo "Use the guarded unified-daemon cutover/rollback workflow instead." >&2
  exit 2
fi

bash "$REPO_DIR/scripts/install_user_service.sh"
bash "$REPO_DIR/scripts/install_materializer_timer.sh" --activate
bash "$REPO_DIR/scripts/install_free_paper_user_services.sh"
bash "$REPO_DIR/scripts/install_state_v02_user_service.sh"
bash "$REPO_DIR/scripts/install_state_v03_user_service.sh"
bash "$REPO_DIR/scripts/install_state_v04_user_service.sh"
bash "$REPO_DIR/scripts/install_outcome_linkage_user_service.sh"

systemctl --user daemon-reload

echo
echo "CrossAlpha guarded split Rust services installed."
echo "Collector:       systemctl --user status crossalpha-observatory.service --no-pager"
echo "Materializer:    systemctl --user status crossalpha-materializer.timer --no-pager"
echo "Paper daily:     systemctl --user status crossalpha-free-paper-daily.timer --no-pager"
echo "Paper weekly:    systemctl --user status crossalpha-free-paper-weekly.timer --no-pager"
echo "State V0.2:      systemctl --user status crossalpha-state-v02.timer --no-pager"
echo "State V0.3:      systemctl --user status crossalpha-state-v03.timer --no-pager"
echo "State V0.4:      systemctl --user status crossalpha-state-v04.timer --no-pager"
echo "Outcome Linkage: systemctl --user status crossalpha-outcome-linkage.timer --no-pager"
echo "All timers:      systemctl --user list-timers --all | grep crossalpha"
