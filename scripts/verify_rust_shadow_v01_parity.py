#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from crossalpha.state import shadow
from crossalpha.storage.indexes import load_recent_daily_manifests
from verify_rust_canonical_materializer import _freeze_recent_daily_manifests
from verify_rust_feature_parity import _python_hyperliquid, _python_stablecoins

REPO_ROOT = Path(__file__).resolve().parents[1]
RUST = REPO_ROOT / "target" / "debug" / "crossalpha-ab-rs"
RECENT_DAYS = 2


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        if stamp.tzinfo is None:
            return value
        return stamp.astimezone(timezone.utc).isoformat()
    return value


def _diff(left: Any, right: Any, path: str = "$") -> list[str]:
    if isinstance(left, dict) and isinstance(right, dict):
        errors: list[str] = []
        if set(left) != set(right):
            errors.append(
                f"{path}: keys differ rust={sorted(left)} python={sorted(right)}"
            )
            return errors
        for key in sorted(left):
            errors.extend(_diff(left[key], right[key], f"{path}.{key}"))
        return errors
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return [f"{path}: length rust={len(left)} python={len(right)}"]
        errors: list[str] = []
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            errors.extend(_diff(a, b, f"{path}[{index}]"))
        return errors
    if isinstance(left, (int, float)) and not isinstance(left, bool) and isinstance(
        right, (int, float)
    ) and not isinstance(right, bool):
        if math.isclose(float(left), float(right), rel_tol=1e-10, abs_tol=1e-12):
            return []
        return [f"{path}: rust={left!r} python={right!r}"]
    return [] if left == right else [f"{path}: rust={left!r} python={right!r}"]


def _python_from_frozen_inputs(
    frozen_root: Path,
    generated_at: datetime,
) -> dict[str, Any]:
    records, errors = load_recent_daily_manifests(frozen_root, days=RECENT_DAYS)
    if errors:
        raise RuntimeError(f"frozen daily manifests contain errors: {errors}")

    hyper = pd.DataFrame(_python_hyperliquid(records, ["BTC", "ETH"]))
    stable_rows, _ = _python_stablecoins(records)
    stable = pd.DataFrame(stable_rows)
    if hyper.empty and stable.empty:
        return {
            "protocol": shadow.PROTOCOL,
            "mode": shadow.MODE,
            "status": "no_inputs",
            "written": False,
        }

    maxima: list[pd.Timestamp] = []
    for frame in (hyper, stable):
        if not frame.empty:
            maxima.append(pd.to_datetime(frame["observed_at"], utc=True).max())
    as_of = min(maxima) if len(maxima) > 1 else maxima[0]
    value = shadow.compute_shadow_state(
        hyper,
        stable,
        as_of=as_of,
        generated_at=generated_at,
    )
    return {**value, "status": "computed", "written": False}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare Python and Rust State Shadow V0.1 on identical frozen inputs"
    )
    parser.add_argument("--data-root", type=Path, required=True)
    args = parser.parse_args()
    data_root = args.data_root.resolve()
    if not RUST.exists():
        raise SystemExit(f"Rust A/B binary missing: {RUST}")

    generated_at = datetime.now(timezone.utc).replace(microsecond=0)
    with tempfile.TemporaryDirectory(prefix="crossalpha-shadow-v01-parity-") as tmp:
        frozen_root = Path(tmp) / "frozen"
        _freeze_recent_daily_manifests(data_root, frozen_root, RECENT_DAYS)
        python_value = _python_from_frozen_inputs(frozen_root, generated_at)
        completed = subprocess.run(
            [
                str(RUST),
                "shadow-preview",
                "--generated-at",
                generated_at.isoformat(),
                "--data-root",
                str(frozen_root),
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise SystemExit(
                f"Rust shadow preview failed rc={completed.returncode}: {completed.stderr}"
            )
        rust_value = json.loads(completed.stdout)

    mismatches = _diff(_normalize(rust_value), _normalize(python_value))
    if mismatches:
        print("ok=false")
        for mismatch in mismatches[:50]:
            print(f"mismatch={mismatch}")
        return 1
    print(
        "ok=true mismatches=0 protocol=CROSSALPHA_STATE_SHADOW_V0_1 "
        f"generated_at={generated_at.isoformat()} recent_days={RECENT_DAYS}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
