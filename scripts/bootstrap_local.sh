#!/usr/bin/env bash
set -euo pipefail
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev,databento]"
[ -f .env ] || cp .env.example .env
mkdir -p data/raw data/canonical data/derived data/manifests
pytest -q
echo
echo "Bootstrap complete. Next:"
echo "  1) edit .env"
echo "  2) crossalpha collect-observatory"
echo "  3) crossalpha fetch-core --start 2010-06-01"
