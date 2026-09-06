#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_TEMPLATE="$REPO_DIR/deploy/systemd/crossalpha-daemon.service"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_DST="$UNIT_DIR/crossalpha-daemon.service"
META="$UNIT_DIR/crossalpha-daemon.pre-cutover.meta"
DATA_ROOT="${CROSSALPHA_DATA_DIR:-}"
ACTIVATE=false
INCLUDE_V03=false
INCLUDE_V04=false

usage() {
  cat <<'EOF'
Usage:
  bash scripts/cutover_unified_rust_daemon.sh --activate [--data-root PATH] [--include-state-v03] [--include-state-v04]

The default unified daemon owns Observatory + bounded materializer only.
State V0.3/V0.4 are opt-in, require production native integrity, and their old
Python timers are stopped only when the matching --include-state-* flag is used.
The script refuses unless rust_migration_acceptance.json authorizes daemon cutover.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --activate) ACTIVATE=true; shift ;;
    --data-root)
      [[ $# -ge 2 ]] || { echo "--data-root requires a value" >&2; exit 2; }
      DATA_ROOT="$2"; shift 2 ;;
    --include-state-v03) INCLUDE_V03=true; shift ;;
    --include-state-v04) INCLUDE_V04=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$ACTIVATE" == true ]] || { echo "Refusing cutover without explicit --activate." >&2; exit 2; }

if [[ -z "$DATA_ROOT" && -f "$REPO_DIR/.env" ]]; then
  line="$(grep -E '^[[:space:]]*CROSSALPHA_DATA_DIR[[:space:]]*=' "$REPO_DIR/.env" | tail -n 1 || true)"
  if [[ -n "$line" ]]; then
    DATA_ROOT="${line#*=}"
    DATA_ROOT="${DATA_ROOT#${DATA_ROOT%%[![:space:]]*}}"
    DATA_ROOT="${DATA_ROOT%${DATA_ROOT##*[![:space:]]}}"
    DATA_ROOT="${DATA_ROOT%\"}"; DATA_ROOT="${DATA_ROOT#\"}"
    DATA_ROOT="${DATA_ROOT%\'}"; DATA_ROOT="${DATA_ROOT#\'}"
  fi
fi
[[ -n "$DATA_ROOT" ]] || { echo "CROSSALPHA_DATA_DIR is not set" >&2; exit 2; }
[[ "$DATA_ROOT" == /* ]] || DATA_ROOT="$REPO_DIR/$DATA_ROOT"
DATA_ROOT="$(realpath -m "$DATA_ROOT")"

ACCEPTANCE="$DATA_ROOT/manifests/rust_migration_acceptance.json"
[[ -f "$ACCEPTANCE" ]] || { echo "Acceptance report missing: $ACCEPTANCE" >&2; exit 2; }
python3 - "$ACCEPTANCE" <<'PY'
import json, sys
p = sys.argv[1]
v = json.load(open(p, encoding="utf-8"))
if v.get("daemon_cutover_allowed") is not True:
    raise SystemExit(f"daemon_cutover_allowed is not true in {p}")
print("acceptance=DAEMON_CUTOVER_ALLOWED")
PY

cd "$REPO_DIR"
cargo build --workspace --release
BINARY="$REPO_DIR/target/release/crossalpha-daemon-rs"
STATE_BINARY="$REPO_DIR/target/release/crossalpha-state-rs"
[[ -x "$BINARY" ]] || { echo "Daemon binary missing: $BINARY" >&2; exit 2; }

if [[ "$INCLUDE_V03" == true ]]; then
  "$STATE_BINARY" v03 integrity --data-root "$DATA_ROOT" | python3 -c 'import json,sys; v=json.load(sys.stdin); raise SystemExit(0 if v.get("cycle_enabled") is True else 2)'
fi
if [[ "$INCLUDE_V04" == true ]]; then
  "$STATE_BINARY" v04 integrity --data-root "$DATA_ROOT" | python3 -c 'import json,sys; v=json.load(sys.stdin); raise SystemExit(0 if v.get("cycle_enabled") is True else 2)'
fi

obs_active=false; obs_enabled=false
mat_active=false; mat_enabled=false
v03_active=false; v03_enabled=false
v04_active=false; v04_enabled=false
systemctl --user is-active --quiet crossalpha-observatory.service && obs_active=true || true
systemctl --user is-enabled --quiet crossalpha-observatory.service && obs_enabled=true || true
systemctl --user is-active --quiet crossalpha-materializer.timer && mat_active=true || true
systemctl --user is-enabled --quiet crossalpha-materializer.timer && mat_enabled=true || true
systemctl --user is-active --quiet crossalpha-state-v03.timer && v03_active=true || true
systemctl --user is-enabled --quiet crossalpha-state-v03.timer && v03_enabled=true || true
systemctl --user is-active --quiet crossalpha-state-v04.timer && v04_active=true || true
systemctl --user is-enabled --quiet crossalpha-state-v04.timer && v04_enabled=true || true
printf 'observatory_active=%s\nobservatory_enabled=%s\nmaterializer_active=%s\nmaterializer_enabled=%s\nstate_v03_active=%s\nstate_v03_enabled=%s\nstate_v04_active=%s\nstate_v04_enabled=%s\ndata_root=%s\n' \
  "$obs_active" "$obs_enabled" "$mat_active" "$mat_enabled" \
  "$v03_active" "$v03_enabled" "$v04_active" "$v04_enabled" "$DATA_ROOT" > "$META"

COMPONENT_ARGS="--component observatory --component materializer --allow-production-materialization"
[[ "$INCLUDE_V03" == true ]] && COMPONENT_ARGS+=" --component state-v03"
[[ "$INCLUDE_V04" == true ]] && COMPONENT_ARGS+=" --component state-v04"

rollback() {
  rc=$?
  trap - ERR INT TERM
  echo "Unified daemon cutover failed (rc=$rc); restoring previous services." >&2
  systemctl --user disable --now crossalpha-daemon.service >/dev/null 2>&1 || true
  if [[ "$obs_enabled" == true ]]; then systemctl --user enable crossalpha-observatory.service >/dev/null 2>&1 || true; fi
  if [[ "$obs_active" == true ]]; then systemctl --user start crossalpha-observatory.service >/dev/null 2>&1 || true; fi
  if [[ "$mat_enabled" == true ]]; then systemctl --user enable crossalpha-materializer.timer >/dev/null 2>&1 || true; fi
  if [[ "$mat_active" == true ]]; then systemctl --user start crossalpha-materializer.timer >/dev/null 2>&1 || true; fi
  if [[ "$v03_enabled" == true ]]; then systemctl --user enable crossalpha-state-v03.timer >/dev/null 2>&1 || true; fi
  if [[ "$v03_active" == true ]]; then systemctl --user start crossalpha-state-v03.timer >/dev/null 2>&1 || true; fi
  if [[ "$v04_enabled" == true ]]; then systemctl --user enable crossalpha-state-v04.timer >/dev/null 2>&1 || true; fi
  if [[ "$v04_active" == true ]]; then systemctl --user start crossalpha-state-v04.timer >/dev/null 2>&1 || true; fi
  exit "$rc"
}
trap rollback ERR INT TERM

# Prevent concurrent writers before unified ownership begins.
systemctl --user disable --now crossalpha-materializer.timer >/dev/null 2>&1 || true
systemctl --user stop crossalpha-materializer.service >/dev/null 2>&1 || true
systemctl --user disable --now crossalpha-observatory.service >/dev/null 2>&1 || true
if [[ "$INCLUDE_V03" == true ]]; then
  systemctl --user disable --now crossalpha-state-v03.timer >/dev/null 2>&1 || true
  systemctl --user stop crossalpha-state-v03.service >/dev/null 2>&1 || true
fi
if [[ "$INCLUDE_V04" == true ]]; then
  systemctl --user disable --now crossalpha-state-v04.timer >/dev/null 2>&1 || true
  systemctl --user stop crossalpha-state-v04.service >/dev/null 2>&1 || true
fi

# Freeze the immutable Observatory audit baseline after old writers are stopped and
# before the new daemon can append anything.
AUDIT="$DATA_ROOT/manifests/raw_snapshots.jsonl"
BEFORE=0
[[ -f "$AUDIT" ]] && BEFORE="$(wc -l < "$AUDIT")"
AFTER="$BEFORE"

mkdir -p "$UNIT_DIR"
sed \
  -e "s|@REPO_DIR@|$REPO_DIR|g" \
  -e "s|@DATA_ROOT@|$DATA_ROOT|g" \
  -e "s|@COMPONENT_ARGS@|$COMPONENT_ARGS|g" \
  "$UNIT_TEMPLATE" > "$UNIT_DST"

systemctl --user daemon-reload
systemctl --user enable --now crossalpha-daemon.service

for _ in $(seq 1 80); do
  systemctl --user is-active --quiet crossalpha-daemon.service || {
    systemctl --user status crossalpha-daemon.service --no-pager >&2 || true
    false
  }
  [[ -f "$AUDIT" ]] && AFTER="$(wc -l < "$AUDIT")"
  DAEMON_OK=false; MATERIALIZER_OK=false; V03_OK=true; V04_OK=true
  [[ -f "$DATA_ROOT/manifests/crossalpha_daemon_health.json" ]] && \
    python3 - "$DATA_ROOT/manifests/crossalpha_daemon_health.json" <<'PY' >/dev/null 2>&1 && DAEMON_OK=true || true
import json, sys
v=json.load(open(sys.argv[1], encoding="utf-8"))
raise SystemExit(0 if v.get("status") == "running" else 1)
PY
  [[ -f "$DATA_ROOT/manifests/materializer_health.json" ]] && \
    python3 - "$DATA_ROOT/manifests/materializer_health.json" <<'PY' >/dev/null 2>&1 && MATERIALIZER_OK=true || true
import json, sys
v=json.load(open(sys.argv[1], encoding="utf-8"))
raise SystemExit(0 if v.get("ok") is True else 1)
PY
  if [[ "$INCLUDE_V03" == true ]]; then
    V03_OK=false
    [[ -f "$DATA_ROOT/manifests/state_v03_daemon_health.json" ]] && \
      python3 - "$DATA_ROOT/manifests/state_v03_daemon_health.json" <<'PY' >/dev/null 2>&1 && V03_OK=true || true
import json, sys
v=json.load(open(sys.argv[1], encoding="utf-8"))
raise SystemExit(0 if v.get("ok") is True else 1)
PY
  fi
  if [[ "$INCLUDE_V04" == true ]]; then
    V04_OK=false
    [[ -f "$DATA_ROOT/manifests/state_v04_daemon_health.json" ]] && \
      python3 - "$DATA_ROOT/manifests/state_v04_daemon_health.json" <<'PY' >/dev/null 2>&1 && V04_OK=true || true
import json, sys
v=json.load(open(sys.argv[1], encoding="utf-8"))
raise SystemExit(0 if v.get("ok") is True else 1)
PY
  fi
  if (( AFTER >= BEFORE + 3 )) && [[ "$DAEMON_OK" == true && "$MATERIALIZER_OK" == true && "$V03_OK" == true && "$V04_OK" == true ]]; then
    break
  fi
  sleep 2
done

(( AFTER >= BEFORE + 3 )) || { echo "Unified daemon did not append expected Observatory records" >&2; false; }
[[ "$DAEMON_OK" == true ]] || { echo "Unified daemon health not running" >&2; false; }
[[ "$MATERIALIZER_OK" == true ]] || { echo "Unified materializer health not green" >&2; false; }
[[ "$V03_OK" == true ]] || { echo "Unified State V0.3 health not green" >&2; false; }
[[ "$V04_OK" == true ]] || { echo "Unified State V0.4 health not green" >&2; false; }

trap - ERR INT TERM
echo "Unified Rust daemon cutover succeeded."
echo "audit_records_before=$BEFORE"
echo "audit_records_after=$AFTER"
echo "components=$COMPONENT_ARGS"
echo "rollback_meta=$META"
echo "Next retirement gate: .venv/bin/python scripts/run_rust_migration_acceptance.py --data-root '$DATA_ROOT' --post-cutover"
