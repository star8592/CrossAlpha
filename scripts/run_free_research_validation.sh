#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

START="${CROSSALPHA_RESEARCH_START:-2010-06-01}"
END="${CROSSALPHA_RESEARCH_END:-2026-09-01}"
BOOTSTRAP="${CROSSALPHA_BOOTSTRAP_REPLICATIONS:-2000}"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=================================================="
echo "CrossAlpha FREE V0.1 validation"
echo "start=$START end=$END bootstrap=$BOOTSTRAP"
echo "data_cost_usd=0"
echo "=================================================="

python -m pip install -e ".[dev]"
pytest -q

crossalpha-free-audit --start "$START" --end "$END"
crossalpha-free-returns --start "$START" --end "$END"
crossalpha-free-baselines --start "$START" --end "$END" --cost-bps 5
crossalpha-free-robustness --start "$START" --end "$END"
crossalpha-free-robustness2 --start "$START" --end "$END" --bootstrap "$BOOTSTRAP" --seed 8592

python - <<'PY'
import json
from pathlib import Path

root = Path("/mnt/disk2/CrossAlphaData/research/free_v01")
start = "2010-06-01"
end = "2026-09-01"

baseline_path = root / "baselines" / f"start={start}" / f"end={end}" / "summary.json"
stage1_path = root / "robustness_stage1" / f"start={start}" / f"end={end}" / "summary.json"
stage2_path = root / "robustness_stage2" / f"start={start}" / f"end={end}" / "summary.json"

baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
stage1 = json.loads(stage1_path.read_text(encoding="utf-8"))
stage2 = json.loads(stage2_path.read_text(encoding="utf-8"))

print("\n==================================================")
print("FINAL VALIDATION SCREEN")
print("==================================================")

for strategy in (
    "B3_ABSOLUTE_TREND_EQUAL_WEIGHT",
    "B4_ABSOLUTE_TREND_INVERSE_VOLATILITY",
):
    b = baseline["strategies"][strategy]
    s1 = stage1["focus_screen"][strategy]
    s2 = stage2["focus_screen"][strategy]
    print(f"\n{strategy}")
    print(f"  baseline CAGR              = {b['cagr']:.6f}")
    print(f"  baseline Sharpe            = {b['sharpe_excess_cash']:.6f}")
    print(f"  baseline MaxDD             = {b['max_drawdown']:.6f}")
    print(f"  stage1 median Sharpe       = {s1['median_scenario_sharpe']:.6f}")
    print(f"  stage1 worst Sharpe        = {s1['worst_scenario_sharpe']:.6f}")
    print(f"  stage1 positive scenarios  = {s1['positive_sharpe_share']:.6f}")
    print(f"  stage2 bootstrap P(SR>0)   = {s2['bootstrap_63d_prob_sharpe_gt_0']:.6f}")
    print(f"  stage2 DSR probability     = {s2['dsr_probability']:.6f}")
    print(f"  stage2 PBO                 = {s2['pbo']:.6f}")
    print(f"  stage2 WF OOS Sharpe       = {s2['walk_forward_selected_oos_sharpe']:.6f}")
    print(f"  stage2 positive WF folds   = {s2['walk_forward_positive_fold_share']:.6f}")
    print(f"  SURVIVES STAGE 2           = {s2['survives_stage2']}")

print("\nStage 2 is a falsification screen, not final proof of alpha.")
PY

echo
echo "=================================================="
echo "FREE V0.1 VALIDATION COMPLETE"
echo "=================================================="
