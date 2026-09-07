#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${CROSSALPHA_GITHUB_REPO_URL:-https://github.com/star8592/CrossAlpha}"
RUNNER_ROOT="${CROSSALPHA_RUNNER_ROOT:-/mnt/disk2/github-actions-runner/CrossAlpha}"
WORK_DIR="${CROSSALPHA_RUNNER_WORK_DIR:-/mnt/disk2/github-actions-work/CrossAlpha}"
RUNNER_NAME="${CROSSALPHA_RUNNER_NAME:-$(hostname)-crossalpha}"
RUNNER_LABELS="${CROSSALPHA_RUNNER_LABELS:-crossalpha}"

if [[ "${EUID}" -eq 0 ]]; then
  echo "error: run this installer as your normal login user, not root" >&2
  exit 2
fi

for command in curl tar python3; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "error: required command not found: $command" >&2
    exit 2
  fi
done

TOKEN="${GITHUB_RUNNER_TOKEN:-}"
if [[ -z "$TOKEN" ]]; then
  read -r -s -p "GitHub self-hosted runner registration token: " TOKEN
  echo
fi
if [[ -z "$TOKEN" ]]; then
  echo "error: registration token is required" >&2
  exit 2
fi

mkdir -p "$RUNNER_ROOT" "$WORK_DIR"
cd "$RUNNER_ROOT"

if [[ ! -x ./config.sh ]]; then
  echo "Resolving latest official actions/runner release..."
  release_json="$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest)"
  version="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"].removeprefix("v"))' <<<"$release_json")"
  archive="actions-runner-linux-x64-${version}.tar.gz"
  url="https://github.com/actions/runner/releases/download/v${version}/${archive}"

  echo "Downloading actions/runner v${version}..."
  curl -fL --retry 3 --retry-delay 2 -o "$archive" "$url"
  tar xzf "$archive"
  rm -f "$archive"
fi

if [[ -f .runner ]]; then
  echo "Runner is already configured in $RUNNER_ROOT"
else
  ./config.sh \
    --url "$REPO_URL" \
    --token "$TOKEN" \
    --name "$RUNNER_NAME" \
    --labels "$RUNNER_LABELS" \
    --work "$WORK_DIR" \
    --unattended \
    --replace
fi

service_pattern='actions.runner.star8592-CrossAlpha.*.service'
if systemctl list-unit-files "$service_pattern" --no-legend 2>/dev/null | grep -q '^actions\.runner\.'; then
  echo "Runner systemd service already installed."
else
  sudo ./svc.sh install "$USER"
fi

sudo ./svc.sh start
./svc.sh status || true

echo
echo "CrossAlpha GitHub self-hosted runner is configured."
echo "repo=$REPO_URL"
echo "runner_name=$RUNNER_NAME"
echo "labels=self-hosted,linux,x64,$RUNNER_LABELS"
echo "runner_root=$RUNNER_ROOT"
echo "work_dir=$WORK_DIR"
echo
echo "The repository workflow requires: [self-hosted, linux, x64, crossalpha]"
