#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_TEMPLATE="$REPO_DIR/deploy/systemd/crossalpha-observatory-rust.service"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_DST="$UNIT_DIR/crossalpha-observatory.service"
UNIT_BACKUP="$UNIT_DIR/crossalpha-observatory.service.pre-rust"
BACKUP_META="$UNIT_DIR/crossalpha-observatory.pre-rust.meta"
BINARY="$REPO_DIR/target/release/crossalpha-rs"
DATA_ROOT="${CROSSALPHA_DATA_DIR:-}"
ACTIVATE=false

usage() {
  cat <<'EOF'
Usage:
  bash scripts/cutover_rust_observatory_service.sh --activate [--data-root PATH]

This command is intentionally guarded. Invoke it through `bash` so it works even
when the repository checkout does not preserve the executable bit.

It will:
  1. build the release Rust binary;
  2. run the isolated shadow-write gate against that release binary;
  3. require the current real Observatory to be healthy;
  4. stop and back up the current Python systemd unit;
  5. install/start the Rust unit;
  6. require a new audit-ledger write and healthy Rust live-health;
  7. automatically roll back to the Python unit on failure.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --activate)
      ACTIVATE=true
      shift
      ;;
    --data-root)
      [[ $# -ge 2 ]] || { echo "--data-root requires a value" >&2; exit 2; }
      DATA_ROOT="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$ACTIVATE" != true ]]; then
  echo "Refusing to change systemd without explicit --activate." >&2
  usage >&2
  exit 2
fi

if [[ -z "$DATA_ROOT" && -f "$REPO_DIR/.env" ]]; then
  line="$(grep -E '^[[:space:]]*CROSSALPHA_DATA_DIR[[:space:]]*=' "$REPO_DIR/.env" | tail -n 1 || true)"
  if [[ -n "$line" ]]; then
    DATA_ROOT="${line#*=}"
    DATA_ROOT="${DATA_ROOT#${DATA_ROOT%%[![:space:]]*}}"
    DATA_ROOT="${DATA_ROOT%${DATA_ROOT##*[![:space:]]}}"
    if [[ "$DATA_ROOT" == \"*\" && "$DATA_ROOT" == *\" ]]; then
      DATA_ROOT="${DATA_ROOT:1:${#DATA_ROOT}-2}"
    elif [[ "$DATA_ROOT" == \'*\' && "$DATA_ROOT" == *\' ]]; then
      DATA_ROOT="${DATA_ROOT:1:${#DATA_ROOT}-2}"
    fi
  fi
fi

if [[ -z "$DATA_ROOT" ]]; then
  echo "CROSSALPHA_DATA_DIR is not set. Pass --data-root explicitly." >&2
  exit 2
fi

if [[ "$DATA_ROOT" != /* ]]; then
  DATA_ROOT="$REPO_DIR/$DATA_ROOT"
fi
DATA_ROOT="$(realpath -m "$DATA_ROOT")"

if [[ ! -d "$DATA_ROOT/manifests" ]]; then
  echo "Data root does not look like CrossAlphaData: $DATA_ROOT" >&2
  exit 2
fi
if [[ ! -f "$UNIT_TEMPLATE" ]]; then
  echo "Rust systemd template missing: $UNIT_TEMPLATE" >&2
  exit 2
fi
if [[ ! -f "$UNIT_DST" ]]; then
  echo "Current Observatory systemd unit missing: $UNIT_DST" >&2
  echo "Refusing cutover because there is no unit to back up for rollback." >&2
  exit 2
fi
if ! grep -Eq 'collect_loop\.py|\.venv/bin/python' "$UNIT_DST"; then
  echo "Current Observatory unit does not look like the Python service:" >&2
  sed -n '1,80p' "$UNIT_DST" >&2
  echo "Refusing to overwrite it automatically." >&2
  exit 2
fi

cd "$REPO_DIR"

echo "==> Building production Rust binary"
cargo build --release -p crossalpha-cli

echo "==> Running release-binary isolated shadow-write gate"
.venv/bin/python scripts/verify_rust_observatory_shadow_write.py \
  --rust-binary "$BINARY"

echo "==> Confirming current real data is healthy before cutover"
"$BINARY" observatory-live-health "$DATA_ROOT" --no-write-report >/dev/null

mkdir -p "$UNIT_DIR"
cp -f "$UNIT_DST" "$UNIT_BACKUP"
printf 'backup=%s\ndata_root=%s\nrepo_dir=%s\n' \
  "$UNIT_BACKUP" "$DATA_ROOT" "$REPO_DIR" > "$BACKUP_META"

rollback() {
  rc=$?
  trap - ERR INT TERM
  echo "Rust Observatory cutover failed (rc=$rc); restoring Python service." >&2
  systemctl --user stop crossalpha-observatory.service >/dev/null 2>&1 || true
  if [[ -f "$UNIT_BACKUP" ]]; then
    cp -f "$UNIT_BACKUP" "$UNIT_DST"
    systemctl --user daemon-reload
    systemctl --user enable --now crossalpha-observatory.service >/dev/null
    echo "Python Observatory service restored." >&2
  else
    echo "Rollback backup is missing: $UNIT_BACKUP" >&2
  fi
  exit "$rc"
}
trap rollback ERR INT TERM

echo "==> Stopping Python Observatory service"
systemctl --user stop crossalpha-observatory.service

AUDIT="$DATA_ROOT/manifests/raw_snapshots.jsonl"
if [[ -f "$AUDIT" ]]; then
  BEFORE_RECORDS="$(wc -l < "$AUDIT")"
else
  BEFORE_RECORDS=0
fi

echo "==> Installing Rust Observatory systemd unit"
sed \
  -e "s|@REPO_DIR@|$REPO_DIR|g" \
  -e "s|@DATA_ROOT@|$DATA_ROOT|g" \
  "$UNIT_TEMPLATE" > "$UNIT_DST"

systemctl --user daemon-reload
systemctl --user enable crossalpha-observatory.service >/dev/null
systemctl --user start crossalpha-observatory.service

echo "==> Verifying first Rust collector cycle"
NEW_RECORDS="$BEFORE_RECORDS"
for _ in $(seq 1 70); do
  if ! systemctl --user is-active --quiet crossalpha-observatory.service; then
    echo "Rust Observatory service exited during verification." >&2
    systemctl --user status crossalpha-observatory.service --no-pager >&2 || true
    false
  fi
  if [[ -f "$AUDIT" ]]; then
    NEW_RECORDS="$(wc -l < "$AUDIT")"
  fi
  if (( NEW_RECORDS >= BEFORE_RECORDS + 3 )); then
    break
  fi
  sleep 2
done

if (( NEW_RECORDS < BEFORE_RECORDS + 3 )); then
  echo "Rust service did not append the expected three Observatory records." >&2
  journalctl --user -u crossalpha-observatory.service -n 100 --no-pager >&2 || true
  false
fi

"$BINARY" observatory-live-health "$DATA_ROOT" --no-write-report >/dev/null
systemctl --user is-active --quiet crossalpha-observatory.service

trap - ERR INT TERM

echo "Rust Observatory cutover succeeded."
echo "audit_records_before=$BEFORE_RECORDS"
echo "audit_records_after=$NEW_RECORDS"
echo "rollback_backup=$UNIT_BACKUP"
echo "status: systemctl --user status crossalpha-observatory.service"
echo "logs:   journalctl --user -u crossalpha-observatory.service -f"
