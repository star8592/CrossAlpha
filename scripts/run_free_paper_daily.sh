#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
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

# A is always sealed first. B then references the exact immutable A mark and
# reuses A's asset_returns; B never performs an independent vendor refresh.
retry_refresh
python scripts/check_free_paper_integrity.py
crossalpha-free-paper-mark --end "$TODAY_UTC"
python scripts/check_free_paper_integrity.py
crossalpha-state-ab-mark --end "$TODAY_UTC"
crossalpha-state-ab-integrity
crossalpha-free-paper-status
crossalpha-state-ab-status
