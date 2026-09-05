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

# Freeze/order discipline:
#   1) refresh point-in-time Core data
#   2) seal A snapshot
#   3) seal contemporaneous State decision + B snapshot referencing A hash
#   4) seal the preceding day's A mark
#   5) seal the same preceding day's B mark referencing that A mark
retry_refresh
python scripts/check_free_paper_integrity.py
crossalpha-free-paper-snapshot --effective-date "$TODAY_UTC"
crossalpha-state-ab-snapshot --effective-date "$TODAY_UTC"
crossalpha-free-paper-mark --end "$TODAY_UTC"
python scripts/check_free_paper_integrity.py
crossalpha-state-ab-mark --end "$TODAY_UTC"
crossalpha-state-ab-integrity
crossalpha-free-paper-status
crossalpha-state-ab-status
