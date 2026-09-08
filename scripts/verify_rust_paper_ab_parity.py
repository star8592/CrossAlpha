#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import subprocess
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from crossalpha.core import frozen_b3_v01
from crossalpha.state import shadow

REPO_ROOT = Path(__file__).resolve().parents[1]
RUST = REPO_ROOT / "target" / "debug" / "crossalpha-kernel-rs"
RISK_ASSETS = tuple(frozen_b3_v01.RISK_ASSETS)
ALL_ASSETS = tuple(frozen_b3_v01.ALL_ASSETS)


def _rust(command: str, payload: dict[str, object], root: Path) -> dict[str, float]:
    path = root / f"{command}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run(
        [str(RUST), command, str(path)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Rust {command} failed rc={completed.returncode}: {completed.stderr}"
        )
    return json.loads(completed.stdout)


def _fixture() -> tuple[dict[str, object], pd.DataFrame, pd.DataFrame]:
    start = date(2025, 6, 1)
    end = date(2026, 9, 1)
    signal = end - timedelta(days=1)
    calendar = pd.date_range(start=start, end=end, inclusive="left", freq="D", tz="UTC")
    rows: list[dict[str, object]] = []

    inception_offsets = {
        "BTC": 30,
        "ETH": 125,
    }
    base_returns = {
        "US_EQUITY": 0.00045,
        "US_GROWTH": 0.00055,
        "GOLD": 0.00030,
        "SILVER": 0.00035,
        "COPPER": 0.00020,
        "WTI": -0.00025,
        "BTC": 0.00070,
        "ETH": 0.00080,
    }
    tradfi = set(RISK_ASSETS[:6])

    for asset in RISK_ASSETS:
        inception = start + timedelta(days=inception_offsets.get(asset, 0))
        first = True
        for stamp in calendar:
            day = stamp.date()
            if day < inception:
                continue
            # Exercise closed-market zero filling for TradFi without changing
            # the economic calendar semantics used by the frozen strategy.
            if asset in tradfi and stamp.weekday() >= 5:
                continue
            daily_return = None if first else base_returns[asset]
            first = False
            rows.append(
                {
                    "date": datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).isoformat(),
                    "economic_asset": asset,
                    "source": "fixture",
                    "symbol": asset,
                    "price": 100.0,
                    "daily_return": daily_return,
                }
            )

    for stamp in calendar:
        rows.append(
            {
                "date": stamp.to_pydatetime().isoformat(),
                "economic_asset": "CASH",
                "source": "fred",
                "symbol": "DGS3MO",
                "price": None,
                "daily_return": 0.00010,
            }
        )

    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame["date"], utc=True)
    daily = pd.DataFrame(index=calendar, columns=ALL_ASSETS, dtype=float)
    available = pd.DataFrame(False, index=calendar, columns=RISK_ASSETS, dtype=bool)
    for asset in RISK_ASSETS:
        part = frame.loc[frame["economic_asset"] == asset, ["date", "daily_return"]].copy()
        part = part.sort_values("date")
        if part.empty:
            continue
        inception = part["date"].min()
        values = pd.to_numeric(part.set_index("date")["daily_return"], errors="coerce").reindex(calendar)
        active = calendar >= inception
        values.loc[active] = values.loc[active].fillna(0.0)
        daily[asset] = values
        available.loc[active, asset] = True
    cash = pd.to_numeric(
        frame.loc[frame["economic_asset"] == "CASH"].set_index("date")["daily_return"],
        errors="coerce",
    ).reindex(calendar)
    daily["CASH"] = cash.ffill().fillna(0.0)

    return (
        {
            "rows": rows,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "signal_date": signal.isoformat(),
        },
        daily,
        available,
    )


def _assert_close(left: dict[str, float], right: dict[str, float], label: str) -> None:
    if set(left) != set(right):
        raise AssertionError(f"{label} keys differ: rust={sorted(left)} python={sorted(right)}")
    for key in sorted(left):
        a = float(left[key])
        b = float(right[key])
        if not math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-12):
            raise AssertionError(f"{label}.{key}: rust={a!r} python={b!r}")


def main() -> int:
    if not RUST.exists():
        raise SystemExit(f"Rust kernel binary missing: {RUST}")
    with tempfile.TemporaryDirectory(prefix="crossalpha-paper-parity-") as tmp:
        root = Path(tmp)
        fixture, daily, available = _fixture()
        signal = pd.Timestamp(fixture["signal_date"], tz="UTC")
        python_weights = frozen_b3_v01.compute_target(
            daily,
            available,
            signal_date=signal,
        ).to_dict()
        rust_weights = _rust("paper-target", fixture, root)
        _assert_close(rust_weights, python_weights, "paper_target")

        for multiplier in (1.0, 0.75, 0.50):
            payload = {"weights": python_weights, "multiplier": multiplier}
            rust_b = _rust(f"ab-multiplier", payload, root)
            python_b = shadow.apply_shadow_multiplier(
                pd.Series(python_weights, dtype=float), multiplier
            ).to_dict()
            _assert_close(rust_b, python_b, f"ab_multiplier_{multiplier}")

    print(
        "ok=true paper_target=true ab_multipliers=3 "
        "pre_inception_and_closed_market_semantics=true"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
