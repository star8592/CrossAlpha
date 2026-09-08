#!/usr/bin/env bash
set -euo pipefail

printf 'CrossAlpha Codex Cloud setup\n'
printf 'cwd=%s\n' "$PWD"

if [ "$PWD" = "/mnt/disk2/CrossAlpha" ]; then
  echo 'refusing to use the production working tree' >&2
  exit 2
fi

if [ -e /mnt/disk2/CrossAlphaData/.crossalpha-ci-write-probe ]; then
  echo 'unexpected production-data write probe exists; refusing setup' >&2
  exit 2
fi

# Codex development environments must not inherit live service credentials.
unset TIINGO_API_TOKEN || true
unset FRED_API_KEY || true
unset BINANCE_API_KEY || true
unset BINANCE_API_SECRET || true

python3 --version
python3 -m pip install --upgrade pip
python3 -m pip install -e '.[dev]'

if command -v rustup >/dev/null 2>&1; then
  rustup toolchain install stable --profile minimal
  rustup component add --toolchain stable rustfmt clippy
  rustup default stable
else
  echo 'rustup is required for CrossAlpha Rust qualification' >&2
  exit 3
fi

rustc --version
cargo --version
cargo fmt --version
cargo clippy --version

# Fast, network-free sanity checks. Full qualification belongs to the task/CI phase.
cargo metadata --no-deps --format-version 1 >/dev/null
python3 - <<'PY'
import crossalpha
print('python_package_import=ok')
PY

printf 'codex_cloud_setup=ok\n'
