from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from crossalpha.core.contracts import normalize_parent_futures_daily  # noqa: E402
from crossalpha.core.free_baselines import (  # noqa: E402
    FreeBaselineConfig,
    _apply_constraints,
    _compute_features,
)
from crossalpha.core.futures_roll import build_roll_mtm_returns  # noqa: E402
from crossalpha.core.roll_map import build_previous_volume_roll_map  # noqa: E402
from crossalpha.outcomes.linkage import _outcome_metrics  # noqa: E402
from verify_rust_canonical_parser_parity import _diff, _normalize  # noqa: E402


def _run(binary: Path, command: str, input_path: Path) -> Any:
    result = subprocess.run(
        [str(binary), command, str(input_path)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Rust kernel {command} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _iso(ts: str) -> str:
    return pd.Timestamp(ts).isoformat().replace("+00:00", "Z")


def data_fixture() -> dict[str, Any]:
    return {
        "bars": [
            {"ts_event": _iso("2026-01-02T00:00:00Z"), "instrument_id": 1, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10.0},
            {"ts_event": _iso("2026-01-03T00:00:00Z"), "instrument_id": 1, "open": 100.5, "high": 102.0, "low": 100.0, "close": 101.5, "volume": 20.0},
        ],
        "definitions": [
            {"ts_recv": _iso("2026-01-01T00:00:00Z"), "instrument_id": 1, "raw_symbol": "F1", "expiration": _iso("2026-02-20T00:00:00Z"), "instrument_class": "F", "asset": "WTI"},
            {"ts_recv": _iso("2026-01-04T00:00:00Z"), "instrument_id": 1, "raw_symbol": "F1_FUTURE_REVISION", "expiration": _iso("2026-03-20T00:00:00Z"), "instrument_class": "F", "asset": "WTI"},
        ],
    }


def python_data(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    bars = pd.DataFrame(fixture["bars"])
    defs = pd.DataFrame(fixture["definitions"])
    frame = normalize_parent_futures_daily(bars, defs)
    columns = ["date", "contract", "instrument_id", "expiration_date", "definition_known_at", "open", "high", "low", "close", "volume", "asset"]
    return frame.reindex(columns=columns).to_dict(orient="records")


def roll_fixture() -> dict[str, Any]:
    dates = pd.date_range("2026-01-01", periods=5, tz="UTC", freq="D")
    metadata = [
        {"contract": "F1", "expiration_date": _iso("2026-01-10T00:00:00Z")},
        {"contract": "F2", "expiration_date": _iso("2026-02-10T00:00:00Z")},
    ]
    rows = []
    for i, date in enumerate(dates):
        rows.extend(
            [
                {"date": date.isoformat().replace("+00:00", "Z"), "contract": "F1", "close": 100.0 + i, "volume": 100.0 - i * 10},
                {"date": date.isoformat().replace("+00:00", "Z"), "contract": "F2", "close": 200.0 + i * 2, "volume": 10.0 + i * 50},
            ]
        )
    return {"bars": rows, "metadata": metadata, "safety_days": 5, "roll_cost_bps": 3.0}


def python_roll(fixture: dict[str, Any]) -> dict[str, Any]:
    bars = pd.DataFrame(fixture["bars"])
    metadata = pd.DataFrame(fixture["metadata"])
    roll_map = build_previous_volume_roll_map(
        bars[["date", "contract", "volume"]], metadata, safety_days=fixture["safety_days"]
    )
    result = build_roll_mtm_returns(
        bars[["date", "contract", "close"]],
        roll_map[["date", "contract"]],
        roll_cost_bps=fixture["roll_cost_bps"],
    ).frame.reset_index()
    return {
        "roll_map": roll_map.to_dict(orient="records"),
        "returns": result.to_dict(orient="records"),
    }


def baseline_fixture() -> dict[str, Any]:
    returns = [0.001 * math.sin(index / 11.0) + 0.0002 for index in range(420)]
    return {
        "returns": returns,
        "raw_weights": {
            "US_EQUITY": 1.0,
            "US_GROWTH": 1.0,
            "GOLD": 1.0,
            "SILVER": 1.0,
            "COPPER": 1.0,
            "WTI": 1.0,
            "BTC": 1.0,
            "ETH": 1.0,
        },
    }


def python_baseline(fixture: dict[str, Any]) -> dict[str, Any]:
    series = pd.DataFrame({"X": fixture["returns"]})
    config = FreeBaselineConfig()
    features = _compute_features(series, config)
    weights = _apply_constraints(pd.Series(fixture["raw_weights"], dtype=float), config)
    return {
        "features": {
            "vol": features["vol"]["X"].tolist(),
            "trend": features["trend"]["X"].tolist(),
            "horizon_30": features["horizon_returns"][30]["X"].tolist(),
            "horizon_90": features["horizon_returns"][90]["X"].tolist(),
            "horizon_180": features["horizon_returns"][180]["X"].tolist(),
            "horizon_365": features["horizon_returns"][365]["X"].tolist(),
            "multi_score": features["multi_score"]["X"].tolist(),
        },
        "weights": weights.to_dict(),
    }


def outcomes_fixture() -> dict[str, Any]:
    dates = ["2026-09-07", "2026-09-08", "2026-09-09"]
    a = []
    b = []
    for index, day in enumerate(dates):
        a_hash = f"a{index}"
        a.append({"date": day, "record_sha256": a_hash, "net_return": [0.01, -0.02, 0.005][index], "cash_return": 0.0001, "path": f"/a/{day}"})
        b.append({"date": day, "record_sha256": f"b{index}", "net_return": [0.008, -0.01, 0.004][index], "cash_return": 0.0001, "a_mark_record_sha256": a_hash, "shadow_risk_multiplier": [0.8, 0.5, 1.0][index], "path": f"/b/{day}"})
    return {"dates": dates, "a_marks": a, "b_marks": b}


def python_outcomes(fixture: dict[str, Any]) -> dict[str, Any]:
    a = {pd.Timestamp(row["date"]).date(): {**row, "__path": row["path"]} for row in fixture["a_marks"]}
    b = {pd.Timestamp(row["date"]).date(): {**row, "__path": row["path"]} for row in fixture["b_marks"]}
    metrics = _outcome_metrics([pd.Timestamp(day).date() for day in fixture["dates"]], a, b)
    # Link arrays are evidence plumbing rather than math kernel; compare scalar contract here.
    metrics.pop("A_mark_links")
    metrics.pop("B_mark_links")
    return metrics


def main() -> int:
    binary = REPO_ROOT / "target" / "debug" / "crossalpha-kernel-rs"
    if not binary.exists():
        print(f"ok=false mismatches=1 error=Rust binary missing: {binary}")
        return 1
    mismatches: list[str] = []
    with tempfile.TemporaryDirectory(prefix="crossalpha-r5-parity-") as tmp:
        root = Path(tmp)
        fixtures = [
            ("data-normalize", data_fixture(), python_data, "$.data"),
            ("futures-roll", roll_fixture(), python_roll, "$.futures"),
            ("baseline", baseline_fixture(), python_baseline, "$.baseline"),
            ("outcomes", outcomes_fixture(), python_outcomes, "$.outcomes"),
        ]
        for index, (command, fixture, python_fn, prefix) in enumerate(fixtures):
            path = root / f"fixture-{index}.json"
            path.write_text(json.dumps(fixture), encoding="utf-8")
            expected = _normalize(python_fn(fixture))
            actual = _normalize(_run(binary, command, path))
            mismatches.extend(_diff(expected, actual, prefix, limit=max(1, 100 - len(mismatches))))
            if len(mismatches) >= 100:
                break
    print(f"ok={str(not mismatches).lower()} mismatches={len(mismatches)} data=1 futures=1 baseline=1 outcomes=1")
    for mismatch in mismatches[:100]:
        print(f"mismatch={mismatch}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
