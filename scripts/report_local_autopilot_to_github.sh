#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

RUN_DIR="${1:-}"
RUN_RC="${2:-1}"
REPORT_REPO="${CROSSALPHA_AUTOPILOT_REPORT_REPO:-star8592/CrossAlpha}"
REPORT_ISSUE="${CROSSALPHA_AUTOPILOT_REPORT_ISSUE:-2}"

[[ -n "$RUN_DIR" && -d "$RUN_DIR" ]] || {
  echo "reporter: run directory missing: $RUN_DIR" >&2
  exit 2
}
command -v gh >/dev/null 2>&1 || {
  echo "reporter: GitHub CLI 'gh' is not installed" >&2
  exit 2
}
gh auth status >/dev/null 2>&1 || {
  echo "reporter: GitHub CLI is not authenticated; run: gh auth login" >&2
  exit 2
}

SUMMARY="$RUN_DIR/summary.txt"
CURRENT_LOG=""
if [[ -f "$SUMMARY" ]]; then
  CURRENT_LOG="$(sed -n 's/^current_log=//p' "$SUMMARY" | tail -n 1)"
fi
if [[ -z "$CURRENT_LOG" || ! -f "$CURRENT_LOG" ]]; then
  CURRENT_LOG="$(find "$RUN_DIR" -maxdepth 1 -type f -name '*.log' -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n 1 | cut -d' ' -f2- || true)"
fi

ACCEPTANCE="${CROSSALPHA_DATA_DIR:-}/manifests/rust_migration_acceptance.json"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
RAW="$TMP_DIR/report_raw.txt"
SANITIZED="$TMP_DIR/report_sanitized.txt"
PAYLOAD="$TMP_DIR/payload.json"

status="FAILED"
run_id="$(basename "$RUN_DIR")"
step="unknown"
branch="$(git branch --show-current 2>/dev/null || true)"
head="$(git rev-parse HEAD 2>/dev/null || true)"
if [[ -f "$SUMMARY" ]]; then
  status="$(sed -n 's/^status=//p' "$SUMMARY" | tail -n 1)"
  run_id="$(sed -n 's/^run_id=//p' "$SUMMARY" | tail -n 1)"
  step="$(sed -n 's/^current_step=//p' "$SUMMARY" | tail -n 1)"
  branch="$(sed -n 's/^branch=//p' "$SUMMARY" | tail -n 1)"
  head="$(sed -n 's/^head=//p' "$SUMMARY" | tail -n 1)"
fi
[[ -n "$status" ]] || status="FAILED"
[[ -n "$run_id" ]] || run_id="$(basename "$RUN_DIR")"
[[ -n "$step" ]] || step="unknown"

r7_json='{}'
if [[ -f "$ACCEPTANCE" ]]; then
  r7_json="$(python3 - "$ACCEPTANCE" <<'PY'
import json, sys
p=sys.argv[1]
try:
    v=json.load(open(p, encoding='utf-8'))
except Exception:
    print('{}')
    raise SystemExit
keys=[
    'protocol','decision','daemon_cutover_allowed','python_retirement_allowed',
    'post_cutover','minimum_soak_seconds','soak_elapsed_seconds'
]
print(json.dumps({k:v.get(k) for k in keys if k in v}, sort_keys=True))
PY
)"
fi

TAIL_LINES=40
if [[ "$RUN_RC" != "0" ]]; then
  TAIL_LINES=140
fi

{
  echo "AUTOPILOT_REPORT_V1"
  echo "status=$status"
  echo "exit_code=$RUN_RC"
  echo "run_id=$run_id"
  echo "branch=$branch"
  echo "head=$head"
  echo "step=$step"
  echo "r7=$r7_json"
  echo
  echo "git_status:"
  git status --short 2>/dev/null || true
  echo
  echo "sanitized_tail_source=$(basename "${CURRENT_LOG:-none}")"
  echo "----- tail -----"
  if [[ -n "$CURRENT_LOG" && -f "$CURRENT_LOG" ]]; then
    tail -n "$TAIL_LINES" "$CURRENT_LOG" || true
  else
    echo "no local step log available"
  fi
} > "$RAW"

python3 - "$RAW" "$SANITIZED" "$REPO_DIR" "${CROSSALPHA_DATA_DIR:-}" <<'PY'
import re, sys
src, dst, repo_dir, data_root = sys.argv[1:5]
text=open(src, encoding='utf-8', errors='replace').read()

if repo_dir:
    text=text.replace(repo_dir, '<REPO_DIR>')
if data_root:
    text=text.replace(data_root, '<DATA_ROOT>')

# Local usernames/host paths are not needed for debugging public-source code.
text=re.sub(r'/home/[^/\s]+', '/home/<user>', text)
text=re.sub(r'(?m)^\([^\n)]*\)\s*[^\s@]+@[^:\s]+:', '(shell) <user>@<host>:', text)

# Known token families.
secret_patterns = [
    r'\bgh[pousr]_[A-Za-z0-9_]{8,}\b',
    r'\bgithub_pat_[A-Za-z0-9_]{8,}\b',
    r'\bsk-[A-Za-z0-9_-]{12,}\b',
    r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}',
]
for pattern in secret_patterns:
    text=re.sub(pattern, '<REDACTED_SECRET>', text)

# Secret-bearing assignments/diagnostics. Keep the variable name, never the value.
text=re.sub(
    r'(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|APIKEY|PRIVATE_KEY|ACCESS_KEY|RPC_URL)[A-Z0-9_]*)\s*([=:])\s*([^\s,;]+)',
    lambda m: f"{m.group(1)}{m.group(2)}<REDACTED>",
    text,
)
# Secret-like URL query parameters.
text=re.sub(
    r'(?i)([?&](?:api_key|apikey|token|access_token|key|secret|password)=)[^&\s]+',
    r'\1<REDACTED>',
    text,
)

# Keep public issue comments bounded. The full log remains local.
if len(text) > 24000:
    text=text[:12000] + '\n... <TRUNCATED_SANITIZED_REPORT> ...\n' + text[-12000:]
open(dst, 'w', encoding='utf-8').write(text)
PY

python3 - "$SANITIZED" "$PAYLOAD" <<'PY'
import json, sys
body=open(sys.argv[1], encoding='utf-8').read()
markdown=(
    '### CrossAlpha local autopilot report\n\n'
    '```text\n' + body.replace('```', '` ` `') + '\n```\n\n'
    '_Machine generated. Raw logs remain local and are not uploaded._\n'
)
json.dump({'body': markdown}, open(sys.argv[2], 'w', encoding='utf-8'))
PY

gh api --method POST "repos/$REPORT_REPO/issues/$REPORT_ISSUE/comments" --input "$PAYLOAD" >/dev/null

echo "reporter: sanitized autopilot report submitted to $REPORT_REPO issue #$REPORT_ISSUE"
