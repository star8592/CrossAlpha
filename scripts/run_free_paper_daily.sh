#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

PAPER="$REPO_DIR/target/release/crossalpha-paper-rs"
AB="$REPO_DIR/target/release/crossalpha-ab-rs"
[[ -x "$PAPER" ]] || { echo "Missing native Paper binary: $PAPER" >&2; exit 2; }
[[ -x "$AB" ]] || { echo "Missing native A/B binary: $AB" >&2; exit 2; }

TODAY_UTC="$(date -u +%F)"

retry_refresh() {
  local attempt
  for attempt in 1 2 3; do
    if "$PAPER" refresh --end "$TODAY_UTC"; then
      return 0
    fi
    echo "native paper refresh attempt $attempt failed" >&2
    if [[ "$attempt" -lt 3 ]]; then
      sleep 60
    fi
  done
  return 1
}

# A is always sealed first. B then references the exact immutable A mark and
# reuses A's asset_returns; B never performs an independent vendor refresh.
retry_refresh
"$PAPER" integrity
"$PAPER" mark --end "$TODAY_UTC"
"$PAPER" integrity
"$AB" mark --end "$TODAY_UTC"
"$AB" integrity
"$PAPER" status
"$AB" status
