#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# A Persistent systemd timer may run after a missed Monday. That must never
# manufacture a retrospective prospective snapshot.
if [[ "$(date -u +%u)" != "1" ]]; then
  echo "Not Monday UTC; refusing retrospective paper snapshot."
  exit 0
fi

TODAY_UTC="$(date -u +%F)"

retry_refresh() {
  local attempt
  for attempt in 1 2 3; do
    if crossalpha-free-paper-refresh --end "$TODAY_UTC"; then
      return 0
    fi
    echo "paper refresh attempt $attempt failed" >&2
    if [[ "$attempt" -lt 3 ]]; then
      sleep 60
    fi
  done
  return 1
}

retry_refresh
crossalpha-free-paper-snapshot --effective-date "$TODAY_UTC"
crossalpha-free-paper-mark --end "$TODAY_UTC"
crossalpha-free-paper-status
