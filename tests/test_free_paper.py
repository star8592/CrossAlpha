from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from crossalpha.core import frozen_b3_v01
from crossalpha.core.free_baselines import (
    FreeBaselineConfig,
    _compute_features,
    _targets_for_date,
)
from crossalpha.core.free_paper import (
    _mark_path,
    _returns_path,
    _verify_sealed,
    create_paper_snapshot,
    freeze_paper_protocol,
    mark_paper_forward,
)


def _final_decision(root: Path, start: str, end: str) -> Path:
    path = (
        root
        / "research"
        / "free_v01"
        / "final_evaluation"
        / f"start={start}"
        / f"end={end}"
        / "final_decision.json"
    )
    params = frozen_b3_v01.frozen_parameters()
    payload = {
        "protocol": frozen_b3_v01.PROTOCOL,
        "core_candidate": frozen_b3_v01.STRATEGY,
        "candidate_config_frozen": True,
        "parameter_optimization_allowed": False,
        "candidate_parameters": {
            "trend_window_calendar_days": params["trend_window_calendar_days"],
            "vol_window_calendar_days": params["vol_window_calendar_days"],
            "target_vol": params["target_vol"],
            "rebalance_weekday": params["rebalance_weekday"],
            "execution_lag_calendar_days": params["execution_lag_calendar_days"],
            "one_way_cost_bps": params["one_way_cost_bps"],
            "shorting": params["shorting"],
            "leverage_cap": params["leverage_cap"],
        },
        "decisions": {
            frozen_b3_v01.STRATEGY: {
                "state": "PROMISING_BUT_UNPROVEN",
            }
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(__import__("json").dumps(payload), encoding="utf-8")
    return path


def _synthetic_wide(start: str, end: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    dates = pd.date_range(start, pd.Timestamp(end) - pd.Timedelta(days=1), freq="D", tz="UTC")
    data: dict[str, np.ndarray] = {}
    for i, asset in enumerate(frozen_b3_v01.RISK_ASSETS):
        x = np.arange(len(dates), dtype=float)
        data[asset] = 0.0007 + 0.0015 * np.sin((x + i) / 17.0)
    risk = pd.DataFrame(data, index=dates)
    daily = risk.copy()
    daily["CASH"] = 0.00005
    available = pd.DataFrame(True, index=dates, columns=frozen_b3_v01.RISK_ASSETS)
    return daily, available


def _write_long_returns(root: Path, start: str, end: str) -> Path:
    daily, _ = _synthetic_wide(start, end)
    rows: list[dict[str, object]] = []
    for timestamp, row in daily.iterrows():
        for asset in frozen_b3_v01.ALL_ASSETS:
            rows.append(
                {
                    "date": timestamp,
                    "economic_asset": asset,
                    "source": "synthetic",
                    "symbol": asset,
                    "price": 100.0 if asset != "CASH" else None,
                    "return": float(row[asset]),
                }
            )
    path = _returns_path(root, start=start, end=end)
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(path, index=False)
    return path


def test_frozen_b3_matches_generic_b3_target() -> None:
    daily, available = _synthetic_wide("2025-01-01", "2026-09-07")
    signal = daily.index[-1]
    risk = daily.loc[:, list(frozen_b3_v01.RISK_ASSETS)]
    config = FreeBaselineConfig()
    features = _compute_features(risk, config)
    generic = _targets_for_date(
        signal + pd.Timedelta(days=1),
        signal,
        risk,
        available,
        features,
        config,
    )[frozen_b3_v01.STRATEGY]
    frozen = frozen_b3_v01.compute_target(daily, available, signal_date=signal)
    pd.testing.assert_series_equal(generic, frozen, check_names=False, atol=1e-12, rtol=1e-12)


def test_paper_freeze_is_immutable_and_sets_next_monday(tmp_path: Path) -> None:
    start = "2025-01-01"
    historical_end = "2026-09-01"
    _final_decision(tmp_path, start, historical_end)
    now = datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc)
    first = freeze_paper_protocol(
        tmp_path,
        historical_start=start,
        historical_end=historical_end,
        now=now,
    )
    second = freeze_paper_protocol(
        tmp_path,
        historical_start=start,
        historical_end=historical_end,
        now=datetime(2026, 9, 6, 6, 0, tzinfo=timezone.utc),
    )
    assert first["first_eligible_effective_date"] == "2026-09-07"
    assert first["record_sha256"] == second["record_sha256"]
    assert second["status"] == "already_frozen"
    assert _verify_sealed(first)


def test_snapshot_is_live_only_and_immutable(tmp_path: Path) -> None:
    start = "2025-01-01"
    historical_end = "2026-09-01"
    _final_decision(tmp_path, start, historical_end)
    freeze_paper_protocol(
        tmp_path,
        historical_start=start,
        historical_end=historical_end,
        now=datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc),
    )
    _write_long_returns(tmp_path, start, "2026-09-07")
    created = create_paper_snapshot(
        tmp_path,
        effective_date="2026-09-07",
        research_start=start,
        now=datetime(2026, 9, 7, 0, 20, tzinfo=timezone.utc),
    )
    repeated = create_paper_snapshot(
        tmp_path,
        effective_date="2026-09-07",
        research_start=start,
        now=datetime(2026, 9, 7, 0, 25, tzinfo=timezone.utc),
    )
    assert created["status"] == "created"
    assert repeated["status"] == "already_exists"
    assert created["record_sha256"] == repeated["record_sha256"]
    assert created["signal_date"] == "2026-09-06"
    assert sum(created["weights"].values()) == pytest.approx(1.0)
    with pytest.raises(ValueError, match="cannot be backfilled"):
        create_paper_snapshot(
            tmp_path,
            effective_date="2026-09-07",
            research_start=start,
            now=datetime(2026, 9, 8, 0, 20, tzinfo=timezone.utc),
        )


def test_forward_mark_charges_initial_turnover_and_never_rewrites(tmp_path: Path) -> None:
    start = "2025-01-01"
    historical_end = "2026-09-01"
    _final_decision(tmp_path, start, historical_end)
    freeze_paper_protocol(
        tmp_path,
        historical_start=start,
        historical_end=historical_end,
        now=datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc),
    )
    _write_long_returns(tmp_path, start, "2026-09-07")
    snapshot = create_paper_snapshot(
        tmp_path,
        effective_date="2026-09-07",
        research_start=start,
        now=datetime(2026, 9, 7, 0, 20, tzinfo=timezone.utc),
    )
    _write_long_returns(tmp_path, start, "2026-09-08")
    first = mark_paper_forward(
        tmp_path,
        end="2026-09-08",
        research_start=start,
        now=datetime(2026, 9, 8, 4, 0, tzinfo=timezone.utc),
    )
    second = mark_paper_forward(
        tmp_path,
        end="2026-09-08",
        research_start=start,
        now=datetime(2026, 9, 8, 5, 0, tzinfo=timezone.utc),
    )
    mark = __import__("json").loads(
        _mark_path(tmp_path, date(2026, 9, 7)).read_text(encoding="utf-8")
    )
    assert first["created_marks"] == 1
    assert second["created_marks"] == 0
    assert mark["turnover"] == pytest.approx(float(snapshot["risk_gross"]))
    assert mark["cost"] > 0
    assert _verify_sealed(mark)
