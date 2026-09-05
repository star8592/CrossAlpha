#!/usr/bin/env bash
set -euo pipefail

# Invoke with:
#   bash scripts/rollback_rust_observatory_service.sh
# This works even if the checkout does not preserve the executable bit.

UNIT_DIR="$HOME/.config/systemd/user"
UNIT_DST="$UNIT_DIR/crossalpha-observatory.service"
UNIT_BACKUP="$UNIT_DIR/crossalpha-observatory.service.pre-rust"

if [[ ! -f "$UNIT_BACKUP" ]]; then
  echo "Rollback backup missing: $UNIT_BACKUP" >&2
  exit 2
fi

systemctl --user stop crossalpha-observatory.service >/dev/null 2>&1 || true
cp -f "$UNIT_BACKUP" "$UNIT_DST"
systemctl --user daemon-reload
systemctl --user enable --now crossalpha-observatory.service >/dev/null

if systemctl --user is-active --quiet crossalpha-observatory.service; then
  echo "Python Observatory service restored successfully."
  echo "status: systemctl --user status crossalpha-observatory.service"
  exit 0
fi

echo "Python Observatory service failed to become active after rollback." >&2
systemctl --user status crossalpha-observatory.service --no-pager >&2 || true
exit 1
