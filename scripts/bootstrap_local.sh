#!/usr/bin/env bash
set -euo pipefail

python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"

[ -f .env ] || cp .env.example .env

DATA_DIR="$(python - <<'PY'
from dotenv import dotenv_values
from pathlib import Path
cfg = dotenv_values('.env')
print(Path(cfg.get('CROSSALPHA_DATA_DIR') or './data').expanduser())
PY
)"

mkdir -p "$DATA_DIR"/{raw,canonical,derived,manifests,research,archive,catalog}
pytest -q

echo
echo "Bootstrap complete."
echo "Repository: $(pwd)"
echo "Data root:  $DATA_DIR"
echo "Next:"
echo "  1) edit .env and add free TIINGO_API_TOKEN + FRED_API_KEY when ready"
echo "  2) crossalpha collect-observatory"
echo "  3) bash scripts/install_user_service.sh"
echo "  4) crossalpha fetch-core-free --start 2010-06-01 --end 2026-09-01"
echo
echo "Databento is optional paid validation only and is not installed by default."
