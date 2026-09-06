#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

PAPER="$REPO_DIR/target/release/crossalpha-paper-rs"
AB="$REPO_DIR/target/release/crossalpha-ab-rs"
[[ -x "$PAPER" ]] || { echo "Missing native Paper binary: $PAPER" >&2; exit 2; }
[[ -x "$AB" ]] || { echo "Missing native A/B binary: $AB" >&2; exit 2; }

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

# Freeze/order discipline:
#   1) refresh point-in-time Core data
#   2) seal A snapshot
#   3) seal contemporaneous State decision + B snapshot referencing A hash
#   4) seal the preceding day's A mark
#   5) seal the same preceding day's B mark referencing that A mark
retry_refresh
"$PAPER" integrity
"$PAPER" snapshot --effective-date "$TODAY_UTC"
"$AB" snapshot --effective-date "$TODAY_UTC"
"$PAPER" mark --end "$TODAY_UTC"
"$PAPER" integrity
"$AB" mark --end "$TODAY_UTC"
"$AB" integrity
"$PAPER" status
"$AB" status
