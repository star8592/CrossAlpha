from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from crossalpha.core import frozen_b3_v01
from crossalpha.core.free_paper import (
    _mark_path as _a_mark_path,
    _returns_path,
    _snapshot_path as _a_snapshot_path,
    _verify_sealed as _verify_a_sealed,
    create_paper_snapshot,
    freeze_paper_protocol,
    mark_paper_forward,
)
from crossalpha.state import ab_paper
from crossalpha.state.ab_paper import (
    _mark_path as _b_mark_path,
    _snapshot_path as _b_snapshot_path,
    _verify_sealed as _verify_b_sealed,
    create_state_ab_snapshot,
    freeze_state_ab_protocol,
    state_ab_integrity_report,
    state_ab_status,
    strict_mark_state_ab,
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
        "decisions": {frozen_b3_v01.STRATEGY: {"state": "PROMISING_BUT_UNPROVEN"}},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _synthetic_wide(start: str, end: str) -> pd.DataFrame:
    dates = pd.date_range(start, pd.Timestamp(end) - pd.Timedelta(days=1), freq="D", tz="UTC")
    data: dict[str, np.ndarray] = {}
    for i, asset in enumerate(frozen_b3_v01.RISK_ASSETS):
        x = np.arange(len(dates), dtype=float)
        data[asset] = 0.0007 + 0.0015 * np.sin((x + i) / 17.0)
    frame = pd.DataFrame(data, index=dates)
    frame["CASH"] = 0.00005
    return frame


def _write_long_returns(root: Path, start: str, end: str) -> Path:
    daily = _synthetic_wide(start, end)
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


def _setup_A(tmp_path: Path, *, start: str = "2025-01-01") -> None:
    historical_end = "2026-09-01"
    _final_decision(tmp_path, start, historical_end)
    freeze_paper_protocol(
        tmp_path,
        historical_start=start,
        historical_end=historical_end,
        now=datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc),
    )


def _fake_state(multiplier: float = 0.5, band: str = "SEVERE") -> dict[str, object]:
    return {
        "protocol": "CROSSALPHA_STATE_SHADOW_V0_1",
        "mode": "SHADOW_ONLY",
        "shadow_only": True,
        "core_protocol_mutated": False,
        "as_of": "2026-09-07T00:18:00+00:00",
        "generated_at": "2026-09-07T00:20:00+00:00",
        "state_band": band,
        "shadow_risk_multiplier": multiplier,
        "data_confidence": "FULL",
        "state_pressure": 0.9 if multiplier < 1 else 0.1,
        "leverage_pressure": 0.9 if multiplier < 1 else 0.1,
        "stablecoin_pressure": 0.0,
        "valid_source_components": 3,
        "expected_source_components": 3,
        "status": "computed",
    }


def test_ab_freeze_is_pre_first_live_and_immutable(tmp_path: Path) -> None:
    _setup_A(tmp_path)
    first = freeze_state_ab_protocol(
        tmp_path,
        now=datetime(2026, 9, 5, 7, 0, tzinfo=timezone.utc),
    )
    second = freeze_state_ab_protocol(
        tmp_path,
        now=datetime(2026, 9, 6, 7, 0, tzinfo=timezone.utc),
    )
    assert first["first_eligible_effective_date"] == "2026-09-07"
    assert first["status"] == "frozen"
    assert second["status"] == "already_frozen"
    assert first["record_sha256"] == second["record_sha256"]
    assert first["retrospective_backfill_allowed"] is False
    assert first["parameter_optimization_allowed"] is False


def test_ab_snapshot_links_A_and_uniformly_derisks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    start = "2025-01-01"
    _setup_A(tmp_path, start=start)
    freeze_state_ab_protocol(tmp_path, now=datetime(2026, 9, 5, 7, 0, tzinfo=timezone.utc))
    _write_long_returns(tmp_path, start, "2026-09-07")
    a = create_paper_snapshot(
        tmp_path,
        effective_date="2026-09-07",
        research_start=start,
        now=datetime(2026, 9, 7, 0, 20, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(ab_paper.shadow, "build_latest_shadow_state", lambda *args, **kwargs: _fake_state())
    b = create_state_ab_snapshot(
        tmp_path,
        effective_date="2026-09-07",
        now=datetime(2026, 9, 7, 0, 20, tzinfo=timezone.utc),
    )
    assert b["a_snapshot_record_sha256"] == a["record_sha256"]
    assert b["shadow_risk_multiplier"] == pytest.approx(0.5)
    assert b["b_risk_gross"] == pytest.approx(b["a_risk_gross"] * 0.5)
    for asset in frozen_b3_v01.RISK_ASSETS:
        assert b["b_weights"][asset] == pytest.approx(b["a_weights"][asset] * 0.5)
    assert sum(b["b_weights"].values()) == pytest.approx(1.0)
    persisted = json.loads(_b_snapshot_path(tmp_path, date(2026, 9, 7)).read_text(encoding="utf-8"))
    assert _verify_b_sealed(persisted)


def test_B_mark_reuses_exact_A_mark_returns_and_hash(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    start = "2025-01-01"
    _setup_A(tmp_path, start=start)
    freeze_state_ab_protocol(tmp_path, now=datetime(2026, 9, 5, 7, 0, tzinfo=timezone.utc))
    _write_long_returns(tmp_path, start, "2026-09-07")
    create_paper_snapshot(
        tmp_path,
        effective_date="2026-09-07",
        research_start=start,
        now=datetime(2026, 9, 7, 0, 20, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(ab_paper.shadow, "build_latest_shadow_state", lambda *args, **kwargs: _fake_state())
    b_snapshot = create_state_ab_snapshot(
        tmp_path,
        effective_date="2026-09-07",
        now=datetime(2026, 9, 7, 0, 20, tzinfo=timezone.utc),
    )
    _write_long_returns(tmp_path, start, "2026-09-08")
    mark_paper_forward(
        tmp_path,
        end="2026-09-08",
        research_start=start,
        now=datetime(2026, 9, 8, 4, 0, tzinfo=timezone.utc),
    )
    b = strict_mark_state_ab(tmp_path, end="2026-09-08")
    a = json.loads(_a_mark_path(tmp_path, date(2026, 9, 7)).read_text(encoding="utf-8"))
    persisted_b = json.loads(_b_mark_path(tmp_path, date(2026, 9, 7)).read_text(encoding="utf-8"))
    assert _verify_a_sealed(a)
    assert _verify_b_sealed(persisted_b)
    assert b["a_mark_record_sha256"] == a["record_sha256"]
    assert b["asset_returns"] == a["asset_returns"]
    expected_gross = sum(
        float(b_snapshot["b_weights"][asset]) * float(a["asset_returns"][asset])
        for asset in frozen_b3_v01.ALL_ASSETS
    )
    assert b["gross_return"] == pytest.approx(expected_gross)
    assert b["turnover"] == pytest.approx(float(b_snapshot["b_risk_gross"]))
    assert b["created_marks"] == 1


def test_ab_gap_is_never_backfilled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    start = "2025-01-01"
    _setup_A(tmp_path, start=start)
    freeze_state_ab_protocol(tmp_path, now=datetime(2026, 9, 5, 7, 0, tzinfo=timezone.utc))
    _write_long_returns(tmp_path, start, "2026-09-07")
    create_paper_snapshot(
        tmp_path,
        effective_date="2026-09-07",
        research_start=start,
        now=datetime(2026, 9, 7, 0, 20, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(ab_paper.shadow, "build_latest_shadow_state", lambda *args, **kwargs: _fake_state())
    create_state_ab_snapshot(
        tmp_path,
        effective_date="2026-09-07",
        now=datetime(2026, 9, 7, 0, 20, tzinfo=timezone.utc),
    )
    _write_long_returns(tmp_path, start, "2026-09-08")
    mark_paper_forward(
        tmp_path,
        end="2026-09-08",
        research_start=start,
        now=datetime(2026, 9, 8, 4, 0, tzinfo=timezone.utc),
    )
    strict_mark_state_ab(tmp_path, end="2026-09-08")
    with pytest.raises(RuntimeError, match="STATE_AB_LEDGER_GAP"):
        strict_mark_state_ab(tmp_path, end="2026-09-10")


def test_ab_integrity_and_status_before_first_mark(tmp_path: Path) -> None:
    _setup_A(tmp_path)
    freeze_state_ab_protocol(tmp_path, now=datetime(2026, 9, 5, 7, 0, tzinfo=timezone.utc))
    integrity = state_ab_integrity_report(tmp_path)
    status = state_ab_status(tmp_path)
    assert integrity["ok"] is True
    assert integrity["mark_count"] == 0
    assert status["state"] == "FROZEN_AWAITING_FIRST_LIVE_AB_SNAPSHOT"
    assert status["parameter_optimization_allowed"] is False
    assert status["retrospective_backfill_allowed"] is False


def test_ab_yaml_matches_frozen_implementation() -> None:
    root = Path(__file__).resolve().parents[1]
    spec = yaml.safe_load((root / "config" / "state_ab_v01.yaml").read_text(encoding="utf-8"))
    assert spec["protocol"] == ab_paper.AB_PROTOCOL
    assert spec["mode"] == ab_paper.MODE
    assert spec["weekly_decision"]["allowed_multipliers"] == list(ab_paper.ALLOWED_MULTIPLIERS)
    assert spec["promotion_gate"]["minimum_comparable_days"] == ab_paper.MIN_COMPARABLE_DAYS
    assert spec["promotion_gate"]["minimum_intervention_days"] == ab_paper.MIN_INTERVENTION_DAYS
    assert (
        spec["promotion_gate"]["max_cumulative_return_sacrifice"]
        == ab_paper.MAX_CUMULATIVE_RETURN_SACRIFICE
    )
