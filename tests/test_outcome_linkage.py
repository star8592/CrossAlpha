from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from crossalpha.core import free_paper
from crossalpha.outcomes import linkage, prospective
from crossalpha.outcomes.integrity import strict_outcome_linkage_integrity_report
from crossalpha.state import ab_paper, v02_prospective


FREEZE_TIME = pd.Timestamp("2026-09-05T00:00:00Z")


def _references(root: Path) -> None:
    for name, path in prospective.reference_paths(root).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"protocol": name, "frozen": True}), encoding="utf-8")


def _write_source(root: Path, known_at: pd.Timestamp) -> Path:
    path = (
        root
        / "research"
        / "state_v02"
        / "prospective"
        / f"year={known_at:%Y}"
        / f"month={known_at:%m}"
        / f"day={known_at:%d}"
        / f"state_at={known_at:%H%M%S%f}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = v02_prospective._seal(
        {
            "protocol": v02_prospective.PROSPECTIVE_PROTOCOL,
            "state_protocol": "CROSSALPHA_STATE_V0_2",
            "known_at": known_at.isoformat(),
            "generated_at": (known_at - pd.Timedelta(minutes=1)).isoformat(),
            "data_confidence": "FULL",
            "descriptive_stress_score": 0.25,
        }
    )
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _mark_paths(root: Path, day: date) -> tuple[Path, Path]:
    a = (
        root
        / "research"
        / "free_v01"
        / "paper"
        / "marks"
        / f"year={day.year:04d}"
        / f"month={day.month:02d}"
        / f"date={day.isoformat()}.json"
    )
    b = (
        root
        / "research"
        / "free_v01"
        / "state_ab_v01"
        / "marks"
        / f"year={day.year:04d}"
        / f"month={day.month:02d}"
        / f"date={day.isoformat()}.json"
    )
    return a, b


def _write_marks(root: Path, day: date, a_return: float, b_return: float, multiplier: float) -> None:
    a_path, b_path = _mark_paths(root, day)
    a_path.parent.mkdir(parents=True, exist_ok=True)
    b_path.parent.mkdir(parents=True, exist_ok=True)
    known = pd.Timestamp(day, tz="UTC") + pd.Timedelta(days=1, minutes=5)
    a = free_paper._seal(
        {
            "paper_protocol": free_paper.PAPER_PROTOCOL,
            "known_at": known.isoformat(),
            "date": day.isoformat(),
            "net_return": a_return,
            "cash_return": 0.0001,
        }
    )
    a_path.write_text(json.dumps(a, indent=2), encoding="utf-8")
    b = ab_paper._seal(
        {
            "protocol": ab_paper.AB_PROTOCOL,
            "known_at": known.isoformat(),
            "date": day.isoformat(),
            "net_return": b_return,
            "shadow_risk_multiplier": multiplier,
            "a_mark_record_sha256": a["record_sha256"],
        }
    )
    b_path.write_text(json.dumps(b, indent=2), encoding="utf-8")


def test_daily_anchor_selects_latest_known_record_per_source_day() -> None:
    early = {"known_at": "2026-09-05T01:00:00Z", "record_sha256": "a"}
    late = {"known_at": "2026-09-05T23:00:00Z", "record_sha256": "b"}
    anchors = linkage.select_daily_anchors(
        {
            "STATE_V02": [early, late],
            "STATE_V03": [],
            "STATE_V04": [],
        },
        not_before="2026-09-05T00:00:00Z",
    )
    assert len(anchors) == 1
    assert anchors[0]["record_sha256"] == "b"


def test_outcome_dates_start_next_full_utc_day() -> None:
    dates = linkage._expected_dates(pd.Timestamp("2026-09-05T23:59:59Z"), 3)
    assert dates == [date(2026, 9, 6), date(2026, 9, 7), date(2026, 9, 8)]


def test_max_drawdown_counts_first_day_loss_from_starting_equity() -> None:
    assert linkage._max_drawdown([-0.10, 0.02]) == pytest.approx(-0.10)


def test_materializer_links_only_complete_horizons_and_audit_recomputes(tmp_path: Path) -> None:
    _references(tmp_path)
    prospective.freeze_outcome_linkage(tmp_path, now=FREEZE_TIME)
    source_known = FREEZE_TIME + pd.Timedelta(hours=1)
    _write_source(tmp_path, source_known)
    for offset, values in enumerate(((0.01, 0.008, 0.75), (-0.02, -0.01, 0.50), (0.03, 0.025, 1.0)), start=1):
        _write_marks(
            tmp_path,
            date(2026, 9, 5 + offset),
            a_return=values[0],
            b_return=values[1],
            multiplier=values[2],
        )
    result = linkage.materialize_outcome_links(
        tmp_path,
        now=pd.Timestamp("2026-09-09T01:00:00Z"),
    )
    assert result["written_links"] == 2  # 1d and 3d are mature; 7/14/28 are pending.
    assert result["pending_incomplete_horizons"] == 3
    integrity = strict_outcome_linkage_integrity_report(tmp_path)
    assert integrity["ok"] is True, integrity
    assert integrity["matured_expected_link_count"] == 2
    assert integrity["link_count"] == 2


def test_selective_link_deletion_is_detected(tmp_path: Path) -> None:
    _references(tmp_path)
    prospective.freeze_outcome_linkage(tmp_path, now=FREEZE_TIME)
    source_known = FREEZE_TIME + pd.Timedelta(hours=1)
    _write_source(tmp_path, source_known)
    for offset in range(1, 4):
        _write_marks(
            tmp_path,
            date(2026, 9, 5 + offset),
            a_return=0.001,
            b_return=0.001,
            multiplier=1.0,
        )
    linkage.materialize_outcome_links(tmp_path, now=pd.Timestamp("2026-09-09T01:00:00Z"))
    three_day = linkage._link_path(tmp_path, "STATE_V02", date(2026, 9, 5), 3)
    three_day.unlink()
    integrity = strict_outcome_linkage_integrity_report(tmp_path)
    assert integrity["ok"] is False
    assert integrity["checks"]["all_matured_links_materialized"] is False
